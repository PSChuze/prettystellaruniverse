"""Partner card presence: online flags and "currently in" kept live for the players holding a card.

Both discs, statically read (PSU_US.erx PSU / AotI):
  S>C 0x1507 (PSU 0x287610 / AotI 0x289ed0): copies 200 bytes from +0x24 into the card list's status array
      ([0x6300bc] + 0x1F8 on PSU), one byte per card slot, 1 = online. Nothing else happens in the handler, so
      it can be sent unasked: whenever a card's owner logs in or out, every online player holding that card
      gets a fresh 0x1507 (C>S 0x1506 still asks for it when the list opens).
  S>C 0x1516 (PSU 0x39b6c0 / AotI 0x3993c0): the card holder status window (universe + location line). The
      handler drops it unless that window is open on the same owner (+0x24 against the selected card's +0x48),
      so a refresh sent on every zone change to the players who asked for it (C>S 0x1515) in the last
      VIEW_SECS is harmless when the window has closed and updates it while it is open.
  S>C 0x1510 / 0x1511 have empty handlers on both discs (jr ra): no push channel there.
"""
import time

from psu import social
from psu.observe import event
from psu.world import WORLD

VIEW_SECS = 300
VIEWERS = {}                         # card owner gid -> {viewer gid: time of the last 0x1515}


def holders(gid):
    """Online players (other than gid) with a partner card whose owner is gid."""
    out = []
    for o in list(WORLD.online.values()):
        if o.gid == gid:
            continue
        try:
            recs = social.cards(o.gid).values()
        except Exception:            # noqa: BLE001  a broken DB row must not stop a login
            continue
        if any(len(r) >= 0x4C and int.from_bytes(r[0x48:0x4C], "little") == gid for r in recs):
            out.append(o)
    return out


def push_status(p, why):
    hs = holders(p.gid)
    for h in hs:
        WORLD.relay(h, social.c1507_status(h, WORLD.online), f"0x1507 card status ({why})")
    if hs:
        event(ev="card_presence", gid=f"{p.gid:#x}", why=why, to=len(hs))
    return hs


def online(p):
    """p is in WORLD.online now."""
    return push_status(p, "online")


def offline(p):
    """p has left WORLD.online."""
    VIEWERS.pop(p.gid, None)
    for owners in VIEWERS.values():
        owners.pop(p.gid, None)
    return push_status(p, "offline")


def viewed(viewer, owner_gid):
    VIEWERS.setdefault(owner_gid, {})[viewer.gid] = time.time()


def moved(p):
    """p changed zone: refresh the card holder windows that asked about p lately."""
    from psu.charload import UNIVERSES
    watchers = VIEWERS.get(p.gid)
    if not watchers or p.raw is None:
        return []
    now_ = time.time()
    sent = []
    for vg, t in list(watchers.items()):
        v = WORLD.online.get(vg)
        if v is None or now_ - t > VIEW_SECS:
            watchers.pop(vg, None)
            continue
        WORLD.relay(v, social.c1516_card_info(v, p, dict(UNIVERSES).get(p.uni, "")), "0x1516 card holder moved")
        sent.append(v)
    if not watchers:
        VIEWERS.pop(p.gid, None)
    return sent
