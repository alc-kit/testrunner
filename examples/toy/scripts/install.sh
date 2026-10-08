#!/usr/bin/env bash
# Asks like a destructive playbook gate does, in colour, and refuses anything but YES.
set -euo pipefail
sandbox="$1"
printf '\033[1;33mAbout to install into %s.\033[0m\n' "$sandbox"
printf '\033[1mType \033[31mYES\033[0;1m to continue:\033[0m '
read -r answer
if [ "$answer" != YES ]; then
  echo "refused: answer was '$answer'"
  exit 3
fi
mkdir -p "$sandbox"
echo installed > "$sandbox/service"
printf '\033[32minstalled\033[0m\n'
