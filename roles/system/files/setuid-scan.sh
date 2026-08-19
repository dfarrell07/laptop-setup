#!/bin/bash
# Ansible managed — do not edit
# Weekly setuid/setgid binary scan.
# Compares current perm /6000 binaries against a stored baseline and logs
# deviations to journald (SyslogIdentifier=setuid-scan via systemd service).
# Baseline is created on first run; subsequent runs diff against it.
set -euo pipefail

BASELINE_DIR=/var/lib/setuid-scan
BASELINE=${BASELINE_DIR}/baseline
CURRENT=$(mktemp)
trap 'rm -f "$CURRENT"' EXIT

mkdir -p "$BASELINE_DIR"
chmod 700 "$BASELINE_DIR"

# Scan all local filesystems; -xdev prevents crossing into /proc /sys /dev etc.
find / -xdev -perm /6000 -type f 2>/dev/null | sort > "$CURRENT"
COUNT=$(wc -l < "$CURRENT")

if [[ ! -f "$BASELINE" ]]; then
  cp "$CURRENT" "$BASELINE"
  chmod 600 "$BASELINE"
  echo "setuid-scan: baseline created — ${COUNT} setuid/setgid binaries recorded"
  exit 0
fi

DIFF=$(diff "$BASELINE" "$CURRENT" || true)
if [[ -n "$DIFF" ]]; then
  echo "setuid-scan: DEVIATION — setuid/setgid binary list changed (${COUNT} current vs $(wc -l < "$BASELINE") baseline):"
  echo "$DIFF"
  # Update baseline so the next run measures from the new state
  cp "$CURRENT" "$BASELINE"
  chmod 600 "$BASELINE"
else
  echo "setuid-scan: OK — ${COUNT} setuid/setgid binaries match baseline"
fi
