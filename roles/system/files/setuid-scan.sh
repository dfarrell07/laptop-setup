#!/bin/bash
# Ansible managed — do not edit
# Weekly setuid/setgid binary scan.
# Compares current perm /6000 binaries against a stored baseline and logs
# deviations to journald (SyslogIdentifier=setuid-scan via systemd service).
# Baseline is created on first run; subsequent runs diff against it, and the
# baseline is rolled forward after each reported deviation.
set -euo pipefail

BASELINE_DIR=/var/lib/setuid-scan
BASELINE=${BASELINE_DIR}/baseline
CURRENT=$(mktemp)
TMP_BASELINE=
trap 'rm -f "$CURRENT" "$TMP_BASELINE"' EXIT

mkdir -p "$BASELINE_DIR"
chmod 700 "$BASELINE_DIR"

# Replace the baseline atomically so an interrupted write cannot leave a truncated file.
write_baseline() {
  TMP_BASELINE=$(mktemp "${BASELINE}.XXXXXX")
  cp "$CURRENT" "$TMP_BASELINE"
  mv -f "$TMP_BASELINE" "$BASELINE"
  TMP_BASELINE=
}

# Scan every btrfs/ext4/xfs filesystem: each btrfs subvolume (e.g. /home) is its own device, so
# a single "find / -xdev" never descends into them. -xdev still keeps out /proc, /sys, /dev.
# find exits 1 on any unreadable or vanished entry; that must not abort the scan (pipefail).
# -l (not -r): raw mode hex-escapes spaces (\x20), which find then cannot open.
mapfile -t ROOTS < <(findmnt -lno TARGET -t btrfs,ext4,xfs | sort -u)
[[ ${#ROOTS[@]} -gt 0 ]] || ROOTS=(/)
# -print0 + tr: a newline in a file name becomes '?', so names cannot forge extra log lines.
{ find "${ROOTS[@]}" -xdev -perm /6000 -type f -print0 2>/dev/null || true; } | tr '\0\n' '\n?' | sort -u > "$CURRENT"
COUNT=$(wc -l < "$CURRENT")

if [[ ! -f "$BASELINE" ]]; then
  write_baseline
  echo "setuid-scan: baseline created — ${COUNT} setuid/setgid binaries recorded"
  exit 0
fi

DIFF=$(diff "$BASELINE" "$CURRENT" || true)
if [[ -n "$DIFF" ]]; then
  echo "setuid-scan: DEVIATION — setuid/setgid binary list changed (${COUNT} current vs $(wc -l < "$BASELINE") baseline):"
  echo "$DIFF"
  # Update baseline so the next run measures from the new state
  write_baseline
else
  echo "setuid-scan: OK — ${COUNT} setuid/setgid binaries match baseline"
fi
