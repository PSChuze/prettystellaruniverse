#!/bin/sh
# The disc's offline lobby zones under the online names the server looks up (lobbies.py has the details):
#   sh native-lobby-zones.sh psu|aoti <unpacked lobby AFS dir> <out dir>
exec python3 "$(dirname "$0")/lobbies.py" "$@"
