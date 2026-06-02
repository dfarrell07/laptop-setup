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
  d="${d//\\/\\\\}"; d="${d//$'\n'/\\n}"; d="${d//$'\t'/\\t}"
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
out=$(run timeout 10 ssh -T git@github.com 2>&1 || true)
if echo "$out" | grep -q "successfully authenticated"; then
  record "github-ssh-auth" "PASS"
else
  record "github-ssh-auth" "WARN" "ssh auth unconfirmed"
fi

# Dev tool presence
for tool in "oc:oc version --client" "kubectl:kubectl version --client" "podman:podman info" "claude:claude --version" "gh:gh --version" "kind:kind version" "helm:helm version --short" "kustomize:kustomize version" "jq:jq --version" "tmux:tmux -V" "go:go version"; do
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

# --- Dotfiles checks ---
for f in .zshrc .gitconfig .tmux.conf .vimrc .bashrc; do
  if [[ -f "$HOME/$f" ]]; then record "dotfile-$f" "PASS"
  else record "dotfile-$f" "FAIL" "not deployed"; fi
done

# SSH config and permissions
if [[ -f "$HOME/.ssh/config" ]]; then
  perms=$(stat -c '%a' "$HOME/.ssh/config" 2>/dev/null || stat -f '%Lp' "$HOME/.ssh/config" 2>/dev/null)
  if [[ "$perms" == "600" ]]; then record "ssh-config" "PASS"
  else record "ssh-config" "FAIL" "permissions $perms, expected 600"; fi
else record "ssh-config" "FAIL" "not deployed"; fi

sshdir_perms=$(stat -c '%a' "$HOME/.ssh" 2>/dev/null || stat -f '%Lp' "$HOME/.ssh" 2>/dev/null || echo "?")
if [[ "$sshdir_perms" == "700" ]]; then record "ssh-dir-perms" "PASS"
else record "ssh-dir-perms" "FAIL" "permissions $sshdir_perms, expected 700"; fi

# --- Git security checks ---
for check in "core.fsmonitor=false" "safe.bareRepository=explicit" "commit.gpgsign=true" "gpg.format=ssh"; do
  key="${check%%=*}" expected="${check#*=}"
  actual=$(run git config --global "$key" 2>/dev/null || echo "")
  if [[ "$actual" == "$expected" ]]; then record "git-$key" "PASS"
  else record "git-$key" "FAIL" "got '$actual', expected '$expected'"; fi
done

# git safe.directory should be empty
if dirs=$(run git config --global --get-all safe.directory 2>/dev/null) && [[ -n "$dirs" ]]; then
  record "git-safe-directory" "FAIL" "set: $dirs"
else record "git-safe-directory" "PASS"; fi

# git hooksPath configured
if hp=$(run git config --global core.hooksPath 2>/dev/null) && [[ -n "$hp" ]]; then
  record "git-hooksPath" "PASS"
else record "git-hooksPath" "FAIL" "not configured"; fi

# Claude Code sandbox enabled (use jq if available, fall back to grep)
for d in "$HOME/.claude" "$HOME/.claude-work" "$HOME/.claude-personal"; do
  [[ -f "$d/settings.json" ]] || continue
  label="${d##*/}"
  if command -v jq &>/dev/null; then
    if jq -e '.sandbox.enabled' "$d/settings.json" &>/dev/null; then
      record "sandbox($label)" "PASS"
    else record "sandbox($label)" "FAIL" "sandbox not enabled"; fi
    if jq -e '.enableAllProjectMcpServers == false' "$d/settings.json" &>/dev/null; then
      record "mcp-disabled($label)" "PASS"
    else record "mcp-disabled($label)" "WARN" "enableAllProjectMcpServers not false"; fi
  else
    if grep -q '"enabled"[[:space:]]*:[[:space:]]*true' "$d/settings.json" 2>/dev/null; then
      record "sandbox($label)" "PASS"
    else record "sandbox($label)" "FAIL" "not enabled in $d/settings.json"; fi
  fi
done

# Claude directory permissions
for d in "$HOME/.claude" "$HOME/.claude-work" "$HOME/.claude-personal"; do
  [[ -d "$d" ]] || continue
  perms=$(stat -c '%a' "$d" 2>/dev/null || stat -f '%Lp' "$d" 2>/dev/null || echo "?")
  if [[ "$perms" == "700" ]]; then record "perms(${d##*/})" "PASS"
  else record "perms(${d##*/})" "FAIL" "permissions $perms, expected 700"; fi
done

# Config file validation
if [[ -f /etc/opt/chrome/policies/managed/security.json ]]; then
  if python3 -c "import json; json.load(open('/etc/opt/chrome/policies/managed/security.json'))" 2>/dev/null; then
    record "chrome-policy-json" "PASS"
  else record "chrome-policy-json" "FAIL" "invalid JSON"; fi
