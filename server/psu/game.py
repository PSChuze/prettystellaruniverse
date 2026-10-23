"""Game service: everything after login, for both discs in one world (plaintext behind stunnel 12061).

Protocol from EGS (github.com/essen/egs; docs/protocol public domain, code AGPL; re-implemented
from the descriptions). Same framing as login.py: u32le size | u16 BE command | u8 channel | u8 0.

  S>C 0x0202  hello on connect (as the login service)
  C>S 0x020D  key auth: GID + the 4-byte key login.py issued        -> 0x0D05 account flags
  C>S 0x0D06  character list request                                -> 0x0D03 four slots
  C>S 0x0D02  create character (slot, UTF-16 name, race, gender, appearance) -> stored, no reply
  C>S 0x0D07  options                                                -> logged
  C>S 0x0807  area change (quest l32, zone l16, map l16, entry l16, nb l16, party pos l32 at body +0x24):
              a map inside the current zone gets EGS's location sequence (0x0205 .. 0x0236);
              another zone/quest with archives in $PSU_NBL_DIR gets the zone sequence (quest too
              if it changes); anything else returns the player to entry 0 of the current map
  C>S 0x0F0A  object event (chairs so far)                            -> psu.dialog.object_event
  C>S 0x0811  enter a counter (type u8, zone/map/entry we came from, counter id at +0x24)
              -> EGS counter_enter: quest 0 + empty zone + counter packets
  C>S 0x1710  counter options (counter id at +0x24) -> 0x1711 background 255 (EGS default)
  C>S 0x0C05  counter files -> 0x0C06 + $PSU_NBL_DIR/counter-<id>.pack (EGS pack)
  C>S 0x0C0F  counter mission options -> 0x0C10 + counter-<id>.opts
  C>S 0x0C11 / 0x0C13  AotI story script item queries (seq.get_item / q.seq.get_item_num, sync) -> 0x0C12 /
              0x0C14 {wait id, 1}
  C>S 0x170B  counter background locations -> 0x170C + a captured table (optional, $PSU_CAPTURE_DIR/packet170c.bin)
  C>S 0x0812  leave the counter -> zone change back to where 0x0811 came from
  C>S 0x0110  character event (event id at +0x2C): 2 = type capabilities -> 0x0113 + the types
              table (progression.types_table; EGS send_0113)
  C>S 0x0304  chat: PS2 body +0x24 type/GID, +0x34 8 modifier bytes, message (UTF-16) right after
              (no 64-byte name, unlike EGS/PC) -> echoed as S>C 0x0304 (EGS send_0304, with the name)
  C>S 0x1A01  object/NPC dialog (PP cube, types menu, shops)           -> psu.dialog
  C>S 0x021D  Uni-cube (server select) opened                       -> 0x021E universe list
  C>S 0x021F  universe chosen (uni l32, entry l32 at +0x24; uni 0 = cancel)
              -> 0x0230 + the character load again; the client then sends 0x021C -> lobby
  S>C keep-alive every 5 s once authenticated: an empty 8-byte packet (EGS does the same)

The feature handlers live in their own modules (missions, combat, items, social, myroom, ...); this module
holds the session loop and dispatches to them. Characters are stored as their raw 0x0D02 body plus a JSON
progression state in psu.db (psu/db.py). The 0x020D key is the one-time key login.py put in the shared DB.

Env: PSU_GAME_PORTS (default 22061; `host:port` binds that item elsewhere), PSU_STATE_DIR (default <log dir>/../state), plus
PSU_BIND / PSU_LOG_DIR / PSU_IDLE_SECS.
"""
import asyncio
import json
import os
import secrets
import struct
import time
import sys
import traceback

from psu import combat, db as dbm, games, drops, econ, gm, growth, helpdesk, instance, judge, synthesis, myroom, shop as npcshop, dialog, items, missions, progression, social, xgame
from psu.area import NBL_DIR
from psu import season as seasons
from psu.world import c010d_char as world_c010d
from psu.world import (CASTS, WORLD, Player, c0111_spawn, c0204_unspawn, c0233_players, c0304_chat, cast_to,
                       spawn_packets, watch_buffs, watch_enemy)
from psu.area import COUNTER_QUEST, LOBBY_QUESTS, user_bin, c010d_char, LOBBY, area_sequence, available, counter_sequence, resolve, lobby_sequence, location_sequence
from psu.charload import UNIVERSES, c021e_universe_list, c0230_leave_area, c0d01_character, c0d04_ack, char_load_sequence, lobby_counter_work, slot_record, story_flag
from psu.login import TAG, hello, packet
from psu.observe import LOG_DIR, IDLE, event, hexdump, listen_spec, now
from psu import admin, adminport, board, events, presence

LISTEN = listen_spec("PSU_GAME_PORTS", "22061")
STATE_DIR = os.environ.get("PSU_STATE_DIR", os.path.join(LOG_DIR, "..", "state"))
social.STATE_DIR = STATE_DIR
FLAGS = (["PSU_VALUE_FLAG"], ["PSU_BOOL_FLAG"], ["PSU_TEMP_FLAG"])
SLOT_BYTES = 348          # one character record in 0x0D03 (EGS: 0:2784 bits for an empty slot)
KEEPALIVE = struct.pack("<II", 8, 0)
RACES = {0: "human", 1: "newman", 2: "cast", 3: "beast"}
ACC = bytes.fromhex("00011300")   # EGS writes 16#00011300:32 big-endian before a GID in 0x0D0x replies


def account_flags(gid):
    names = b"".join(n.encode("ascii").ljust(16, b"\0") for group in FLAGS for n in group)
    counts = struct.pack("<III", *(len(g) for g in FLAGS))
    return packet(0x0D05, bytes(4) + ACC + struct.pack("<I", gid) + bytes(8)
                  + ACC + struct.pack("<I", gid) + bytes(8) + names + counts)


def character_list(gid, slots=(None, None, None, None)):
    records = b"".join(r if r else bytes(SLOT_BYTES) for r in slots)
    return packet(0x0D03, bytes(4) + ACC + struct.pack("<I", gid) + bytes(8)
                  + ACC + struct.pack("<I", gid) + bytes(8) + bytes(4) + records)


def fragment_list(pkt, limit=0x4000):
    """The wire pieces for one command: itself, or its 0x0B03 fragments (see fragments())."""
    if len(pkt) <= limit:
        return [pkt]
    return [struct.pack("<I", 16 + len(pkt[o:o + limit])) + bytes.fromhex("0b030000") + struct.pack("<II", len(pkt), o)
            + pkt[o:o + limit] for o in range(0, len(pkt), limit)]


def fragments(pkt, limit=0x4000):
    """EGS: a command over 0x4000 bytes is sent as 0x0B03 fragments of <= 0x4000 bytes each:
    u32le fragment size (16 + chunk) | 0b 03 00 00 | u32le total size | u32le offset | chunk."""
    if len(pkt) <= limit:
        return pkt
    out = b""
    for o in range(0, len(pkt), limit):
        chunk = pkt[o:o + limit]
        out += struct.pack("<I", 16 + len(chunk)) + bytes.fromhex("0b030000") + struct.pack("<II", len(pkt), o) + chunk
    return out


def char_state(gid, slot, raw):
    """The saved progression/inventory state of a character; a new character gets level 1 in its chosen
    type and the offline starter pack."""
    if raw is None:
        return None
    d = dbm.get(STATE_DIR)
    st = d.char_state(gid, slot)
    if not st or "level" not in st:
        st = progression.new_state(raw)
        st["inv"], st["pas"], st["next_uuid"] = items.starter(raw, 1)
        d.save_char_state(gid, slot, st)
    elif progression.migrate_type_exp(st) is not None:   # a state main()'s migrate_db did not see (restored)
        d.save_char_state(gid, slot, st)
        d.audit("type_curve", d.username(gid) or "", gid, slot=slot, curve=progression.TYPE_CURVE, at="load")
    if renumber_ground_uuids(st):
        d.save_char_state(gid, slot, st)
    return st


def renumber_ground_uuids(st):
    """Picked-up drops keep their ground UUID (drops.UUID_BASE 0x40000000 and up), and the ground counter starts
    over with the process, so after a restart a new drop could share a UUID with a held item (a sell or use then
    hits the wrong one). On every character load such items get the next character UUID: the client
    learns UUIDs from the 0x0A0A that follows, so nothing it holds goes stale. Returns how many changed."""
    inv = st.get("inv") or []
    n = 0
    for x in inv:
        if x["uuid"] >= drops.UUID_BASE:
            new = max([st.get("next_uuid", 1)] + [y["uuid"] + 1 for y in inv if y["uuid"] < drops.UUID_BASE])
            x["uuid"], st["next_uuid"] = new, new + 1
            n += 1
    return n


def load_characters(gid):
    """{slot: raw 0x0D02 body} for this account (psu/db.py)."""
    return dbm.get(STATE_DIR).characters(gid)


def save_character(gid, body):
    slot, = struct.unpack_from("<I", body, 0x24)
    name = body[0x28:0x68].decode("utf-16-le", "replace").split("\0")[0]
    race = body[0x68]
    dbm.get(STATE_DIR).save_character(gid, slot, name, bytes(body))
    return slot, name, RACES.get(race, race)


async def party_join(inviter, invitee):
    """invitee accepted inviter's invite: make/extend the party, 1004 to members, 1005 to the newcomer."""
    party = inviter.party or social.Party(inviter)
    inviter.party = party
    if invitee in party.slots or None not in party.slots:
        return
    slot = party.slots.index(None)
    party.slots[slot] = invitee
    invitee.party = party
    for i, m in party.members():
        if m is not invitee:
            WORLD.relay(m, social.c1004_member(m, invitee, slot), f"0x1004 member {slot}")
    WORLD.relay(invitee, social.c1005_party(invitee, party), "0x1005 party")
    event(ev="party", op="join", leader=f"{party.slots[party.leader].gid:#x}", member=f"{invitee.gid:#x}", slot=slot)


async def party_leave(p, kicked=False):
    """p leaves (or is kicked): 1006 to the others, a solo 1005 to p; a party of one dissolves."""
    party = p.party
    if party is None:
        return
    slot = party.slot_of(p)
    party.slots[slot] = None
    p.party = None
    if p.gid in WORLD.online:
        if kicked:
            WORLD.relay(p, social.c1006_event(p, 3, slot), "0x1006 removed")
        WORLD.relay(p, social.c1005_solo(p), "0x1005 solo")
    rest = party.members()
    for i, m in rest:
        WORLD.relay(m, social.c1006_event(m, 13 if kicked else 4, slot), "0x1006 member gone")
    if slot == party.leader and rest:
        party.leader = rest[0][0]
        for i, m in rest:
            WORLD.relay(m, social.c1006_event(m, 7, party.leader), "0x1006 new leader")
    if len(rest) == 1:                    # alone: back to a solo party
        last = rest[0][1]
        last.party = None
        WORLD.relay(last, social.c1005_solo(last), "0x1005 solo")
    event(ev="party", op="kick" if kicked else "leave", member=f"{p.gid:#x}", slot=slot, left=len(rest))


def mission_tail_delay():
    """Seconds to hold a mission zone load after 0x0205 (load map), read on every mission entry from
    $PSU_STATE_DIR/mission_tail_delay (absent or bad: 0, no delay). A diagnostic toggle for real PS2s that
    hang on mission loads where PCSX2 does not: write e.g. 3 into the file, delete it to turn it off."""
    try:
        with open(os.path.join(STATE_DIR, "mission_tail_delay"), encoding="utf-8") as f:
            return max(0.0, min(float(f.read().strip() or 0), 30.0))
    except (OSError, ValueError):
        return 0


