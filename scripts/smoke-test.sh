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
WARNS=0

record() { # name status [detail]
  local n="$1" s="$2" d="${3:-}"
  d="${d//\\/\\\\}"; d="${d//$'\n'/\\n}"; d="${d//$'\t'/\\t}"
  RESULTS+=("$(printf '{"name":"%s","status":"%s","detail":"%s"}' "$n" "$s" "${d//\"/\\\"}")")
  case "$s" in
    PASS) $JSON || printf "${P}PASS${R}  %s\n" "$n" ;;
    WARN) $JSON || printf "${W}WARN${R}  %s — %s\n" "$n" "$d"; WARNS=$((WARNS + 1)) ;;
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
else record "gh-auth" "WARN" "not authenticated (interactive login required)"; fi

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
  if grep -q "Ansible managed" "$HOME/$f" 2>/dev/null; then record "dotfile-$f" "PASS"
  else record "dotfile-$f" "FAIL" "not deployed or not Ansible-managed"; fi
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
  record "git-hooks-path" "PASS"
else record "git-hooks-path" "FAIL" "not configured"; fi

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
  if resolvectl status 2>/dev/null | grep -qE '\+DNSOverTLS'; then
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

  # Secure Boot (informational — not managed by Ansible, but critical to verify)
  if command -v mokutil &>/dev/null; then
    if mokutil --sb-state 2>/dev/null | grep -q "SecureBoot enabled"; then record "secure-boot" "PASS"
    else record "secure-boot" "WARN" "disabled — kernel lockdown weakened without Secure Boot chain of trust"; fi
  fi

  # LUKS disk encryption (informational — not managed by Ansible)
  if lsblk -o FSTYPE 2>/dev/null | grep -q "crypto_LUKS"; then record "luks-encryption" "PASS"
  else record "luks-encryption" "WARN" "no LUKS volumes found — full-disk encryption not confirmed"; fi

  # Firewall default zone = drop
  if command -v firewall-cmd &>/dev/null; then
    zone=$(firewall-cmd --get-default-zone 2>/dev/null || echo "?")
    if [[ "$zone" == "drop" ]]; then record "firewall-zone" "PASS"
    else record "firewall-zone" "FAIL" "'$zone', expected 'drop'"; fi
  fi

  # USBGuard (verify both installed and service active)
  if command -v usbguard &>/dev/null; then
    if systemctl is-active usbguard &>/dev/null; then record "usbguard" "PASS"
    else record "usbguard" "FAIL" "installed but usbguard.service not active"; fi
  else record "usbguard" "FAIL" "not installed"; fi

  # auditd service enabled and running
  if systemctl is-active auditd &>/dev/null && systemctl is-enabled auditd &>/dev/null; then
    record "auditd-service" "PASS"
  else record "auditd-service" "FAIL" "auditd not active or not enabled"; fi

  # Crypto policy
  if command -v update-crypto-policies &>/dev/null; then
    cp=$(update-crypto-policies --show 2>/dev/null || echo "?")
    if [[ "$cp" == "DEFAULT:NO-SHA1" ]]; then record "crypto-policy" "PASS"
    else record "crypto-policy" "WARN" "'$cp', expected 'DEFAULT:NO-SHA1'"; fi
  fi

