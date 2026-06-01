#!/bin/bash
# smoke-test.sh — post-provisioning verification for workstation Ansible playbook
# Usage: smoke-test.sh [--json] [--user-only] [--container <name>]
# Exit: 0 = all pass, 1 = any failures
set -euo pipefail

JSON=false USER_ONLY=false CONTAINER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)      JSON=true; shift ;;
    --user-only) USER_ONLY=true; shift ;;
    --container) [[ $# -ge 2 ]] || { echo "Error: --container requires a value" >&2; exit 2; }; CONTAINER="$2"; shift 2 ;;
    -h|--help)   sed -n '2,4p' "$0"; exit 0 ;;
    *)           echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

if $JSON || [[ ! -t 1 ]]; then
  P="" W="" F="" R=""
else
  P="\033[32m" W="\033[33m" F="\033[31m" R="\033[0m"
fi

declare -a RESULTS=()
FAILURES=0

record() { # name status [detail]
  local n="$1" s="$2" d="${3:-}"
  RESULTS+=("$(printf '{"name":"%s","status":"%s","detail":"%s"}' "$n" "$s" "${d//\"/\\\"}")")
  case "$s" in
    PASS) $JSON || printf "${P}PASS${R}  %s\n" "$n" ;;
    WARN) $JSON || printf "${W}WARN${R}  %s — %s\n" "$n" "$d" ;;
    FAIL) $JSON || printf "${F}FAIL${R}  %s — %s\n" "$n" "$d"; FAILURES=$((FAILURES + 1)) ;;
  esac
}

run() { # execute locally or inside container
  if [[ -n "$CONTAINER" ]]; then
    if command -v toolbox &>/dev/null; then
      toolbox run -c "$CONTAINER" "$@" 2>/dev/null
    elif command -v distrobox &>/dev/null; then
      distrobox enter "$CONTAINER" -- "$@" 2>/dev/null
    else
      echo "Neither toolbox nor distrobox found" >&2; return 1
    fi
  else
    "$@" 2>/dev/null
  fi
}

# ---- User-level checks (always run) ----

# SSH auth to GitHub
out=$(timeout 10 ssh -T git@github.com 2>&1 || true)
if echo "$out" | grep -q "successfully authenticated"; then
  record "github-ssh-auth" "PASS"
else
  record "github-ssh-auth" "WARN" "ssh auth unconfirmed"
fi

# Dev tool presence
for tool in "oc:oc version --client" "podman:podman info" "claude:claude --version" "gh:gh --version"; do
  name="${tool%%:*}"; cmd="${tool#*:}"
  if run $cmd &>/dev/null; then record "$name" "PASS"; else record "$name" "FAIL" "not found"; fi
done

# GitHub CLI authenticated
if run gh auth status &>/dev/null 2>&1; then record "gh-auth" "PASS"
else record "gh-auth" "FAIL" "not authenticated"; fi

# YubiKey
if run ykman info &>/dev/null; then record "yubikey" "PASS"
else record "yubikey" "WARN" "not detected (plugged in?)"; fi

# Tailscale
if run tailscale status &>/dev/null; then record "tailscale" "PASS"
else record "tailscale" "WARN" "not connected"; fi

# ssh-agent has loaded keys
out=$(run ssh-add -l 2>&1 || true)
if [[ -n "$out" && "$out" != *"no identities"* && "$out" != *"Could not"* ]]; then
  record "ssh-agent-key" "PASS"
else record "ssh-agent-key" "WARN" "no keys loaded in ssh-agent"; fi

# git safe.directory should be empty
if dirs=$(run git config --global --get-all safe.directory 2>/dev/null) && [[ -n "$dirs" ]]; then
  record "git-safe-directory" "FAIL" "set: $dirs"
else record "git-safe-directory" "PASS"; fi

# Claude Code sandbox enabled
for d in "$HOME/.claude" "$HOME/.claude-work" "$HOME/.claude-personal"; do
  [[ -f "$d/settings.json" ]] || continue
  if grep -q '"enabled"[[:space:]]*:[[:space:]]*true' "$d/settings.json" 2>/dev/null; then
    record "sandbox(${d##*/})" "PASS"
  else record "sandbox(${d##*/})" "FAIL" "not enabled in $d/settings.json"; fi
done

# ---- System-level checks (skipped with --user-only, --container, or macOS) ----
IS_LINUX=true
[[ "$(uname -s)" == "Darwin" ]] && IS_LINUX=false

if ! $USER_ONLY && [[ -z "$CONTAINER" ]] && $IS_LINUX; then

  # DNS-over-TLS
  if resolvectl status 2>/dev/null | grep -qi "DNS.*Over.*TLS.*yes"; then
    record "dns-over-tls" "PASS"
  else record "dns-over-tls" "FAIL" "not active"; fi

  # ptrace scope
  val=$(sysctl -n kernel.yama.ptrace_scope 2>/dev/null || echo "?")
  if [[ "$val" == "1" ]]; then record "ptrace-scope" "PASS"
  else record "ptrace-scope" "FAIL" "=$val, expected 1"; fi

  # SELinux
  if command -v getenforce &>/dev/null; then
    se=$(getenforce 2>/dev/null || echo "?")
    if [[ "$se" == "Enforcing" ]]; then record "selinux" "PASS"
    else record "selinux" "FAIL" "$se, expected Enforcing"; fi
  fi

  # Kernel lockdown
  if [[ -f /sys/kernel/security/lockdown ]]; then
    ld=$(cat /sys/kernel/security/lockdown)
    if echo "$ld" | grep -q '\[integrity\]'; then record "kernel-lockdown" "PASS"
    else record "kernel-lockdown" "FAIL" "$ld"; fi
  fi

  # Firewall default zone = drop
  if command -v firewall-cmd &>/dev/null; then
    zone=$(firewall-cmd --get-default-zone 2>/dev/null || echo "?")
    if [[ "$zone" == "drop" ]]; then record "firewall-zone" "PASS"
    else record "firewall-zone" "FAIL" "'$zone', expected 'drop'"; fi
  fi

  # USBGuard
  if command -v usbguard &>/dev/null; then
    if usbguard list-rules &>/dev/null; then record "usbguard" "PASS"
    else record "usbguard" "WARN" "installed but cannot list rules"; fi
  else record "usbguard" "FAIL" "not installed"; fi

  # Unexpected listening ports (non-loopback)
  listeners=$(ss -tlnp 2>/dev/null | grep -vE "127\.0\.0\.1|::1" | tail -n +2 || true)
  if [[ -z "$listeners" ]]; then record "no-open-ports" "PASS"
  else record "no-open-ports" "WARN" "$(echo "$listeners" | wc -l) non-loopback listeners"; fi

fi

# ---- Output ----
if $JSON; then
  printf '{"results":[%s],"failures":%d}\n' "$(IFS=,; echo "${RESULTS[*]}")" "$FAILURES"
fi
exit $(( FAILURES > 0 ? 1 : 0 ))