async def session(reader, writer, port):
    peer = writer.get_extra_info("peername") or ("?", 0)
    sid = f"{now():%Y%m%dT%H%M%S%f}_{peer[0]}_{peer[1]}_p{port}"
    gid = 0x10000000 | secrets.randbits(24)
    authed = False
    event(ev="open", sid=sid, peer=peer[0], port=port, role="psu-game")
    with open(os.path.join(LOG_DIR, "sessions", sid + ".log"), "a", encoding="utf-8") as log:
        def record(direction, data):
            log.write(f"{now().isoformat()} {direction} {len(data)} bytes\n{hexdump(data)}\n")
            log.flush()

        lock = asyncio.Lock()       # one writer at a time: this session and relays from other players

        async def send(data, what, quiet=False):
            async with lock:
                writer.write(data)
                await writer.drain()
            if not quiet:
                record("S>C", data)
                event(ev="send", sid=sid, what=what, n=len(data))

        me = None                   # world.Player once a character is in the game

        def load_sequence(raw):
            """Character load with this account's stored partner cards, blacklist and mail."""
            seq = []
            u = me.uni if me is not None else uni
            for data, what in char_load_sequence(gid, raw, slot, options, st=state, uni=u,
                                                 players=WORLD.population().get(u, 0) or 1):
                if what.startswith("0x1501"):
                    data = social.c1501_cards(gid)
                elif what.startswith("0x1512"):
                    data = social.c1512_blacklist(gid)
                seq.append((data, what))
            if state is not None and state.get("shortcuts"):
                seq.append((social.c0303_shortcuts(gid, state["shortcuts"]), "0x0303 chat shortcuts"))
            box = social.mailbox(gid)
            if box:
                seq.append((packet(0x150F, social.load_hdr(gid) + struct.pack("<I", len(box)) + b"".join(box)),
                            f"0x150f mail ({len(box)})"))
            return seq

        async def go_zone(dest, entry, mission_zone=False, var=None, key=None):
            """Zone/quest change to dest (quest, zone, map) at entry, with the full EGS zone sequence.
            key: the world zone to register in when it is not (quest, zone) (a My Room is per owner)."""
            nonlocal area, areanb
            area = dest
            me.area, me.entry = dest, entry
            await enter_zone(*(key or dest[:2]))
            host = 0
            if mission_zone and me.party is not None:      # the host is the party leader's LID in this zone
                leader = me.party.slots[me.party.leader]
                host = leader.lid if leader is not None and leader.zone == me.zone else me.lid
            elif mission_zone:
                host = me.lid
            tail_delay = mission_tail_delay() if mission_zone else 0
            for data, what in with_players(area_sequence(gid, me.raw, dest, entry, nb=areanb + 1, quest_change=True,
                                                         lid=me.lid, st=state, mission=mission_zone, var=var, host=host)):
                await send_paced(data, what)
                if tail_delay and what.startswith("0x0205"):
                    # diagnostic (see mission_tail_delay): hold the rest of the load while the console
                    # reads the mission field from disc
                    event(ev="mission_tail_delay", sid=sid, secs=tail_delay)
                    await asyncio.sleep(tail_delay)
            areanb += 2
            me.areanb = areanb
            await show_to_zone()

        def raw_char():
            return me.raw if me is not None else load_characters(gid).get(slot)

        def save_state():
            progression.tick_clock(state)
            dbm.get(STATE_DIR).save_char_state(gid, slot, state)

        def with_players(seq):
            """EGS sends 0x0233 (players already in the zone) just before 0x0208 finish loading."""
            others = WORLD.others(me)
            if not others:
                return seq
            i = next(n for n, (_, what) in enumerate(seq) if what.startswith("0x0208"))
            return seq[:i] + [(c0233_players(me, others), f"0x0233 players ({len(others)})")] + seq[i:]

        async def enter_zone(q, z):
            if me.zone is not None:
                await leave_zone()
            WORLD.enter(me, q, z)
            event(ev="zone_enter", sid=sid, zone=f"{q}/{z}", lid=me.lid, others=len(WORLD.others(me)))

        async def leave_zone():
            for d in WORLD.leave(me):
                WORLD.relay(d, c0204_unspawn(d, me), "0x0204 unspawn")

        async def show_to_zone():
            for d in WORLD.others(me):
                for data, what in spawn_packets(d, me):
                    WORLD.relay(d, data, what)
            party_location()
            presence.moved(me)

        async def write_paced(wire):
            """In 1 KB pieces, 5 ms apart: PCSX2's Sockets backend copes badly with big bursts."""
            for o in range(0, len(wire), 1024):
                writer.write(wire[o:o + 1024])
                await writer.drain()
                if o + 1024 < len(wire):
                    await asyncio.sleep(0.005)

        async def wait_frag_ack(n, total):
            """Read until the client sends 0x0B05 (fragment ack) or 5 s pass; other packets stay in buf."""
            nonlocal buf
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                o = 0
                while o + 8 <= len(buf):
                    size, = struct.unpack_from("<I", buf, o)
                    if size < 8:
                        break
                    if buf[o + 4:o + 6] == b"\x0b\x05" and size == 8:
                        buf = buf[:o] + buf[o + 8:]
                        event(ev="frag_ack", sid=sid, fragment=n, of=total)
                        return True
                    o += size
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    event(ev="frag_ack_timeout", sid=sid, fragment=n, of=total)
                    return False
                try:
                    data = await read_some(left)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    return False
                record("C>S", data)
                buf += data

        async def send_paced(data, what):
            """Over 0x4000 bytes: 0x0B03 fragments (EGS packet_fragment_send), one at a time, each
            waiting for the client's 0x0B05 (PS2 acked only once when all three went back to back)."""
            frags = fragment_list(data)
            async with lock:
                for n, frag in enumerate(frags, 1):
                    await write_paced(frag)
                    if len(frags) > 1:
                        await wait_frag_ack(n, len(frags))
            record("S>C", data)
            event(ev="send", sid=sid, what=what, n=len(data), fragments=len(frags))

        async def mission_check(force_clear=False):
            """Start (flag-gated missions) and clear per the quest rules; on clear: rank, rewards, results."""
            if mission is None or not mission.get("in") or me is None:
                return
            qid, owner = mission["qid"], mission.get("owner", gid)
            q = (missions.mission(qid) or {}).get("quest") or {}
            r = judge.run(me.uni, qid, owner)

            def flag(z, m_, e_):
                return judge.flag_set(me.uni, qid, owner, z, m_, e_)
            if not r["started"] and (q.get("start") is None or judge.evaluate(q.get("start"), flag, at=me.area)):
                r["started"], r["t0"] = True, time.time()
                for d in [me] + WORLD.others(me):
                    data = missions.c0c09(d.gid, 0, 0, lid=d.lid)
                    if d is me:
                        await send(data, "0x0c09 mission start")
                    else:
                        WORLD.relay(d, data, "0x0c09 mission start")
                event(ev="mission_start", sid=sid, quest=qid)
            if r["cleared"] or not r["started"] or not (force_clear or judge.evaluate(q.get("clear"), flag, at=me.area)):
                return
            r["cleared"] = True
            elapsed = int(time.time() - (r["t0"] or time.time()))
            rank, total, secs = judge.rank_of(q, elapsed, r, flag)
            got = judge.rewards(q, rank, flag)
            tp = ((q.get("rank") or {}).get("type_points") or [0, 0, 0, 0])[rank]
            event(ev="mission_clear", sid=sid, quest=qid, secs=elapsed, rank=rank, total=total, kills=r["kills"],
                  rewards=got, type_points=tp)
            members = [me] + WORLD.others(me)
            store = dbm.get(STATE_DIR)
            party = [(p.gid, getattr(p, "slot", 0), store.char_name(p.gid, getattr(p, "slot", 0)) or "?")
                     for p in members if p.state]
            store.record_clear(me.game, me.uni, qid, elapsed, rank, total, r["kills"], party)
            for p in members:
                if not p.state:
                    continue
                meseta, new_items, out = 0, [], []
                for rw in got:
                    if rw["flags"] & 4 or rw["item"] == "ffffffff":
                        meseta += int(rw["meseta"] * events.meseta_mult(p.game))
                    elif rw["item"] in items.db():
                        added, err = items.add_item(p.state, rw["item"], max(1, rw["qty"]))
                        if not err:
                            new_items.append(added)
                            out.append((items.c0106_add(p.gid, p.lid, added), f"0x0106 reward {rw['item']}"))
                p.state["meseta"] = min(p.state.get("meseta", 0) + meseta, 999_999_999)
                if missions.record_clear(p.state, qid, rank):
                    event(ev="story_clear", sid=sid, quest=qid, gid=f"{p.gid:#010x}", rank=rank)
                old_tl, new_tl = judge.add_type_exp(p.raw, p.state, tp)
                dbm.get(STATE_DIR).save_char_state(p.gid, getattr(p, "slot", 0), p.state)
                block = judge.result_block(rank, total, secs, meseta, new_items)
                out = [(judge.c0c09_results(p.gid, p.lid, elapsed, block), f"0x0c09 clear rank {rank}")] + out
                out.append((combat.c0115(p, 0xFFFFFFFF), "0x0115 rewards"))
                if new_tl != old_tl:
                    out.append((c010d_char(p.gid, p.raw, p.area, p.entry, lid=p.lid, st=p.state, game=p.game),
                                f"0x010d type level {new_tl}"))
                for data, what in out:
                    if p is me:
                        await send_paced(data, what)
                    else:
                        WORLD.relay(p, data, what)

        async def synth(cmd_, body_):
            """Synthesis subs (psu/synthesis.py): replies in the reference order, inventory via 0x0106."""
            cur = getattr(me, "synth_cur", None)
            if cur is None:
                cur = me.synth_cur = {"entry": None, "variant": -1}
            out, inv = synthesis.handle(me, state, cur, cmd_, body_)
            sub_ = struct.unpack_from("<I" if cmd_ == 0x130D else "<h", body_, 0x28)[0]
            event(ev="synth", sid=sid, cmd=f"{cmd_:#06x}", sub=sub_, replies=len(out),
                  inv=[(k, s_["id"]) for k, s_, _ in inv])
            for kind, s_, n in inv:
                await send(items.c0106_add(gid, me.lid, s_) if kind == "add" else items.c0106_remove(gid, me.lid, s_, n),
                           f"0x0106 {kind} (synthesis)")
            for data, what in out:
                await send(data, what, quiet=what.startswith("synth list"))
            if inv or cmd_ == 0x130F:
                save_state()

        async def party_hp(p):
            """0x1022 with p's party slot to every member (solo: p alone, slot 0)."""
            members = p.party.members() if p.party is not None else [(0, p)]
            k = p.party.slot_of(p) if p.party is not None else 0
            for _, m in members:
                data = social.c1022_hp(m, p.state.get("hp", 0) if p.state else 0, k)
                if m is me:
                    await send(data, "0x1022 hp")
                else:
                    WORLD.relay(m, data, "0x1022 hp")

        def party_location():
            """0x100E: my location to every other party member."""
            if me is None or me.party is None:
                return
            k = me.party.slot_of(me)
            for _, m in me.party.members():
                if m is not me:
                    WORLD.relay(m, social.c100e_location(m, me, k), "0x100e member location")

        async def gm_command(text):
            """Chat "!" commands (psu/gm.py). Replies go to the sender as 0x0228 dialogs."""
            await gm.run(gm.Ctx(sid, gid, me, state, send, save_state, go_zone), text)

        async def warp_to(dest, entry):
            """GM summon / goto (gm.bring, queued with run_job): a zone change, refused on a mission."""
            if mission is not None:
                return "That player is on a mission."
            await go_zone(dest, entry)

        jobs, wake = [], asyncio.Event()    # work other sessions queue for this one (Player.run_job)
        pending_read = None

        def run_job(fn):
            """Run the coroutine function fn in this session's own loop (it may read fragment acks)."""
            jobs.append(fn)
            wake.set()

        async def read_some(timeout, jobs_too=False):
            """reader.read through one task that outlives a timeout (nothing read is lost); with jobs_too also
            woken (None) when run_job queued work."""
            nonlocal pending_read
            if pending_read is None:
                pending_read = asyncio.ensure_future(reader.read(65536))
            waits = {pending_read}
            woke = asyncio.ensure_future(wake.wait()) if jobs_too else None
            if woke is not None:
                waits.add(woke)
            done, _ = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if woke is not None and not woke.done():
                woke.cancel()
            if pending_read in done:
                t, pending_read = pending_read, None
                return t.result()
            if woke is not None and woke in done:
                return None
            raise asyncio.TimeoutError

        async def pp_tick():
            """Weapon PP regeneration (psu/combat.py pp_regen), in a mission once the client moves in its zone."""
            if (me is None or state is None or me.zone is None or mission is None or not mission.get("in")
                    or not getattr(me, "pp_ready", False)):
                return
            changed = combat.pp_regen(state)
            if changed:
                save_state()
                for w in changed:
                    await send(items.c0105_pp(gid, me.lid, w), "0x0105 pp regen", quiet=True)

        async def keepalive():
            try:
                while True:
                    await asyncio.sleep(combat.PP_TICK)
                    await send(KEEPALIVE, "keepalive", quiet=True)
                    run_job(pp_tick)
            except (ConnectionError, OSError):
                writer.transport.abort()     # wakes the session's read: the peer is gone

        ka = None
        uni = 1                     # universe before the Player exists
        options = None
        slot, selected = 0, False
        area, areanb = None, 0      # (quest, zone, map) once in the lobby; EGS area counter (0x0205/0x0208)
        prev = None                 # ((quest, zone, map), entry) to return to when leaving a counter
        state = None                # progression + inventory of the selected character (psu/progression.py)
        mission = None              # {qid, origin, in, start_pending, t0} while a mission is accepted / running
        errors = 0                  # handler exceptions this session (psu: see the guard in the loop)
        shop_list = []              # the NPC shop list last sent (0x010A event 1): [(item key, disc id)]
        lc_work = {}                # SAVE_LC00..05 as the client last wrote them (0x0D04; the Colony gates set them)
        entrance = 1                # 0x080E Entrance (+0x2F) this client sent; AotI's room data repeats it (myroom.c1327)
        buf = b""
        try:
            await send(hello(gid), "0x0202 hello")
            while True:
                while len(buf) >= 8:
                    size, = struct.unpack_from("<I", buf)
                    if size < 8 or len(buf) < size:
                        break
                    pkt, buf = buf[:size], buf[size:]
                    cmd, = struct.unpack_from(">H", pkt, 4)
                    body = pkt[8:]
                    event(ev="recv", sid=sid, cmd=f"{cmd:#06x}", channel=pkt[6], size=size, body=body[:96].hex())
                    try:
                        if pkt[6] == 1 and cmd in CASTS and me is not None and me.zone is not None:
                            if cmd == 0x0102 and len(body) >= 0x4C:     # where the player stands (for newcomers)
                                d16, = struct.unpack_from("<H", body, 0x2E)
                                x, y, zz = struct.unpack_from("<fff", body, 0x34)
                                me.pos = (x, y, zz, d16 / 182.0416)
                            for d in WORLD.others(me):
                                WORLD.relay(d, cast_to(d, me, cmd, body), f"cast {cmd:#06x}", quiet=True)
                            if state is not None and cmd in (0x0102, 0x0104, 0x010F):     # PP an attack spent
                                me.pp_ready = True
                                spent = combat.pp_cast(me, cmd, body)
                                if spent is not None:
                                    save_state()
                                    await send(items.c0105_pp(gid, me.lid, spent), "0x0105 pp use")
                        if cmd == 0x020D and len(body) >= 0x2C:
                            claimed, = struct.unpack_from("<I", body, 0x24)
                            got = dbm.get(STATE_DIR).take_key(claimed, body[0x28:0x2C])
                            if got is None:
                                event(ev="auth_refused", sid=sid, gid=f"{claimed:#010x}")
                                return                      # unknown or reused key: drop the connection (finally closes)
                            games.set_current(got[0])       # this session's task: NBLs and codecs per disc
                            games.set_platform(got[1])
                            if dbm.get(STATE_DIR).banned(claimed):
                                event(ev="auth_refused", sid=sid, gid=f"{claimed:#010x}", reason="banned")
                                return
                            gid = claimed
                            authed = True
                            event(ev="auth", sid=sid, gid=f"{gid:#010x}", game=games.current(), platform=games.platform())
                            await send(account_flags(gid), "0x0d05 account flags")
                            ka = asyncio.create_task(keepalive())
                        elif cmd == 0x080E and len(body) >= 0x28:
                            entrance = body[0x27]
                        elif cmd == 0x0D06:
                            if state is not None:           # the list reads the saved play time
                                save_state()
                            chars = load_characters(gid)
                            slots = [slot_record(chars.get(i), char_state(gid, i, chars.get(i))) for i in range(4)]
                            await send_paced(character_list(gid, slots), f"0x0d03 character list ({len(chars)} used)")
                        elif cmd == 0x0D04 and len(body) >= 0x40:   # chrwork / chrflag write
                            if body[0x24:0x2B] == b"SAVE_LC":
                                try:
                                    lc_work[int(body[0x2B:0x2D])] = struct.unpack_from("<i", body, 0x3C)[0]
                                except ValueError:
                                    pass
                            # Both discs' scripts wait for this ack (AotI 9000 frames = 150 s, PSU 450 = 7.5 s): the Spaceport
                            # counter's chrwork_set -1 x6 kept lobby.start's sync blocked, then the timeout's -1 was popped
                            # as the planet (cancel). EGS answers every 0x0D04 the same way.
                            ack, changed = story_flag(state, body, lambda n: missions.story_gate(state, n))
                            if changed:
                                save_state()
                                event(ev="story_flag", sid=sid, flag=body[0x24:0x34].split(b"\0")[0].decode("latin1"),
                                      value=struct.unpack_from("<i", body, 0x3C)[0])
                            await send(c0d04_ack(gid, ack), "0x0d04 work ack")
                        elif cmd == 0x0D07 and len(body) >= 0x38:
                            options = body[0x24:0x38]
                            event(ev="options", sid=sid, options=options.hex())
                        elif cmd == 0x020B and len(body) >= 0x28:
                            if state is not None:           # the character played so far: bank its play time
                                progression.stop_clock(state)
                                save_state()
                            slot, = struct.unpack_from("<I", body, 0x24)
                            raw = load_characters(gid).get(slot)
                            selected = raw is not None
                            state = char_state(gid, slot, raw) if raw else None
                            progression.start_clock(state)  # play time counts while a character is selected
                            if state is not None:
                                state["game"] = games.current()   # the disc it last played on (psu/board.py)
                            event(ev="select", sid=sid, gid=f"{gid:#010x}", slot=slot, found=raw is not None,
                                  level=state["level"] if state else None, items=len(state["inv"]) if state else None)
                            if raw:
                                for data, what in load_sequence(raw):
                                    await send_paced(data, what)
                        elif cmd == 0x021C:
                            event(ev="load_complete", sid=sid)
                            raw = load_characters(gid).get(slot) if selected else None
                            if raw:
                                if me is None:
                                    me = Player(gid, raw, writer, send)
                                    me.uni = uni if uni in dict(UNIVERSES) else UNIVERSES[0][0]
                                    WORLD.online[gid] = me
                                    me.warp, me.run_job = warp_to, run_job
                                    presence.online(me)             # partner card holders: online flag
                                    admin.welcome(me)               # the current notice as a ticker
                                    helpdesk.deliver_later(me)      # staff answers sent while offline
                                area = (LOBBY["quest"], LOBBY["zone"], LOBBY["map"])
                                me.raw, me.area, me.entry, me.areanb, me.state = raw, area, LOBBY["entry"], 2, state
                                me.slot = slot
                                await enter_zone(*area[:2])
                                for data, what in with_players(lobby_sequence(gid, raw, lid=me.lid, st=state)):
                                    await send_paced(data, what)
                                areanb = 2
                                await show_to_zone()
                                event(ev="lobby", sid=sid, gid=f"{gid:#010x}", area=f"{LOBBY['quest']}/{LOBBY['zone']}/{LOBBY['map']}")
                        elif cmd == 0x0807 and len(body) >= 0x34:
                            q, z, m, entry, nchanges, party = struct.unpack_from("<IHHHHI", body, 0x24)
                            if me is not None:
                                me.pp_ready = False             # no PP regeneration packets during the load
                            asked = f"{q}/{z}/{m}"
                            if (mission is None and me is not None and me.party is not None and me.party.mission
                                    and q == me.party.mission["qid"] and z == 0xFFFF):    # a member follows the leader
                                if missions.playable(q, me.game):
                                    mission = {"qid": q, "origin": prev, "in": False, "start_pending": False,
                                               "owner": me.party.mission["owner"]}
                                else:           # the leader's mission is on the other disc: bounce
                                    event(ev="mission_follow_refused", sid=sid, quest=q, game=me.game)
                            if mission is not None and me is not None:
                                mq, mmaps = mission["qid"], missions.maps(mission["qid"])
                                if q == mq and z == 0xFFFF:                       # departing into the mission
                                    ez, em, ee = missions.mission(mq)["enter"]
                                    event(ev="mission_enter", sid=sid, quest=mq, to=f"{ez}/{em}/{ee}")
                                    mission["in"], mission["start_pending"] = True, True
                                    await go_zone((mq, ez, em), ee, mission_zone=True,
                                                  key=(mq, (ez, mission.get("owner", gid))))
                                    continue
                                if mission.get("in") and not (q == mq and m in mmaps.get(z, ())):   # leaving
                                    event(ev="mission_leave", sid=sid, quest=mq, asked=asked)
                                    await send(social.c1006_event(me, 10, 0), "0x1006 mission end")
                                    if me.party is not None and me.party.slots[me.party.leader] is me:
                                        me.party.mission = None
                                    back = mission["origin"]
                                    nxt = missions.story_next(mq, judge.run(me.uni, mq, mission.get("owner", gid))["cleared"])
                                    if nxt is not None and available(*nxt[0][:2]):     # a cleared first act: its base
                                        event(ev="story_base", sid=sid, quest=mq, base=f"{nxt[0]} entry {nxt[1]}")
                                        back = nxt
                                    mission = None
                                    if state is not None:
                                        state["hp"] = progression.derived(me.raw, state)["max_hp"]
                                        save_state()
                                    if back is not None:
                                        await go_zone(back[0], back[1])
                                    continue
                            to_room = (q == myroom.QUEST and z in (0, 1, 0xFFFF)) or (q in LOBBY_QUESTS and z == 1 and m == 1)
                            if to_room and me is not None and state is not None:
                                # EGS Colony elevators (1120000/0/100), the native ones (zone 1 = the room zone, map 1),
                                # or the room console's visit warp (-1s): own room
                                event(ev="myroom_enter", sid=sid, asked=asked, entry=entry)
                                await go_zone((myroom.QUEST, myroom.ZONE, myroom.MAP), 0, var=myroom.ROOM_VAR,
                                              key=(myroom.QUEST, gid))
                                owner_byte = user_bin(gid, me.lid, me.raw, me.area, me.entry, st=state)[0x15]
                                await send(myroom.c1327(me, gid, owner_byte, me.raw, state, entrance), "0x1327 room data")
                                continue
                            q, z, m = resolve(q, z, m)
                            raw = load_characters(gid).get(slot) if selected else None
                            solo = party == 0xFFFFFFFF
                            same_zone = area is not None and (q, z) == area[:2] and solo
                            other_zone = area is not None and not same_zone and solo and available(q, z)
                            how = "map" if same_zone else "zone" if other_zone else "bounce"
                            event(ev="area_change", sid=sid, asked=asked, to=f"{q}/{z}/{m}", entry=entry, nb=nchanges,
                                  party=f"{party:#x}", handled=how)
                            if raw and area is not None and me is not None:
                                if same_zone:
                                    area = (q, z, m)
                                    me.area, me.entry = area, entry
                                    seq = location_sequence(gid, raw, area, entry, nb=areanb + 1, lid=me.lid, st=state)
                                elif other_zone:
                                    quest_change = q != area[0]
                                    origin = area[0]
                                    area = (q, z, m)
                                    me.area, me.entry = area, entry
                                    await enter_zone(q, (z, mission.get("owner", gid)) if mission and mission.get("in") else z)
                                    seq = with_players(area_sequence(gid, raw, area, entry, nb=areanb + 1,
                                                                     quest_change=quest_change, lid=me.lid, st=state))
                                    if q == 1104000:    # Spaceport lobby counter reads SAVE_LC00..05 from 0x0D01
                                        work = lobby_counter_work(origin, lc_work)
                                        event(ev="lobby_counter", sid=sid, origin=origin, lc=[work[k] for k in sorted(work)])
                                        seq = [(c0d01_character(gid, raw, options, work),
                                                "0x0d01 character (lobby counter work)")] + seq
                                else:       # no archives for that quest/zone (e.g. My Room): back to entry 0 here
                                    me.entry = 0
                                    seq = location_sequence(gid, raw, area, 0, nb=areanb + 1, lid=me.lid, st=state)
                                for data, what in seq:
                                    await send_paced(data, what)
                                areanb += 2
                                me.areanb = areanb
                                if other_zone:
                                    await show_to_zone()
                                else:
                                    party_location()
                        elif cmd == 0x0811 and len(body) >= 0x30:
                            ctype, _, fz, fm, fe, counter = struct.unpack_from("<BBHHHI", body, 0x24)
                            raw = load_characters(gid).get(slot) if selected else None
                            event(ev="counter_enter", sid=sid, counter=counter, type=ctype, came_from=f"{fz}/{fm}/{fe}")
                            if (raw and area is not None and me is not None and missions.base(area[0])
                                    and missions.base_counter(area[0], counter) is None):
                                # a story base's way back (counter 0 / 120, 122, ..): no table, back to the HQ
                                event(ev="story_base_leave", sid=sid, base=area[0], counter=counter)
                                await go_zone(*missions.STORY_HOME)
                                continue
                            if raw and area is not None and me is not None:
                                prev = ((area[0], fz, fm), fe)
                                area = (COUNTER_QUEST, 0, 0)
                                await leave_zone()          # counters are private
                                for data, what in counter_sequence(gid, raw, counter, nb=areanb + 1, lid=me.lid, st=state):
                                    await send_paced(data, what)
                                areanb += 2
                                me.areanb = areanb
                        elif cmd == 0x1710 and len(body) >= 0x28:
                            counter, = struct.unpack_from("<I", body, 0x24)
                            event(ev="counter_options", sid=sid, counter=counter)
                            await send(packet(0x1711, bytes(20) + ACC + struct.pack("<I", gid) + bytes(8)
                                              + bytes([255, 0, 0, 0])), "0x1711 counter bg")
                        elif cmd in (0x0C05, 0x0C0F) and len(body) >= 0x28:
                            counter, = struct.unpack_from("<I", body, 0x24)
                            table = missions.table_for(counter)
                            if table is not None and missions.data()["counters"]:
                                event(ev="counter_missions", sid=sid, counter=counter, table=table)
                                if cmd == 0x0C05:
                                    await send_paced(missions.c0c06(gid, table), f"0x0c06 counter table {table}")
                                else:
                                    await send(missions.c0c10(gid, table, state), f"0x0c10 counter table {table} options")
                                continue
                            ext = "pack" if cmd == 0x0C05 else "opts"
                            path = games.data_file(NBL_DIR, f"counter-{counter}.{ext}")
                            event(ev="counter_" + ext, sid=sid, counter=counter, have=os.path.exists(path))
                            if os.path.exists(path):
                                with open(path, "rb") as f:
                                    blob = f.read()
                                if cmd == 0x0C05:
                                    await send_paced(packet(0x0C06, bytes(36) + struct.pack("<I", 1) + blob), f"0x0c06 counter {counter} pack")
                                else:
                                    await send(packet(0x0C10, bytes(20) + ACC + struct.pack("<I", gid) + bytes(8)
                                                      + bytes([1, 0]) + struct.pack("<H", len(blob)) + blob), f"0x0c10 counter {counter} opts")
                        elif cmd == 0x0110 and len(body) >= 0x30:
                            ev_id, param = struct.unpack_from("<II", body, 0x2C)
                            event(ev="char_event", sid=sid, event_id=ev_id, param=param)
                            if ev_id == 2 and state is not None:     # types menu: computed per-type preview
                                await send(packet(0x0113, bytes(8) + struct.pack("<I", gid) + bytes(8) + ACC + struct.pack("<I", gid)
                                                  + bytes(8) + struct.pack("<I", gid) + bytes(4)
                                                  + progression.types_table(raw_char(), state)), "0x0113 type capabilities")
                            elif ev_id == 7 and me is not None:          # down: a Scape Doll (03010900) revives
                                doll = next((x for x in (state or {}).get("inv", []) if x["id"] == "03010900"), None)
                                if doll is not None:
                                    mx = progression.derived(me.raw, state)["max_hp"]
                                    rec = items.var_rec(doll)
                                    items.remove_item(state, doll["uuid"], 1)
                                    state["hp"] = mx
                                    save_state()
                                    event(ev="death", sid=sid, doll=True)
                                    # 0x0105 event 8 = the client uses the doll itself (count - 1, revive self-hit)
                                    await send(packet(0x0105, struct.pack("<H", me.lid) + bytes(6) + struct.pack("<I", gid)
                                                      + bytes(8) + ACC + struct.pack("<I", gid) + bytes(8)
                                                      + struct.pack("<IIBBBBI", gid, me.lid, 0, 8, rec[0x14], 0,
                                                                    doll["uuid"])), "0x0105 scape doll")
                                    for d in [me] + WORLD.others(me):
                                        data = combat.c0117(d, mx) if d is me else None
                                        if d is me:
                                            await send(data, f"0x0117 revived {mx}")
                                    await party_hp(me)
                                else:
                                    event(ev="death", sid=sid, doll=False)
                                    if mission is not None and mission.get("in"):
                                        judge.run(me.uni, mission["qid"], mission.get("owner", gid))["deaths"] += 1
                                    await send(c0111_spawn(me, me, 3, 1), "0x0111 incapacitated")
                            elif ev_id == 8 and me is not None and state is not None:     # return to the lobby after death
                                state["hp"] = progression.derived(me.raw, state)["max_hp"]
                                save_state()
                                back = mission["origin"] if mission is not None else prev
                                if mission is not None:
                                    event(ev="mission_leave", sid=sid, quest=mission["qid"], asked="death")
                                    await send(social.c1006_event(me, 10, 0), "0x1006 mission end")
                                    mission = None
                                if back is not None:
                                    await go_zone(back[0], back[1])
                            elif ev_id == 3 and state is not None and me is not None:     # change type to `param`
                                if progression.can_change_type(state, param):
                                    state["cls"] = param
                                    state["hp"] = min(state["hp"], progression.derived(me.raw, state)["max_hp"])
                                    save_state()
                                    event(ev="type_change", sid=sid, cls=param)
                                    await send(c010d_char(gid, me.raw, me.area, me.entry, lid=me.lid, st=state),
                                               f"0x010d type {param}")
                        elif cmd == 0x0109 and len(body) >= 0x74 and state is not None:     # palette
                            right, left = items.set_palette(state, body)
                            save_state()
                            event(ev="palette", sid=sid, right=[f"{u:x}" for u in right], left=[f"{u:x}" for u in left],
                                  items=state["apal"])
                        elif cmd in myroom.SHOP_CMDS and await myroom.shop_handle(myroom.shop_ctx(   # My Shop, PM name
                                me=me, state=state, send=send, send_paced=send_paced, save_state=save_state,
                                event=event, sid=sid, db=dbm.get(STATE_DIR), world=WORLD, chars=load_characters),
                                cmd, body):
                            pass
                        elif cmd == 0x130B and len(body) >= 0x30 and state is not None and me is not None:   # room goods
                            rslot, = struct.unpack_from("<h", body, 0x28)
                            ruuid, = struct.unpack_from("<I", body, 0x2C)
                            goods, err, change = myroom.set_object(state, rslot, ruuid)
                            event(ev="myroom_object", sid=sid, slot=rslot, uuid=f"{ruuid:x}", goods=goods, error=err)
                            if err is None:
                                save_state()
                                kind, islot, n = change
                                await send(items.c0106_add(gid, me.lid, islot) if kind == "add"
                                           else items.c0106_remove(gid, me.lid, islot, n), f"0x0106 {kind} {islot['id']}")
                                await send(myroom.c130c(me, rslot, goods), f"0x130c room slot {rslot} = {goods:#x}")
                                for d in WORLD.others(me):
                                    WORLD.relay(d, myroom.c130c(d, rslot, goods), "0x130c room slot")
                        elif cmd == 0x130D and len(body) >= 0x34 and me is not None:          # room console menus
                            sub, b = struct.unpack_from("<I", body, 0x28)[0], struct.unpack_from("<I", body, 0x30)[0]
                            if sub == 0 and state is not None:                # withdraw from the room stock
                                qty, index = struct.unpack_from("<II", body, 0x2C)
                                added, err = myroom.withdraw(state, index, qty)
                                event(ev="myroom_stock", sid=sid, op="withdraw", index=index, qty=qty, error=err)
                                if err is None:
                                    save_state()
                                    await send(items.c0106_add(gid, me.lid, added), "0x0106 add (stock)")
                                    await send_paced(myroom.c1324(me, state), "0x1324 stock")
                                continue
                            if sub in (1, 4, 6, 7, 8) and state is not None:     # synthesis (production)
                                await synth(cmd, body)
                                continue
                            out = myroom.menu_reply(me, state, sub, b)
                            if sub == 0x0C and b == 0 and state is not None:
                                owner_byte = user_bin(gid, me.lid, me.raw, me.area, me.entry, st=state)[0x15]
                                out = [(myroom.c1327(me, gid, owner_byte, me.raw, state, entrance), "0x1327 room data")]
                            event(ev="myroom_msg", sid=sid, cmd="0x130d", sub=f"{sub:#x}", b=b,
                                  answered=[w for _, w in out])
                            for data, what in out:
                                await send_paced(data, what)
                        elif cmd == 0x130F and len(body) >= 0x40 and me is not None and state is not None:
                            sub, = struct.unpack_from("<h", body, 0x28)
                            uuid, = struct.unpack_from("<I", body, 0x3C)
                            if sub not in (0, 4):                             # synthesis (production)
                                await synth(cmd, body)
                                continue
                            if sub == 0:                                      # deposit into the room stock
                                qty, = struct.unpack_from("<h", body, 0x2A)
                                removed, err = myroom.deposit(state, uuid, qty)
                                event(ev="myroom_stock", sid=sid, op="deposit", uuid=f"{uuid:x}", qty=qty, error=err)
                                if err is None:
                                    save_state()
                                    await send(items.c0106_remove(gid, me.lid, removed, removed["qty"]), "0x0106 remove (stock)")
                                    await send_paced(myroom.c1324(me, state), "0x1324 stock")
                                continue
                            removed = myroom.give_item(state, uuid) if sub == 4 else None
                            ok = removed is not None
                            event(ev="myroom_msg", sid=sid, cmd="0x130f", sub=sub, uuid=f"{uuid:x}", given=ok)
                            if ok:
                                evolved, ptype, pstats, pdeltas = econ.probo_gift(state, removed)
                                save_state()
                                event(ev="probo", sid=sid, item=removed["id"], type=ptype, stats=pstats, evolved=evolved)
                                await send(items.c0106_remove(gid, me.lid, removed, 1), "0x0106 remove (probo)")
                                await send(econ.c1312(gid, me.lid, evolved, ptype, pstats, pdeltas), "0x1312 probo fed")
                                await send(myroom.c130e(me, 22, 0, 0), "0x130e probo animation")
                                for d in WORLD.others(me):
                                    WORLD.relay(d, myroom.c130e(d, 22, 0, 0), "0x130e probo animation")
                        elif cmd == 0x1304 and len(body) >= 0x2C and me is not None and state is not None:   # visit a room
                            owner, = struct.unpack_from("<I", body, 0x24)
                            other = WORLD.online.get(owner)
                            o_raw = other.raw if other is not None else load_characters(owner).get(0)
                            o_st = other.state if other is not None else (dbm.get(STATE_DIR).char_state(owner, 0) or {})
                            event(ev="myroom_visit", sid=sid, owner=f"{owner:#x}", online=other is not None,
                                  known=o_raw is not None)
                            if o_raw is not None and owner != gid:
                                await go_zone((myroom.QUEST, myroom.ZONE, myroom.MAP), 0, var=myroom.ROOM_VAR,
                                              key=(myroom.QUEST, owner))
                                o_byte = user_bin(owner, 0, o_raw, me.area, 0, st=o_st)[0x15]
                                await send(myroom.c1327(me, owner, o_byte, o_raw, o_st, 0), "0x1327 room data (visit)")
                                myroom.shop_visited(myroom.shop_ctx(me=me, state=state, save_state=save_state,
                                                                    db=dbm.get(STATE_DIR), world=WORLD,
                                                                    chars=load_characters), owner)
                        elif cmd in (0x1328, 0x1319) and me is not None:
                            event(ev="myroom_msg", sid=sid, cmd=f"{cmd:#06x}", body=body[0x24:0x40].hex())
                        elif cmd == 0x010A and len(body) >= 0x34 and state is not None and me is not None:   # NPC shop
                            sev, qa, _qb = body[0x2C], body[0x2E], body[0x2F]
                            param, = struct.unpack_from("<I", body, 0x30)
                            if sev == 1:
                                shop_list, cat = npcshop.listing(param, state["level"])
                                event(ev="shop", sid=sid, op="enter", npc=param, category=cat,
                                      tier=npcshop.tier_for(state["level"]), n=len(shop_list or []))
                                if shop_list is None:
                                    shop_list = []
                                    await send(npcshop.c010a_fail(me, 1), "0x010a shop (unknown)")
                                else:
                                    await send(npcshop.c010a_list(me, shop_list, cat), f"0x010a shop list ({len(shop_list)})")
                            elif sev == 2:
                                key = shop_list[param][0] if param < len(shop_list) else None
                                slot_, err = npcshop.buy(state, key, qa) if key else (None, "no such entry")
                                event(ev="shop", sid=sid, op="buy", index=param, item=key, qty=qa, error=err,
                                      meseta=state.get("meseta"))
                                if err:
                                    await send(npcshop.c010a_fail(me, 2), "0x010a buy failed")
                                else:
                                    save_state()
                                    await send(combat.c0115(me, 0xFFFFFFFF), "0x0115 meseta")
                                    await send(npcshop.c010a_bought(me, slot_), f"0x010a bought {key}")
                            elif sev == 3:
                                qty, gain = npcshop.sell(state, param, qa)
                                event(ev="shop", sid=sid, op="sell", uuid=f"{param:x}", qty=qty, gain=gain,
                                      meseta=state.get("meseta"))
                                if not qty:
                                    await send(npcshop.c010a_fail(me, 3), "0x010a sell failed")
                                else:
                                    save_state()
                                    await send(combat.c0115(me, 0xFFFFFFFF), "0x0115 meseta")
                                    await send(npcshop.c010a_sold(me, qty, param), f"0x010a sold x{qty}")
                            else:
                                event(ev="shop", sid=sid, op=f"event{sev}", param=param)
                        elif cmd == 0x0106 and len(body) >= 0x54 and body[0x2D] == 6 and me is not None \
                                and state is not None and me.zone is not None:          # pick up a ground item
                            uuid, = struct.unpack_from("<I", body, 0x34)
                            g = drops.ground.get(me.zone, {}).get(uuid)
                            if g is None:
                                event(ev="pickup", sid=sid, uuid=f"{uuid:x}", error="gone")
                                continue
                            fail = 0
                            if not xgame.ground_ok(g, me.game):              # never shown to this disc (xgame.py)
                                event(ev="pickup", sid=sid, item=g["id"], error=f"not on {me.game}")
                                await send(drops.c0106_pickup(me, me, g, 1), "0x0106 pickup refused (other disc)")
                                continue
                            if g["meseta"]:
                                state["meseta"] = min(state.get("meseta", 0) + g["qty"], 99_999_999)
                            else:
                                _slot, err = items.add_item(state, g["id"], g["qty"], uuid=g["uuid"])
                                fail = 1 if err else 0
                            event(ev="pickup", sid=sid, item=g["id"], qty=g["qty"], fail=fail)
                            if fail:
                                await send(drops.c0106_pickup(me, me, g, fail), "0x0106 pickup failed")
                                continue
                            del drops.ground[me.zone][uuid]
                            save_state()
                            if g["meseta"]:
                                await send(combat.c0115(me, 0xFFFFFFFF), "0x0115 meseta (pickup)")
                            for d in [me] + WORLD.others(me):
                                if d is me:
                                    await send(drops.c0106_pickup(d, me, g), "0x0106 pickup")
                                elif xgame.ground_ok(g, d.game):
                                    WORLD.relay(d, drops.c0106_pickup(d, me, g), "0x0106 pickup")
                        elif cmd == 0x0A09 and len(body) >= 0x28 and state is not None and me is not None and body[0x24] == 1:
                            gslot, wslot = body[0x25], struct.unpack_from("<H", body, 0x26)[0]
                            inv_ = state.get("inv", [])
                            chance = (econ.grind_chance(inv_[gslot], inv_[wslot])
                                      if gslot < len(inv_) and wslot < len(inv_) else None)
                            event(ev="grind_quote", sid=sid, grinder=gslot, weapon=wslot, chance=chance)
                            await send(econ.c0a09_quote(gid, me.lid, body, chance), "0x0a09 grind rate")
                        elif (cmd == 0x0105 and len(body) >= 0x34 and state is not None and me is not None and body[0x2D] == 8
                              and any(x["uuid"] == struct.unpack_from("<I", body, 0x30)[0] and x["id"].startswith("0f01")
                                      for x in state.get("inv", []))):            # use a grinder on a weapon: grind
                            guuid, = struct.unpack_from("<I", body, 0x30)
                            grinder, weapon, ok, gidx, err = econ.grind(state, guuid, body[0x2F])
                            event(ev="grind", sid=sid, weapon=weapon and weapon["id"], ok=ok, error=err,
                                  grind=weapon and weapon.get("grind"))
                            if err:
                                continue
                            await send(econ.c0106_grind(gid, me.lid, gidx, body[0x2F], guuid, weapon, ok),
                                       f"0x0106 grind {'ok' if ok else 'failed'}")
                            removed, _e = items.remove_item(state, guuid, 1)
                            save_state()
                            if removed:
                                await send(items.c0106_remove(gid, me.lid, removed, 1), "0x0106 remove (grinder)")
                        elif (cmd == 0x0105 and len(body) >= 0x34 and state is not None and me is not None
                              and body[0x2D] == growth.LEARN):              # learn a PA disk (psu/growth.py)
                            pos, entry, dcat, name, err = growth.learn_disk(state, body)
                            event(ev="learn", sid=sid, item=name, pa=entry and entry[1], pos=pos, error=err,
                                  index=body[0x2C], uuid=f"{struct.unpack_from('<I', body, 0x30)[0]:x}")
                            if err:                                         # dropped by the client (+0x2E 0x24)
                                await send(items.c0105_reply(gid, me.lid, body), "0x0105 learn refused")
                                continue
                            save_state()
                            await send(growth.c0105_learn(gid, me.lid, body, pos, dcat, entry), "0x0105 learn")
                        elif cmd == 0x0105 and len(body) >= 0x34 and state is not None:
                            if body[0x2D] == 8 and me is not None:     # a used item may still be the attack id of hits
                                used_uuid, = struct.unpack_from("<I", body, 0x30)
                                used = next((x for x in state.get("inv", []) if x["uuid"] == used_uuid), None)
                                if used is not None:
                                    me.recent_use = {used_uuid: dict(used)}
                                    for w in items.pp_item(state, used, body[0x2F]) or []:   # Photon Charge (before the
                                        await send(items.c0105_pp(gid, me.lid, w), "0x0105 pp item")   # echo, 0x29708c)
                            ev_i, name = items.apply_event(state, body)
                            save_state()
                            event(ev="item", sid=sid, event_id=ev_i, item=name)
                            extra = b""
                            if ev_i == 1:                          # equip a weapon: its hitbox block (+0x3C)
                                eq_uuid, = struct.unpack_from("<I", body, 0x30)
                                extra = items.equip_block(next((x for x in state.get("inv", []) if x["uuid"] == eq_uuid), None))
                            await send(items.c0105_reply(gid, me.lid if me else 0, body, extra=extra), f"0x0105 item event {ev_i}")
                            eq_slot = next((x for x in state.get("inv", []) if x["uuid"] == struct.unpack_from("<I", body, 0x30)[0]), None)
                            if ev_i == 1 and eq_slot and items.db().get(eq_slot["id"], {}).get("cat") in (2, 8):
                                # the weapons' reach was computed before this reach factor arrived: re-confirm them
                                for wi, w in enumerate(state.get("inv", [])):
                                    if w.get("active") and items.db().get(w["id"], {}).get("cat") == 1:
                                        req = bytes(0x24) + struct.pack("<IIBBBBI", gid, me.lid if me else 0, wi, 1, 0, 0, w["uuid"])
                                        await send(items.c0105_reply(gid, me.lid if me else 0, req, extra=items.equip_block(w)),
                                                   "0x0105 weapon re-equip (reach)")
                        elif cmd == 0x0304 and len(body) >= 0x3C:
                            mods, msg = body[0x34:0x3C], body[0x3C:]
                            raw = load_characters(gid).get(slot) if selected else None
                            text = msg.decode("utf-16-le", "replace").split("\0")[0]
                            event(ev="chat", sid=sid, text=text)
                            if (text.strip() == "!clear" and mission is not None and mission.get("in")
                                    and gm.is_admin(dbm.get(STATE_DIR).username(gid))):   # GM: clear now
                                judge.run(me.uni, mission["qid"], mission.get("owner", gid))["started"] = True
                                await mission_check(force_clear=True)
                                continue
                            if text.strip().startswith("!") and me is not None:
                                await gm_command(text)
                                continue
                            if me is not None and gm.muted(gid):             # GM mute: not relayed
                                await send(gm.c0228_notice(me, gm.muted_text(gid), 3, 4), "0x0228 muted")
                                continue
                            if me is not None:        # the sender needs its own echo (no local log line)
                                channel = body[0x38] if len(body) > 0x38 else 0
                                if channel == 1 and me.party is not None:        # party chat: every member
                                    targets = [m for _, m in me.party.members()]
                                else:
                                    targets = [me] + WORLD.others(me)
                                for d in targets:
                                    data = c0304_chat(d, me, body)
                                    if d is me:
                                        await send(data, "0x0304 chat echo")
                                    else:
                                        WORLD.relay(d, data, "0x0304 chat")
                        elif cmd == 0x170B:
                            path = os.path.join(dialog.CAPTURE_DIR, "packet170c.bin")
                            event(ev="counter_bg_locations", sid=sid, have=os.path.exists(path))
                            if os.path.exists(path):
                                with open(path, "rb") as f:
                                    await send(packet(0x170C, struct.pack("<H", 0xFFFF) + bytes(18) + ACC + struct.pack("<I", gid)
                                                      + bytes(8) + f.read()), "0x170c bg locations")
                        elif cmd == 0x0C01 and len(body) >= 0x28 and me is not None:     # accept a mission
                            qid, = struct.unpack_from("<I", body, 0x24)
                            ok = missions.mission(qid) is not None
                            event(ev="mission_accept", sid=sid, quest=qid, ok=ok)
                            if ok and not missions.unlocked(state, qid):     # a story act not opened yet (greyed)
                                event(ev="mission_locked", sid=sid, quest=qid)
                                await send(missions.c0c02(gid, missions.PREREQUISITES), "0x0c02 refused (story order)")
                                continue
                            dropped = missions.start_run(state, qid) if ok else []
                            if dropped:
                                event(ev="story_run_flags", sid=sid, quest=qid, cleared=dropped)
                                save_state()
                            if ok:
                                party_ = me.party
                                owner = party_.slots[party_.leader].gid if party_ is not None else gid
                                mission = {"qid": qid, "origin": prev, "in": False, "start_pending": False, "owner": owner}
                                await send(missions.c1020(gid), "0x1020")
                                await send_paced(missions.c1015(gid, qid), f"0x1015 mission {qid}")
                                if party_ is not None and party_.slots[party_.leader] is me:
                                    party_.mission = {"qid": qid, "owner": owner}
                                    for _, m_ in party_.members():
                                        if m_ is not me and m_.zone == me.zone:     # members at the same counter
                                            if not missions.playable(qid, m_.game):   # other disc: refused
                                                WORLD.relay(m_, missions.c0c02(m_.gid, missions.OTHER_DISC),
                                                            "0x0c02 refused (mission on the other disc)")
                                                event(ev="mission_member_refused", sid=sid, quest=qid,
                                                      member=f"{m_.gid:#010x}", game=m_.game)
                                                continue
                                            WORLD.relay(m_, missions.c1020(m_.gid), "0x1020 (party)")
                                            WORLD.relay(m_, missions.c1015(m_.gid, qid, m_.game), f"0x1015 mission {qid} (party)")
                                            WORLD.relay(m_, missions.c0c02(m_.gid, 0), "0x0c02 accept (party)")
                            await send(missions.c0c02(gid, 0 if ok else 1), f"0x0c02 accept {'ok' if ok else 'rejected'}")
                        elif cmd in (0x0C11, 0x0C13) and len(body) >= 0x28:
                            # AotI story scripts' item queries, waited on with sync (no reply: -1 after the timeout,
                            # and STORY13_A's lw_Is_Item_Num_OK then sends the player out of the mission):
                            # q.seq.get_item_num (0x1c4430) sends 0x0C13 {+0x24 wait id, +0x28 item ids, +0x88 n};
                            # 0x0C14 (0x40b2c0) completes the wait, +0x28 != 0 = success. seq.get_item (0x40b380)
                            # sends 0x0C11 {+0x24 wait id, +0x28 arg}; 0x0C12 (0x40b310) returns +0x28 (< 0 = fail).
                            # EGS answers 0x0C11 with 0x0C12 {id, 1}; both get 1 here.
                            wait_id, = struct.unpack_from("<I", body, 0x24)
                            reply = cmd + 1
                            event(ev="seq_query", sid=sid, cmd=f"{cmd:#06x}", wait=wait_id)
                            await send(packet(reply, bytes(20) + ACC + struct.pack("<I", gid) + bytes(8)
                                              + struct.pack("<Ii", wait_id, 1)), f"{reply:#06x} seq query reply")
                        elif cmd == 0x0C0E and me is not None and mission is not None:    # cancel / abort
                            event(ev="mission_abort", sid=sid, quest=mission["qid"], inside=mission.get("in"))
                            await send(social.c1006_event(me, 12, 0), "0x1006 mission cancelled")
                            if me.party is not None and me.party.slots[me.party.leader] is me:
                                me.party.mission = None
                            back, inside, mission = mission["origin"], mission.get("in"), None
                            if inside and back is not None:
                                await go_zone(back[0], back[1])
                        elif cmd == 0x0808 and mission is not None and mission.get("in") and me.zone is not None:
                            # zone loaded: the join snapshot (conditions ready + flags), then the start once
                            for data, what in instance.snapshot(me, instance.get(me.zone)):
                                await send(data, what)
                            if drops.ground.get(me.zone):
                                await send(drops.c0a03(me, me.zone), "0x0a03 ground items")
                            if not mission.get("start_pending"):
                                continue
                            mission["start_pending"] = False
                            r_ = judge.run(me.uni, mission["qid"], mission.get("owner", gid))
                            if r_["started"]:                  # a member joining a running mission
                                mission["t0"] = r_["t0"]
                                await send(missions.c0c09(gid, 0, int(time.time() - r_["t0"]), lid=me.lid),
                                           "0x0c09 mission start (joined)")
                            else:
                                await mission_check()
                                mission["t0"] = r_["t0"] or time.time()
                        elif cmd == 0x0812:
                            raw = load_characters(gid).get(slot) if selected else None
                            event(ev="counter_leave", sid=sid, back_to=f"{prev[0]} entry {prev[1]}" if prev else None)
                            if raw and prev and available(*prev[0][:2]) and me is not None:
                                area, entry = prev
                                me.area, me.entry = area, entry
                                await enter_zone(*area[:2])
                                for data, what in with_players(area_sequence(gid, raw, area, entry, nb=areanb + 1,
                                                                             quest_change=True, lid=me.lid, st=state)):
                                    await send_paced(data, what)
                                areanb += 2
                                me.areanb = areanb
                                prev = None
                                await show_to_zone()
                        elif cmd == 0x1A01 and len(body) >= 0x30:
                            enabled = None
                            if state is not None:
                                enabled = progression.type_menu(state)
                            name, shop, replies = dialog.reply(gid, body, enabled=enabled)
                            if name == "lumilass" and me is not None:
                                replies = [(econ.c1a03(gid, me.lid, me.raw), "0x1a03 lumilass hair list")]
                            event(ev="dialog", sid=sid, kind=name, shop=shop, answered=len(replies))
                            for data, what in replies:
                                await send(data, what)
                        elif cmd == 0x0F0A and len(body) >= 0x64 and mission is not None and mission.get("in")                             and me.zone is not None:
                            desc, out = instance.object_event(instance.get(me.zone), body)
                            otype, oev = struct.unpack_from("<H", body, 0x34)[0], body[0x62]
                            if (otype, oev) == (50, 9) and state is not None:     # healing pad tick: +10 % max HP
                                mx = progression.derived(me.raw, state)["max_hp"]
                                if state.get("hp", mx) < mx:
                                    state["hp"] = min(mx, state.get("hp", mx) + mx // 10)
                                    save_state()
                                    await send(combat.c0117(me, state["hp"]), f"0x0117 hp {state['hp']}")
                                    await send(c0111_spawn(me, me, 4), "0x0111 healing aura")
                                    await party_hp(me)
                            event(ev="mission_object", sid=sid, what=desc, answered=[w for _, w in out])
                            for build, what in out:
                                await send(build(me), what)
                                for d in WORLD.others(me):
                                    WORLD.relay(d, build(d), what)
                            if out:
                                await mission_check()
                        elif cmd == 0x1A05 and len(body) >= 0x59 and me is not None and me.raw is not None:   # Lumilass
                            new_raw = econ.apply_lumilass(me.raw, body)
                            me.raw = new_raw
                            nm = new_raw[0x28:0x68].decode("utf-16-le", "replace").split("\0")[0]
                            dbm.get(STATE_DIR).save_character(gid, slot, nm, new_raw)
                            event(ev="lumilass", sid=sid, entry=struct.unpack_from("<I", body, 0x2C)[0])
                            for d in WORLD.others(me):             # others rebuild the model from a fresh 0x010D
                                WORLD.relay(d, world_c010d(d, me), "0x010d appearance")
                        elif cmd == 0x0F0A and len(body) >= 0x64:
                            desc, replies = dialog.object_event(gid, body)
                            event(ev="object", sid=sid, what=desc, answered=len(replies))
                            for data, what in replies:
                                await send(data, what)
                        # --- combat (psu/combat.py): conditions echoed to the zone, hits resolved here ---
                        elif cmd in (0x0402, 0x0403, 0x0404) and me is not None and me.zone is not None                             and len(body) >= (0x3C if cmd == 0x0402 else 0x2C):
                            inst = instance.get(me.zone)
                            cid, mode = struct.unpack_from("<II", body, 0x24)
                            event(ev="condition", sid=sid, cmd=f"{cmd:#06x}", id=cid, data=body[0x24:0x3C].hex())
                            if cmd == 0x0402:
                                build, what = (lambda d: combat.c1201(d, body)), "0x1201 condition"
                            else:
                                build = instance.flag(inst, cid, mode, b=cmd == 0x0403)
                                what = f"{'0x1203' if cmd == 0x0403 else '0x1205'} flag {cid} mode {mode}"
                            await send(build(me), what)
                            for d in WORLD.others(me):
                                WORLD.relay(d, build(d), what)
                            if cmd == 0x0402 and len(body) >= 0x34:           # an enemy reporting in: its status now
                                eid = cid + 150                     # condition id + 150 = the enemy's target id
                                ex, ey, ez = struct.unpack_from("<hhh", body, 0x28)   # its world position (s16)
                                if ex or ez:
                                    combat.positions[(me.zone, eid)] = struct.pack("<fff", ex, ey, ez)
                                enemies_ = combat.zone_enemies(me.zone)
                                if eid in combat.roster(me.zone) and eid not in enemies_:
                                    lvl_ = max(1, state["level"] if state else 1)
                                    en_ = enemies_[eid] = combat.new_enemy(me.zone, eid, lvl_)
                                    event(ev="enemy_status", sid=sid, enemy=eid, hp=en_.max_hp,
                                          state=struct.unpack_from("<I", body, 0x30)[0] & 0xFFFF)
                                    await send(combat.c0e08(me, en_), f"0x0e08 enemy {eid}")
                                    for d in WORLD.others(me):
                                        WORLD.relay(d, combat.c0e08(d, en_), f"0x0e08 enemy {eid}")
                            if cmd == 0x0402 and len(body) >= 0x34 and (struct.unpack_from("<I", body, 0x30)[0] & 3) == 3:
                                combat.roster(me.zone)                 # gateway condition id = gateway id - 150
                                qz = combat.zone_ids(me.zone)
                                for g in combat._gates.get(qz, []):
                                    if g["id"] - 150 == cid and g["flags"][0] >= 0:
                                        g.setdefault("cleared_sent", set())
                                        if me.zone in g["cleared_sent"]:
                                            break
                                        g["cleared_sent"].add(me.zone)
                                        fid = g["map_index"] * 256 + g["flags"][0]
                                        wb = instance.flag(inst, fid, 0)
                                        event(ev="wave_clear", sid=sid, gateway=g["id"], flag=fid, via="0x0402")
                                        await send(wb(me), f"0x1205 wave clear {fid}")
                                        for d in WORLD.others(me):
                                            WORLD.relay(d, wb(d), f"0x1205 wave clear {fid}")
                                        break
                            await mission_check()
                        elif cmd == 0x0E00 and len(body) >= 0x30 and me is not None and me.zone is not None:
                            players = [me] + WORLD.others(me)
                            known = set(combat.zone_enemies(me.zone))
                            level = max(1, state["level"] if state else 1)
                            entries, kills, hurt, grown = combat.resolve(me, players, body, level)
                            fresh = [e for i, e in combat.zone_enemies(me.zone).items() if i not in known]
                            event(ev="hits", sid=sid, n=len(entries), new=[e.id for e in fresh],
                                  kills=[e.id for e in kills], results=[(struct.unpack_from("<IIII", x, 4)[0],
                                                                         *struct.unpack_from("<II", x, 0x14))
                                                                        for x in entries])
                            for d in players:
                                out = [(combat.c0e08(d, e), f"0x0e08 enemy {e.id}") for e in fresh]
                                if entries:
                                    out.append((combat.c0e07(d, entries), f"0x0e07 hits ({len(entries)})"))
                                for data, what in out:
                                    if d is me:
                                        await send(data, what)
                                    else:
                                        WORLD.relay(d, data, what)
                            for p in hurt:
                                dbm.get(STATE_DIR).save_char_state(p.gid, getattr(p, "slot", 0), p.state)
                                await party_hp(p)
                            for p in set(getattr(combat.resolve, "buffed", [])):   # support techniques, statuses
                                watch_buffs(p)
                            for e in getattr(combat.resolve, "statused", []):      # enemies with a status
                                watch_enemy(me.zone, e)
                            for p, entry, up in grown:            # PA EXP / level (0x0108) to the zone
                                if up:
                                    dbm.get(STATE_DIR).save_char_state(p.gid, getattr(p, "slot", 0), p.state)
                                    event(ev="pa_level", sid=sid, gid=f"{p.gid:#x}", pa=entry[1], exp=entry[0])
                                for d in players:
                                    data = growth.c0108(d, p, entry, up)
                                    if d is me:
                                        await send(data, "0x0108 pa exp", quiet=not up)
                                    else:
                                        WORLD.relay(d, data, "0x0108 pa exp", quiet=True)
                            if kills:                         # a gateway whose enemies are all dead: its clear flag
                                inst = instance.get(me.zone)
                                for g, fid in combat.cleared_gateways(me.zone, combat.zone_enemies(me.zone)):
                                    build = instance.flag(inst, fid, 0)
                                    event(ev="wave_clear", sid=sid, gateway=g["id"], flag=fid)
                                    for d in players:
                                        if d is me:
                                            await send(build(d), f"0x1205 wave clear {fid}")
                                        else:
                                            WORLD.relay(d, build(d), f"0x1205 wave clear {fid}")
                            for tid, box, bpos in getattr(combat.resolve, "broken", []):   # boxes: break + drop
                                inst = instance.get(me.zone)
                                oid_ = box[3]                          # the map-local object id, not the target id
                                obj_state = inst.objects.get(oid_, 0) | 3
                                inst.objects[oid_] = obj_state
                                maps_ = inst.maps
                                map_id = maps_[box[0]] if box[0] < len(maps_) else 0
                                qz = combat.zone_ids(me.zone)
                                area_, zlevel = drops.zone_area(*qz, map_id=map_id)
                                dtype = box[1][2] if len(box[1]) > 3 and box[1][2] < 0x80 else -1
                                res = drops.roll_object(dtype, box[1][3], zlevel, area_) if dtype >= 0 else None
                                if len(box) > 4 and box[4]:            # the box's own spot (the hit pos is a local offset)
                                    bpos = struct.pack("<fff", *box[4])
                                g = drops.make(me.zone, res, map_id, bpos) if res else None
                                bev = struct.unpack_from("<h", box[1], 0x20)[0] if len(box[1]) >= 0x22 else -1
                                bflag = instance.flag(inst, box[0] * 256 + bev, 0) if bev >= 0 else None
                                event(ev="box", sid=sid, box=tid, drop=g and g["id"],
                                      flag=bev >= 0 and box[0] * 256 + bev or None)
                                for d in players:
                                    out_ = [(instance.c1213(d, oid_, obj_state), f"0x1213 box {oid_} broken")]
                                    if bflag is not None:           # params +0x20: event set on break (e.g. a wave trigger)
                                        out_.append((bflag(d), f"0x1205 box event {box[0] * 256 + bev}"))
                                    if g is not None and xgame.ground_ok(g, d.game):    # AotI-only: not to PSU
                                        out_.append((drops.c0a02(d, g), f"0x0a02 box drop {g['id']}"))
                                    for data, what in out_:
                                        if d is me:
                                            await send(data, what)
                                        else:
                                            WORLD.relay(d, data, what)
                            if kills and mission is not None and mission.get("in"):
                                judge.run(me.uni, mission["qid"], mission.get("owner", gid))["kills"] += len(kills)
                            for e in kills:                       # drops: rolled per kill, shared by the zone
                                if e.spec is not None:
                                    area_, _lvl = drops.zone_area(*combat.zone_ids(me.zone), map_id=e.spec["map"])
                                    res = drops.roll_enemy(e.spec["variant"], e.level, area_)
                                    for _ in range(events.drop_rolls(me.game) - 1):   # an event's extra rolls
                                        if res is not None:
                                            break
                                        res = drops.roll_enemy(e.spec["variant"], e.level, area_)
                                    if res is None:               # PSU_SEASON event items (season.py)
                                        res = seasons.event_drop(game=me.game)
                                    if res is not None and res[0] == "meseta":
                                        res = ("meseta", int(res[1] * events.meseta_mult(me.game)))
                                    where = combat.positions.get((me.zone, e.id), getattr(e, "killpos", bytes(12)))
                                    g = drops.make(me.zone, res, e.spec["map"], where) if res else None
                                    if g is not None:
                                        event(ev="drop", sid=sid, enemy=e.id, item=g["id"], qty=g["qty"])
                                        for d in players:
                                            if d is me:
                                                await send(drops.c0a02(d, g), f"0x0a02 drop {g['id']}")
                                            elif xgame.ground_ok(g, d.game):          # AotI-only: not to PSU
                                                WORLD.relay(d, drops.c0a02(d, g), f"0x0a02 drop {g['id']}")
                            for e in kills:
                                for pgid, amount in combat.kill_exp(e).items():
                                    p = next((x for x in players if x.gid == pgid), None)
                                    if p is None or not p.state or p.state.get("hp", 1) == 0:
                                        continue
                                    amount = int(amount * events.exp_mult(p.game))
                                    up = combat.add_exp(p, amount)
                                    dbm.get(STATE_DIR).save_char_state(p.gid, getattr(p, "slot", 0), p.state)
                                    event(ev="exp", sid=sid, gid=f"{p.gid:#x}", enemy=e.id, exp=amount,
                                          total=p.state["exp"], level=p.state["level"], level_up=up)
                                    if up:      # 0x010D alone, naming the enemy: the client's level-up (area.c010d_char)
                                        out = [(c010d_char(p.gid, p.raw, p.area, p.entry, lid=p.lid, st=p.state, game=p.game,
                                                           source=e.id), f"0x010d level {p.state['level']} exp +{amount}")]
                                    else:
                                        out = [(combat.c0115(p, e.id), f"0x0115 exp +{amount}")]
                                    for data, what in out:
                                        if p is me:
                                            await send(data, what)
                                        else:
                                            WORLD.relay(p, data, what)
                            if kills:
                                await mission_check()
                        # --- player to player (psu/social.py) ---
                        elif cmd == 0x100A and len(body) >= 0x68 and me is not None:
                            target, = struct.unpack_from("<I", body, 0x24)
                            other = WORLD.online.get(target)
                            event(ev="party_invite", sid=sid, to=f"{target:#x}", online=other is not None)
                            if other is None or (other.party is not None and len(other.party.members()) > 1):
                                await send(social.c100c_error(me, target, 4, body[0x28:0x68]), "0x100c invite failed")
                            else:
                                social.PENDING_INVITE[other.gid] = me
                                WORLD.relay(other, social.c100a_invite(other, me), "0x100a invite")
                        elif cmd == 0x100C and len(body) >= 0x78 and me is not None:
                            result = body[0x34]
                            inviter = social.PENDING_INVITE.pop(me.gid, None)
                            event(ev="party_answer", sid=sid, result=result, inviter=f"{inviter.gid:#x}" if inviter else None)
                            if inviter is not None and inviter.gid in WORLD.online:
                                WORLD.relay(inviter, social.c100c_result(inviter, body), "0x100c invite result")
                                if result == 1:
                                    await party_join(inviter, me)
                        elif cmd == 0x1002 and me is not None:
                            await party_leave(me)
                        elif cmd == 0x1007 and len(body) >= 0x28 and me is not None and me.party is not None:
                            slot_k = body[0x24]
                            victim = me.party.slots[slot_k] if slot_k < 6 else None
                            if me.party.slot_of(me) == me.party.leader and victim is not None and victim is not me:
                                await party_leave(victim, kicked=True)
                        elif cmd == 0x101D and len(body) >= 0x28 and me is not None:     # "receive invites" setting
                            me.invites = body[0x24]
                            await send(social.c101d_ack(me, body[0x24]), "0x101d ack")
                        elif cmd == 0x1013 and len(body) >= 0x28 and me is not None and me.party is not None:
                            new_leader = body[0x24]
                            party_ = me.party
                            if party_.slot_of(me) == party_.leader and new_leader < 6 and party_.slots[new_leader] is not None:
                                party_.leader = new_leader
                                event(ev="party", sid=sid, op="leader", slot=new_leader)
                                for _, m in party_.members():
                                    data = social.c1006_event(m, 7, new_leader)
                                    if m is me:
                                        await send(data, "0x1006 new leader")
                                    else:
                                        WORLD.relay(m, data, "0x1006 new leader")
                        elif cmd == 0x1515 and len(body) >= 0x28 and me is not None:          # partner card: status
                            owner, = struct.unpack_from("<I", body, 0x24)
                            other = WORLD.online.get(owner)
                            event(ev="card_status", sid=sid, owner=f"{owner:#x}", online=other is not None)
                            presence.viewed(me, owner)
                            if other is not None and other.raw is not None:
                                uname = dict(UNIVERSES).get(other.uni, "")
                                await send(social.c1516_card_info(me, other, uname), "0x1516 card holder status")
                        elif cmd == 0x0302 and len(body) >= 0xCC and state is not None:        # chat shortcut
                            idx, = struct.unpack_from("<I", body, 0x24)
                            if idx < 12:
                                state.setdefault("shortcuts", {})[str(idx)] = body[0x28:0xCC].hex()
                                save_state()
                        elif cmd == 0x1514 and len(body) >= 0x28 and me is not None:
                            target, = struct.unpack_from("<I", body, 0x24)
                            other = WORLD.online.get(target)
                            event(ev="card_send", sid=sid, to=f"{target:#x}", online=other is not None)
                            if other is not None:
                                social.PENDING_CARD[other.gid] = me
                                WORLD.relay(other, social.c1503_card(other, me), "0x1503 card offered")
                        elif cmd == 0x1504 and len(body) >= 0x27 and me is not None:
                            want_slot, accept = struct.unpack_from("<hB", body, 0x24)
                            sender = social.PENDING_CARD.pop(me.gid, None)
                            event(ev="card_answer", sid=sid, accept=accept, sender=f"{sender.gid:#x}" if sender else None)
                            if accept == 1 and sender is not None:
                                card_slot = want_slot if want_slot >= 0 else social.free_card_slot(me.gid)
                                if card_slot is not None:
                                    rec = social.card_record(sender, game=me.game)
                                    social.store_card(me.gid, card_slot, rec)
                                    await send(social.c1509_slot(me, card_slot, rec), f"0x1509 card slot {card_slot}")
                        elif cmd == 0x1508 and len(body) >= 0x28 + 0x1D4:
                            card_slot, = struct.unpack_from("<h", body, 0x24)
                            if card_slot >= 0:
                                social.store_card(gid, card_slot, body[0x28:0x28 + 0x1D4])
                            event(ev="card_save", sid=sid, slot=card_slot, valid=body[0x28 + 0x1C8])
                        elif cmd == 0x1506 and me is not None:
                            await send(social.c1507_status(me, WORLD.online), "0x1507 card status")
                        elif cmd == 0x150B and len(body) >= 0x1D4 and me is not None:
                            rec = body[0x24:0x24 + 0x1AC]
                            n, = struct.unpack_from("<I", body, 0x1D0)
                            rcpts = struct.unpack_from(f"<{min(n, 32)}I", body, 0x1D4) if len(body) >= 0x1D4 + 4 * min(n, 32) else ()
                            event(ev="mail", sid=sid, to=[f"{r:#x}" for r in rcpts])
                            for r in rcpts:
                                social.store_mail(r, rec)
                                other = WORLD.online.get(r)
                                if other is not None:
                                    WORLD.relay(other, social.c150f_mail(other, [rec]), "0x150f mail")
                        elif cmd == 0x150D and len(body) >= 0x2C:
                            sender_gid, serial = struct.unpack_from("<II", body, 0x24)
                            social.mark_read(gid, sender_gid, serial)
                            if me is not None:
                                await send(social.c150e_read(me, sender_gid, serial), "0x150e read")
                        elif cmd == 0x1513 and len(body) >= 0x70:
                            op, who = social.blacklist_update(gid, body)
                            event(ev="blacklist", sid=sid, op=op, who=f"{who:#x}")
                        elif cmd == 0x1901 and len(body) >= 0x2A and me is not None:     # trade (psu/social.py)
                            partner, = struct.unpack_from("<I", body, 0x24)
                            action, param = body[0x28], body[0x29]
                            other = WORLD.online.get(partner)
                            t = social.TRADES.get(me.gid)
                            event(ev="trade", sid=sid, action=action, param=param, partner=f"{partner:#x}",
                                  online=other is not None, open=t is not None)
                            if other is None:
                                if t is not None:
                                    social.trade_end(t)
                                continue

                            async def tell(d, a2, p2, peer):
                                data, what = social.c1902_state(d, peer, a2, p2), f"0x1902 trade ({a2}, {p2})"
                                if d is me:
                                    await send(data, what)
                                else:
                                    WORLD.relay(d, data, what)

                            if action == 1:                                   # request
                                social.TRADE_ASKED[other.gid] = me.gid
                                WORLD.relay(other, social.c1901_request(other, me.gid), "0x1901 trade request")
                            elif action == 2 and social.TRADE_ASKED.get(me.gid) == other.gid:   # the asked side accepts
                                del social.TRADE_ASKED[me.gid]
                                t = social.Trade(other.gid, me.gid)
                                social.TRADES[me.gid] = social.TRADES[other.gid] = t
                                await tell(other, 1, 1, me.gid)
                            elif action == 2 and t is not None and t.a == me.gid:    # requester confirms: its window
                                await tell(me, 2, 1, other.gid)
                            elif action == 8:                                 # decline / busy
                                social.TRADE_ASKED.pop(me.gid, None)
                                await tell(other, 1, 8, me.gid)
                                if t is not None:
                                    social.trade_end(t)
                            elif action == 9 and param == 6 and t is not None:   # "no" at the final dialog
                                t.confirmed.clear()
                                t.final.clear()
                                await tell(other, 5, 3, me.gid)
                            elif action == 9:                                 # cancel
                                social.TRADE_ASKED.pop(me.gid, None)
                                social.TRADE_ASKED.pop(other.gid, None)
                                await tell(other, 0, 10, me.gid)
                                if t is not None:
                                    social.trade_end(t)
                            elif action == 4 and t is not None:               # items confirmed
                                t.confirmed.add(me.gid)
                                if len(t.confirmed) == 2:
                                    await tell(me, 4, 1, other.gid)
                                    await tell(other, 4, 1, me.gid)
                                else:
                                    await tell(other, 4, 5, me.gid)
                            elif action == 6 and t is not None:               # final OK
                                t.final.add(me.gid)
                                if len(t.final) < 2:
                                    await tell(other, 5, 5, me.gid)
                                    continue
                                pa, pb = (me, other) if t.a == me.gid else (other, me)
                                changes = []
                                err = (xgame.trade_error(t, {me.gid: me, other.gid: other})
                                       or (social.trade_swap(t, pa.state, pb.state, changes) if pa.state and pb.state else "no state"))
                                event(ev="trade_done", sid=sid, a=f"{t.a:#x}", b=f"{t.b:#x}", error=err,
                                      items={f"{g:#x}": {f"{u:x}": n for u, n in v.items()} for g, v in t.items.items()},
                                      meseta={f"{g:#x}": m for g, m in t.meseta.items()})
                                social.trade_end(t)
                                for p in (me, other):
                                    await tell(p, 6 if err is None else 4, 1 if err is None else 6,
                                               other.gid if p is me else me.gid)
                                if err is None:
                                    for p in (me, other):
                                        dbm.get(STATE_DIR).save_char_state(p.gid, getattr(p, "slot", 0), p.state)
                                        out = [(items.c0106_add(p.gid, p.lid, sl, game=p.game) if kind == "add"
                                                else items.c0106_remove(p.gid, p.lid, sl, n, game=p.game), f"0x0106 {kind} (trade)")
                                               for g2, kind, sl, n in changes if g2 == p.gid]
                                        out.append((combat.c0115(p, 0xFFFFFFFF), "0x0115 meseta (trade)"))
                                        for data, what in out:
                                            if p is me:
                                                await send(data, what)
                                            else:
                                                WORLD.relay(p, data, what)
                        elif cmd == 0x1903 and len(body) >= 0x38 and me is not None:
                            _snd, partner, uuid, count = struct.unpack_from("<IIII", body, 0x24)
                            op = body[0x34]
                            other = WORLD.online.get(partner)
                            t = social.TRADES.get(me.gid)
                            event(ev="trade_item", sid=sid, partner=f"{partner:#x}", op=op, uuid=f"{uuid:x}", count=count)
                            if other is None or t is None or state is None:
                                continue
                            new, trec = False, b""
                            if op == 3:
                                t.meseta[me.gid] = min(count, state.get("meseta", 0))
                            else:
                                slot_ = next((x for x in state.get("inv", []) if x["uuid"] == uuid), None)
                                if slot_ is None:
                                    continue
                                if op == 1 and not xgame.item_ok(slot_["id"], other.game):
                                    # the partner's disc has no such item: take it back out of the sender's window
                                    event(ev="trade_item", sid=sid, refused=slot_["id"], partner_game=other.game)
                                    back = bytearray(body)
                                    back[0x34] = 2
                                    await send(social.c1904_item(me, bytes(back)), "0x1904 trade item refused (other disc)")
                                    continue
                                if op == 1:
                                    new = uuid not in t.items[me.gid]
                                    t.items[me.gid][uuid] = min(count, slot_["qty"])
                                    trec = social.trade_record(slot_, t.items[me.gid][uuid], other.game)
                                else:
                                    t.items[me.gid].pop(uuid, None)
                            await send(social.c1904_item(me, body, new), "0x1904 trade item (echo)")
                            WORLD.relay(other, social.c1904_item(other, body, new, trec), "0x1904 trade item")
                            if t.confirmed:                                   # an offer changed: un-confirm both
                                t.confirmed.clear()
                                await send(social.c1902_state(me, other.gid, 4, 3), "0x1902 trade (4, 3)")
                                WORLD.relay(other, social.c1902_state(other, me.gid, 4, 3), "0x1902 trade (4, 3)")
                        elif cmd == 0x0A07 and len(body) >= 0x28 and me is not None:
                            target, = struct.unpack_from("<I", body, 0x24)
                            event(ev="view_stats", sid=sid, target=f"{target:#x}")
                            await send(social.c0a07_view(me, target), "0x0a07 view (empty)")
                        elif cmd == 0x021D:
                            event(ev="unicube", sid=sid)
                            await send(c021e_universe_list(counts=WORLD.population()), "0x021e universe list")
                        elif cmd == 0x021F and len(body) >= 0x2C:
                            uni, entry = struct.unpack_from("<II", body, 0x24)
                            raw = load_characters(gid).get(slot) if selected else None
                            event(ev="unicube_select", sid=sid, uni=f"{uni:#x}", entry=entry)
                            if uni and raw and uni in dict(UNIVERSES):
                                if me is not None:
                                    if me.zone is not None:
                                        await leave_zone()
                                    me.uni = uni                  # the 0x021C that follows enters the new universe
                                await send(c0230_leave_area(gid), "0x0230")
                                for data, what in load_sequence(raw):
                                    await send_paced(data, what)
                        elif cmd == 0x0D02 and len(body) >= 0x6A:
                            slot, name, race = save_character(gid, body)
                            event(ev="character", sid=sid, gid=f"{gid:#010x}", slot=slot, name=name, race=race)
                    except (ConnectionError, OSError):
                        raise
                    except Exception as e:      # one bad packet or handler bug must not drop the player
                        errors += 1
                        event(ev="error", sid=sid, cmd=f"{cmd:#06x}", error=repr(e)[:300],
                              where=traceback.format_exc().strip().splitlines()[-3][:200])
                        log.write(f"{now().isoformat()} handler error {cmd:#06x}\n{traceback.format_exc()}\n")
                        if errors > 50:         # a client that keeps tripping handlers is dropped
                            return
                # once authed the client may sit silent (standing still); the 5 s keepalive write
                # notices a dead peer, so only unauthenticated connections time out
                data = await read_some(None if authed else IDLE, jobs_too=True)
                if data is None:            # woken for work queued by another session (GM summon, psu/gm.py)
                    wake.clear()
                    while jobs:
                        await jobs.pop(0)()
                    continue
                if not data:
                    break
                record("C>S", data)
                buf += data
        except asyncio.TimeoutError:
            log.write(f"{now().isoformat()} idle timeout\n")
        except ConnectionError as e:
            log.write(f"{now().isoformat()} {e!r}\n")
        finally:
            if state is not None:                   # play time up to the logout
                try:
                    progression.stop_clock(state)
                    save_state()
                except Exception as e:
                    log.write(f"{now().isoformat()} play time save failed: {e!r}\n")
            if me is not None:
                WORLD.online.pop(me.gid, None)
                presence.offline(me)
                t = social.TRADES.get(me.gid)
                if t is not None:                   # the partner's window: cancelled
                    social.trade_end(t)
                    o = WORLD.online.get(t.other(me.gid))
                    if o is not None:
                        WORLD.relay(o, social.c1902_state(o, me.gid, 0, 10), "0x1902 trade cancelled (left)")
                await party_leave(me, kicked=False)
            if me is not None and me.zone is not None:
                for d in WORLD.leave(me):
                    WORLD.relay(d, c0204_unspawn(d, me), "0x0204 unspawn")
            if ka:
                ka.cancel()
            if pending_read is not None:
                pending_read.cancel()
            writer.close()
            event(ev="close", sid=sid, authed=authed)


STARTED = time.time()


def status():
    """What status.json holds: players online per universe and per area, uptime."""
    per_uni, per_area = {}, {}
    for o in list(WORLD.online.values()):
        per_uni[str(o.uni)] = per_uni.get(str(o.uni), 0) + 1
        key = f"{o.area[0]}/{o.area[1]}" if o.area else "-"
        per_area[key] = per_area.get(key, 0) + 1
    return {"time": now().isoformat(), "uptime_s": int(time.time() - STARTED), "online": len(WORLD.online),
            "universes": per_uni, "areas": per_area, "zones": len(WORLD.zones)}


async def status_writer(every=30):
    """Writes $PSU_LOG_DIR/status.json every `every` s (atomic replace), for monitoring."""
    path = os.path.join(LOG_DIR, "status.json")
    while True:
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(status(), f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
        await asyncio.sleep(every)


async def board_writer():
    """Writes the public board file (psu/board.py) every BOARD_SECS for the PlayOnline boards service."""
    while True:
        try:
            board.write(board.snapshot(dbm.get(STATE_DIR), WORLD, STARTED, events=events.board_rows()))
        except Exception as e:                      # a board must never take the game down
            print(f"game: board: {e!r}", file=sys.stderr, flush=True)
        await asyncio.sleep(board.BOARD_SECS)


async def main():
    os.makedirs(os.path.join(LOG_DIR, "sessions"), exist_ok=True)
    asyncio.get_running_loop().create_task(status_writer())
    asyncio.get_running_loop().create_task(board_writer())
    asyncio.get_running_loop().create_task(events.watch(lambda text: gm.broadcast(text, None, gm.TICKER, 0, "events", "calendar")))
    progression.migrate_db(dbm.get(STATE_DIR), STATE_DIR,               # one-time retail type EXP curve
                           log=lambda m: print(f"game: {m}", file=sys.stderr, flush=True))
    await admin.start(STATE_DIR)                    # loopback admin channel + news rotation (psu/admin.py)
    adminport.start(lambda: board.snapshot(dbm.get(STATE_DIR), WORLD, STARTED,   # an OpenLobby panel
                                           events=events.board_rows()))      # (psu/adminport.py)
    servers = []
    for host, port in LISTEN:
        servers.append(await asyncio.start_server(lambda r, w, p=port: session(r, w, p), host, port))
        print(f"game: listening on {host}:{port}", file=sys.stderr, flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