  # sshd hardening (verify key directives, not just file existence)
  if grep -q 'PasswordAuthentication no' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q 'PermitRootLogin no' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q 'MaxAuthTries' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null; then
    record "sshd-hardening" "PASS"
  elif [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    record "sshd-hardening" "FAIL" "sshd drop-in missing key directives"
  else record "sshd-hardening" "FAIL" "sshd drop-in not deployed"; fi

  # auditd rules (verify immutability flag and sentinel watch rule)
  if grep -q '^-e 2' /etc/audit/rules.d/claude-code.rules 2>/dev/null && \
     grep -q 'claude-sensitive-write' /etc/audit/rules.d/claude-code.rules 2>/dev/null; then
    record "auditd-rules" "PASS"
  else record "auditd-rules" "FAIL" "auditd rules not deployed, missing -e 2, or sentinel rule absent"; fi

  # kernel module blacklist (verify key blacklist entries)
  if grep -q 'install cramfs /bin/false' /etc/modprobe.d/hardening.conf 2>/dev/null && \
     grep -q 'blacklist usb_storage' /etc/modprobe.d/hardening.conf 2>/dev/null; then
    record "modprobe-hardening" "PASS"
  else record "modprobe-hardening" "FAIL" "modprobe hardening not deployed or missing key blacklist entries"; fi

  # core dump disabled (verify Storage=none not just file existence)
  if grep -q 'Storage=none' /etc/systemd/coredump.conf.d/disable.conf 2>/dev/null; then
    record "coredump-disabled" "PASS"
  else record "coredump-disabled" "FAIL" "coredump Storage=none not configured"; fi

  # journald persistent storage (verify Storage=persistent, not just file existence)
  if grep -q 'Storage=persistent' /etc/systemd/journald.conf.d/99-hardening.conf 2>/dev/null; then
    record "journald-persistent" "PASS"
  else record "journald-persistent" "FAIL" "journald Storage=persistent not configured"; fi

  # cups-browsed masked (CVE-2024-47176 RCE vector)
  if systemctl is-masked cups-browsed.service &>/dev/null; then record "cups-browsed-masked" "PASS"
  else record "cups-browsed-masked" "WARN" "not masked"; fi

  # cups.service disabled (not masked — cups.socket must remain for Flatpak on-demand activation)
  _cups_state=$(systemctl show -p UnitFileState --value cups.service 2>/dev/null)
  if [[ "$_cups_state" == "disabled" ]]; then record "cups-disabled" "PASS"
  else record "cups-disabled" "WARN" "cups.service state is '$_cups_state', expected 'disabled' (masked breaks Flatpak print)"; fi
  unset _cups_state

  # avahi-daemon masked
  if systemctl is-masked avahi-daemon.service &>/dev/null; then record "avahi-masked" "PASS"
  else record "avahi-masked" "WARN" "not masked"; fi

  # AIDE file integrity
  if systemctl is-enabled aide-check.timer &>/dev/null; then record "aide-timer" "PASS"
  else record "aide-timer" "WARN" "timer not enabled"; fi

  # Chrony NTS
  if grep -qE '^(pool|server|peer).*\bnts\b' /etc/chrony.conf 2>/dev/null; then record "chrony-nts" "PASS"
  else record "chrony-nts" "WARN" "NTS not configured in chrony.conf"; fi

  # pam_wheel.so
  if grep -qE '^auth.*required.*pam_wheel.so' /etc/pam.d/su 2>/dev/null; then record "pam-wheel" "PASS"
  else record "pam-wheel" "FAIL" "su not restricted to wheel group"; fi

  # Root account locked
  if passwd -S root 2>/dev/null | grep -qE '\bLK\b|\bL\b'; then record "root-locked" "PASS"
  else record "root-locked" "FAIL" "root account not locked"; fi

  # faillock.conf (deny=5, unlock_time=900, local_users_only for SSSD safety)
  if grep -q '^deny = 5' /etc/security/faillock.conf 2>/dev/null; then record "faillock-deny" "PASS"
  else record "faillock-deny" "FAIL" "faillock deny not set to 5"; fi
  if grep -q '^local_users_only' /etc/security/faillock.conf 2>/dev/null; then record "faillock-local-only" "PASS"
  else record "faillock-local-only" "FAIL" "faillock missing local_users_only (SSSD double-lockout risk)"; fi
  if grep -q '^unlock_time = 900' /etc/security/faillock.conf 2>/dev/null; then record "faillock-unlock-time" "PASS"
  else record "faillock-unlock-time" "FAIL" "faillock unlock_time not set to 900"; fi

  # pwquality.conf (minlen=14)
  if grep -q '^minlen = 14' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-minlen" "PASS"
  else record "pwquality-minlen" "FAIL" "pwquality minlen not set to 14"; fi

  # sudoers hardening drop-in (verify use_pty, logfile, umask)
  if grep -q 'use_pty' /etc/sudoers.d/99-hardening 2>/dev/null && \
     grep -q 'logfile=' /etc/sudoers.d/99-hardening 2>/dev/null && \
     grep -q 'umask=' /etc/sudoers.d/99-hardening 2>/dev/null; then
    record "sudoers-hardening" "PASS"
  else record "sudoers-hardening" "FAIL" "sudoers hardening drop-in missing or incomplete"; fi

  # pwhistory remember=24 (CIS 5.3.5)
  if grep -q '^remember = 24' /etc/security/pwhistory.conf 2>/dev/null; then record "pwhistory-remember" "PASS"
  else record "pwhistory-remember" "FAIL" "pwhistory remember not set to 24"; fi

  # yescrypt password hashing (CIS 5.3.6)
  if grep -q '^ENCRYPT_METHOD YESCRYPT' /etc/login.defs 2>/dev/null; then record "yescrypt" "PASS"
  else record "yescrypt" "FAIL" "ENCRYPT_METHOD YESCRYPT not set in login.defs"; fi

  # yescrypt cost factor (CIS 5.4.1)
  if grep -q '^YESCRYPT_COST_FACTOR 5' /etc/login.defs 2>/dev/null; then record "yescrypt-cost" "PASS"
  else record "yescrypt-cost" "FAIL" "YESCRYPT_COST_FACTOR 5 not set in login.defs"; fi

  # fprintd masked (prevents fingerprint from bypassing faillock)
  if systemctl is-masked fprintd.service &>/dev/null; then record "fprintd-masked" "PASS"
  else record "fprintd-masked" "FAIL" "fprintd.service not masked (fingerprint can bypass faillock)"; fi

  # resolv.conf points to systemd-resolved stub (required for split DNS/MagicDNS)
  if [[ "$(readlink /etc/resolv.conf 2>/dev/null)" == "/run/systemd/resolve/stub-resolv.conf" ]]; then
    record "resolv-stub" "PASS"
  else record "resolv-stub" "WARN" "resolv.conf not symlinked to stub-resolv.conf"; fi

  # cron.allow restricts cron to root only (CIS 5.1.8)
  if grep -qx 'root' /etc/cron.allow 2>/dev/null; then record "cron-allow-root" "PASS"
  else record "cron-allow-root" "WARN" "/etc/cron.allow missing or not restricted to root"; fi

  # Critical file permissions (CIS 6.1.x)
  shadow_mode=$(stat -c '%a' /etc/shadow 2>/dev/null || echo "?")
  if [[ "$shadow_mode" == "0" ]]; then record "shadow-perms" "PASS"
  else record "shadow-perms" "FAIL" "permissions $shadow_mode, expected 0000"; fi
  gshadow_mode=$(stat -c '%a' /etc/gshadow 2>/dev/null || echo "?")
  if [[ "$gshadow_mode" == "0" ]]; then record "gshadow-perms" "PASS"
  else record "gshadow-perms" "FAIL" "permissions $gshadow_mode, expected 0000"; fi

  # TMOUT session timeout (CIS 5.5.5) — verify readonly attribute and <=900s value
  if grep -qE '^readonly TMOUT=[1-9][0-9]*' /etc/profile.d/tmout.sh 2>/dev/null; then
    record "tmout" "PASS"
  else record "tmout" "FAIL" "tmout.sh missing, not readonly, or TMOUT not set"; fi

  # /tmp noexec (CIS 1.1.2.x)
  if findmnt -n -o OPTIONS /tmp 2>/dev/null | grep -q noexec; then record "tmp-noexec" "PASS"
  else record "tmp-noexec" "FAIL" "/tmp not mounted noexec"; fi

  # /dev/shm noexec (CIS 1.1.7.x)
  if findmnt -n -o OPTIONS /dev/shm 2>/dev/null | grep -q noexec; then record "shm-noexec" "PASS"
  else record "shm-noexec" "FAIL" "/dev/shm not mounted noexec"; fi

  # /var/tmp bind-mounted to /tmp (CIS 1.1.8)
  if findmnt -n -o OPTIONS /var/tmp 2>/dev/null | grep -q bind; then record "var-tmp-bind" "PASS"
  else record "var-tmp-bind" "WARN" "/var/tmp not bind-mounted to /tmp"; fi

  # kernel.core_pattern safety
  if sysctl -n kernel.core_pattern 2>/dev/null | grep -q '/bin/false'; then record "core-pattern" "PASS"
  else record "core-pattern" "FAIL" "kernel.core_pattern not set to |/bin/false"; fi

  # ctrl+alt+del disabled (physical security)
  if systemctl is-masked ctrl-alt-del.target &>/dev/null; then record "ctrl-alt-del-masked" "PASS"
  else record "ctrl-alt-del-masked" "FAIL" "ctrl-alt-del.target not masked"; fi

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
  listeners=$(ss -tulnp 2>/dev/null | grep -vE "127\.[0-9]+\.[0-9]+\.[0-9]+|::1" | tail -n +2 || true)
  if [[ -z "$listeners" ]]; then record "no-open-ports" "PASS"
  else record "no-open-ports" "WARN" "$(echo "$listeners" | wc -l) non-loopback listeners"; fi

  # login.defs password aging (CIS 5.4.x)
  if grep -qE '^PASS_MAX_DAYS[[:space:]]+365' /etc/login.defs 2>/dev/null; then record "pass-max-days" "PASS"
  else record "pass-max-days" "FAIL" "PASS_MAX_DAYS not set to 365 in login.defs"; fi
  if grep -qE '^UMASK[[:space:]]+027' /etc/login.defs 2>/dev/null; then record "umask-login-defs" "PASS"
  else record "umask-login-defs" "FAIL" "UMASK not set to 027 in login.defs"; fi
  if grep -qE '^INACTIVE[[:space:]]+30' /etc/login.defs 2>/dev/null; then record "inactive-lock" "PASS"
  else record "inactive-lock" "FAIL" "INACTIVE not set to 30 in login.defs"; fi

  # WiFi MAC address randomization (privacy)
  if grep -q 'wifi.scan-rand-mac-address=yes' /etc/NetworkManager/conf.d/99-wifi-mac-rand.conf 2>/dev/null; then
    record "wifi-mac-rand" "PASS"
  else record "wifi-mac-rand" "WARN" "WiFi MAC randomization not configured"; fi

  # logind IdleAction=lock (physical security)
  if grep -q '^IdleAction=lock' /etc/systemd/logind.conf.d/99-hardening.conf 2>/dev/null; then
    record "logind-idle-lock" "PASS"
  else record "logind-idle-lock" "FAIL" "logind IdleAction not set to lock"; fi

  # Critical kernel sysctl values
  _sysctl_check() { local k="$1" v="$2" n="$3"; local got; got=$(sysctl -n "$k" 2>/dev/null || echo "?"); [[ "$got" == "$v" ]] && record "$n" "PASS" || record "$n" "FAIL" "$k=$got expected $v"; }
  _sysctl_check "kernel.kptr_restrict"               "1" "sysctl-kptr-restrict"
  _sysctl_check "kernel.kexec_load_disabled"         "1" "sysctl-kexec-disabled"
  _sysctl_check "kernel.io_uring_disabled"           "1" "sysctl-io-uring-disabled"
  _sysctl_check "kernel.dmesg_restrict"              "1" "sysctl-dmesg-restrict"
  _sysctl_check "kernel.unprivileged_bpf_disabled"   "1" "sysctl-bpf-restrict"
  _sysctl_check "kernel.perf_event_paranoid"         "2" "sysctl-perf-paranoid"
  _sysctl_check "net.core.bpf_jit_harden"            "1" "sysctl-bpf-jit-harden"
  _sysctl_check "kernel.randomize_va_space"          "2" "sysctl-aslr"
  _sysctl_check "fs.suid_dumpable"                   "0" "sysctl-suid-dumpable"
  _sysctl_check "net.ipv4.tcp_syncookies"            "1" "sysctl-syncookies"
  _sysctl_check "net.ipv4.conf.all.accept_redirects" "0" "sysctl-no-accept-redirects"
  _sysctl_check "net.ipv4.conf.all.send_redirects"   "0" "sysctl-no-send-redirects"

  # vsyscall=none kernel param (ROP gadget mitigation, requires reboot after grubby)
  if grep -q 'vsyscall=none' /proc/cmdline 2>/dev/null; then record "vsyscall-none" "PASS"
  else record "vsyscall-none" "WARN" "vsyscall=none not in cmdline (requires reboot if grubby ran)"; fi

fi

# ---- Output ----
if $JSON; then
  printf '{"results":[%s],"failures":%d,"warns":%d}\n' "$(IFS=,; echo "${RESULTS[*]}")" "$FAILURES" "$WARNS"
else
  total=${#RESULTS[@]}
  passes=$((total - FAILURES - WARNS))
  if [[ $FAILURES -gt 0 ]]; then
    printf "\n--- %d PASS / %d WARN / ${F}%d FAIL${R} (total %d) ---\n" "$passes" "$WARNS" "$FAILURES" "$total"
  else
    printf "\n--- %d PASS / %d WARN (total %d) ---\n" "$passes" "$WARNS" "$total"
  fi
fi
exit $(( FAILURES > 0 ? 1 : 0 ))
