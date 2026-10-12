#!/usr/bin/env bash
# Placeholder for YOUR provisioning: boot a rescue system / PXE, partition, install, reboot.
# It keeps the one thing that matters for a destructive step: it asks before it wipes.
set -euo pipefail
host="$1" node="$2"
read -r -p "type the node name ($node) to wipe it: " answer
[ "$answer" = "$node" ] || { echo "not confirmed; nothing touched"; exit 3; }
echo "reinstalling $host ..."
ssh -o BatchMode=yes "root@$host" "touch /tmp/reinstalled"   # your installer goes here