fi

# --- Notes repo (transcrypt-encrypted) ---
if [[ -d "$HOME/notes/.git" ]]; then
  record "notes-repo" "PASS"
  if command -v transcrypt &>/dev/null; then
    if (cd "$HOME/notes" && transcrypt --display) &>/dev/null 2>&1; then
      record "notes-transcrypt" "PASS"
    else
      record "notes-transcrypt" "FAIL" "repo exists but transcrypt not initialized"
    fi
  else
    record "notes-transcrypt" "WARN" "transcrypt not installed"
  fi
else
  record "notes-repo" "WARN" "notes repo not cloned — run 'make notes'"
fi

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

  # Crypto policy
  if command -v update-crypto-policies &>/dev/null; then
    cp=$(update-crypto-policies --show 2>/dev/null || echo "?")
    if [[ "$cp" == "DEFAULT:NO-SHA1" ]]; then record "crypto-policy" "PASS"
    else record "crypto-policy" "WARN" "'$cp', expected 'DEFAULT:NO-SHA1'"; fi
  fi

  # sshd hardening
  if [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    if grep -q 'PasswordAuthentication no' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null; then
      record "sshd-hardening" "PASS"
    else record "sshd-hardening" "WARN" "config deployed but PasswordAuthentication not disabled"; fi
  else record "sshd-hardening" "FAIL" "drop-in config not deployed"; fi

  # auditd rules
  if [[ -f /etc/audit/rules.d/claude-code.rules ]]; then record "auditd-rules" "PASS"
  else record "auditd-rules" "FAIL" "not deployed"; fi

  # kernel module blacklist
  if [[ -f /etc/modprobe.d/hardening.conf ]]; then record "modprobe-hardening" "PASS"
  else record "modprobe-hardening" "FAIL" "not deployed"; fi

  # core dump disabled
  if [[ -f /etc/systemd/coredump.conf.d/disable.conf ]]; then record "coredump-disabled" "PASS"
  else record "coredump-disabled" "FAIL" "config not deployed"; fi

  # cups-browsed masked
  if systemctl is-masked cups-browsed.service &>/dev/null; then record "cups-browsed-masked" "PASS"
  else record "cups-browsed-masked" "WARN" "not masked"; fi

  # avahi-daemon masked
  if systemctl is-masked avahi-daemon.service &>/dev/null; then record "avahi-masked" "PASS"
  else record "avahi-masked" "WARN" "not masked"; fi

  # AIDE file integrity
  if systemctl is-enabled aide-check.timer &>/dev/null; then record "aide-timer" "PASS"
  else record "aide-timer" "WARN" "timer not enabled"; fi

  # Chrony NTS
  if grep -q 'nts' /etc/chrony.conf 2>/dev/null; then record "chrony-nts" "PASS"
  else record "chrony-nts" "WARN" "NTS not configured in chrony.conf"; fi

  # pam_wheel.so
  if grep -qE '^auth.*required.*pam_wheel.so' /etc/pam.d/su 2>/dev/null; then record "pam-wheel" "PASS"
  else record "pam-wheel" "WARN" "su not restricted to wheel group"; fi

  # Root account locked
  if passwd -S root 2>/dev/null | grep -qE '\bLK\b|\bL\b'; then record "root-locked" "PASS"
  else record "root-locked" "WARN" "root account not locked"; fi

  # dnf-automatic
  timer="dnf-automatic.timer"
  command -v dnf5 &>/dev/null && timer="dnf5-automatic.timer"
  if systemctl is-enabled "$timer" &>/dev/null; then record "dnf-automatic" "PASS"
  else record "dnf-automatic" "WARN" "timer not enabled"; fi

  # Chrome policies
  if [[ -f /etc/opt/chrome/policies/managed/security.json ]]; then
    record "chrome-policies" "PASS"
  else record "chrome-policies" "WARN" "not deployed"; fi

  # Unexpected listening ports (non-loopback)
  listeners=$(ss -tulnp 2>/dev/null | grep -vE "127\.0\.0\.1|::1" | tail -n +2 || true)
  if [[ -z "$listeners" ]]; then record "no-open-ports" "PASS"
  else record "no-open-ports" "WARN" "$(echo "$listeners" | wc -l) non-loopback listeners"; fi

fi

# ---- Output ----
if $JSON; then
  printf '{"results":[%s],"failures":%d}\n' "$(IFS=,; echo "${RESULTS[*]}")" "$FAILURES"
fi
exit $(( FAILURES > 0 ? 1 : 0 ))
