#!/usr/bin/env bash
# Lists the TCP and UDP ports that accept connections from the network, ignoring
# loopback-only listeners. Run it before and after harden.sh to see what changed.
#
#   scripts/audit-ports.sh            human-readable
#   scripts/audit-ports.sh --count    just the number
set -euo pipefail

ports=$(ss -H -lntu 2>/dev/null | awk '
  {
    addr = $5
    if (addr ~ /^127\./ || addr ~ /^\[::1\]/ || addr ~ /^::1/) next   # loopback only
    n = split(addr, parts, ":")
    port = parts[n]
    print $1 "/" port
  }' | sort -u)

if [ -z "$ports" ]; then count=0; else count=$(printf '%s\n' "$ports" | wc -l | tr -d ' '); fi

if [ "${1:-}" = "--count" ]; then
  echo "$count"
else
  echo "Ports listening on a non-loopback address:"
  [ -n "$ports" ] && printf '  %s\n' $ports
  echo "total: $count"
fi
