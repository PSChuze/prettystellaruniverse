#!/bin/sh
# Build the Colony lobby quest and zone NBLs with EGS's own code (github.com/essen/egs, AGPL-3.0), for the PS2.
# Needs erl, erlc and gcc (the erlang:26 image has them). python -m extract runs this; by hand:
#   EGS=<egs source> OUT=<dir> sh egs-build.sh
# The EGS tree is copied to a scratch dir and patched there, so the downloaded source stays as fetched.
set -eu
: "${EGS:?EGS=<egs source dir>}" "${OUT:?OUT=<output dir>}"
TMP="${TMP:-/tmp/egs-build}"
rm -rf "$TMP" && mkdir -p "$TMP" && cp -r "$EGS" "$TMP/egs" && cd "$TMP/egs"
# The PS2 client's chunk format is NSR/NSIF where the PC (and EGS) use NXR/NXIF
# (0x95c0 walks for the NSR chunk up to NEND; file-type table at 0x6c20).
sed -i 's/\$N, \$X, \$R, 0/$N, $S, $R, 0/g; s/\$N, \$X, \$I, \$F/$N, $S, $I, $F/g' apps/egs/src/egs_files.erl
grep -c '\$N, \$S, \$R' apps/egs/src/egs_files.erl
# ...and the PS2 asks the quest NBL for "quest.snr" (0x227850/0x227d60), not "quest.xnr"
sed -i 's/"quest\.xnr"/"quest.snr"/g' apps/egs/src/egs_quests_db.erl
grep -c 'quest.snr' apps/egs/src/egs_quests_db.erl
# Job Counter (set object 47, TObjColliJobCounter): the only way to open the types window in its
# "change type" mode. The native Colony has it in map 5 at (0, 10, 33); EGS's Colony has none.
# Native params end in 0a 10 08 00 where EGS writes ffffffff.
sed -i 's/<< TalkRadius:32\/little-float, 0:384, 16#ffffffff:32 >>/<< TalkRadius:32\/little-float, 0:384, 16#0a100800:32 >>/' apps/egs/src/egs_files.erl
grep -c '16#0a100800' apps/egs/src/egs_files.erl
awk -v obj='		{type_counter, {0.0, 10.0, 33.0}, {0.0, 0.0, 0.0}, [{talk_radius, 20.0}]},' \
  'BEGIN{m=0} {print} /^[{][{]map, 5[}]/{m=1; next} m==1 && /Always available/{print obj; m=2}' \
  priv/quests/1100000/zone-0/set_r0.conf > "$TMP/set_r0.conf" && mv "$TMP/set_r0.conf" priv/quests/1100000/zone-0/set_r0.conf
grep -c 'type_counter' priv/quests/1100000/zone-0/set_r0.conf
mkdir -p "$OUT" "$TMP/ebin" apps/prs/ebin apps/prs/priv
ERTS_INC=$(erl -noshell -eval 'io:format("~s/usr/include",[code:root_dir()]),halt().')
gcc -shared -fPIC -O2 -I"$ERTS_INC" apps/prs/c_src/*.c -o apps/prs/priv/prs_drv.so
printf '{application,prs,[{modules,[prs]}]}.\n' > apps/prs/ebin/prs.app
erlc -o apps/prs/ebin apps/prs/src/prs.erl
for m in egs_files egs_script_lexer egs_script_parser egs_script_compiler egs_quests_db; do
  erlc -I apps/egs/include -I apps/egs -o "$TMP/ebin" apps/egs/src/$m.erl
done
run() {
  OUT="$OUT" ERL_LIBS=$(pwd)/apps erl -noshell -pa "$TMP/ebin" -pa apps/prs/ebin -eval "$1"
}
# The Colony zone first, from the quest as EGS lays it out: zone_nbl reads the quest settings quest_nbl caches,
# and with the shop maps moved into zone 0 (below) it has no set data for them.
run '
    {ok, _} = egs_quests_db:start_link(),
    _ = egs_quests_db:quest_nbl(1100000),
    Z = egs_quests_db:zone_nbl(1100000, 0),
    ok = file:write_file(os:getenv("OUT") ++ "/zone-1100000-0.nbl", Z),
    io:format("zone 1100000/0 ~b bytes~n", [byte_size(Z)]),
    halt().'
# Colony: the server serves the native zone (maps 1-5, 100-103) as zone 0, so the shop maps join zone 0 and
# the shop warps (EGS: zones 11-13) point at zone 0; otherwise the shop exits have no warp (stuck inside).
sed -i 's/{maps, \[1, 2, 3, 4, 5, 103\]}/{maps, [1, 2, 3, 4, 5, 100, 101, 102, 103]}/; s/{1100000, 1[123], \(10[0-2]\)/{1100000, 0, \1/g' priv/quests/1100000/quest.conf
grep -c '{1100000, 0, 10[0-2]' priv/quests/1100000/quest.conf
# Planets: the same (native zone maps as zone 0; EGS's zones 11-13 hold the shop and side maps).
sed -i 's/{ 0, \[{areaid, 3}, {enemy_level, 1}, {sets, \[100\]}, {maps, \[1, 2, 3\]}]}/{ 0, [{areaid, 3}, {enemy_level, 1}, {sets, [100]}, {maps, [1, 2, 3, 4, 100, 200, 400]}]}/; s/{1101000, 1[123], /{1101000, 0, /g' priv/quests/1101000/quest.conf
sed -i 's/{ 0, \[{areaid, 4}, {enemy_level, 1}, {sets, \[100\]}, {maps, \[1\]}]}/{ 0, [{areaid, 4}, {enemy_level, 1}, {sets, [100]}, {maps, [1, 3, 100, 200]}]}/; s/{1102000, 1[123], /{1102000, 0, /g' priv/quests/1102000/quest.conf
sed -i 's/{ 0, \[{areaid, 5}, {enemy_level, 1}, {sets, \[100\]}, {maps, \[1\]}]}/{ 0, [{areaid, 5}, {enemy_level, 1}, {sets, [100]}, {maps, [1, 2, 100, 101, 102, 200]}]}/; s/{1103000, 1[123], /{1103000, 0, /g' priv/quests/1103000/quest.conf
grep -h '{ 0, \[{areaid' priv/quests/110[123]000/quest.conf
run '
    {ok, _} = egs_quests_db:start_link(),
    Out = os:getenv("OUT"),
    lists:foreach(fun(QID) ->
        Q = egs_quests_db:quest_nbl(QID),
        ok = file:write_file(Out ++ lists:flatten(io_lib:format("/quest-~b.nbl", [QID])), Q),
        io:format("quest ~b ~b bytes~n", [QID, byte_size(Q)])
    end, [0, 1100000, 1101000, 1102000, 1103000, 1104000]),
    halt().'
rm -rf "$TMP"
