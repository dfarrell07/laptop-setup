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
for tool in "oc:oc version --client" "kubectl:kubectl version --client" "podman:podman info" "claude:claude --version" "gh:gh --version" "kind:kind version" "helm:helm version --short" "kustomize:kustomize version" "jq:jq --version" "tmux:tmux -V" "go:go version" "rg:rg --version" "fzf:fzf --version" "tc:tc -V" "strace:strace --version" "cosign:cosign version" "tkn:tkn version --component=cli" "bpfman:bpfman --version"; do
  name="${tool%%:*}"; cmd="${tool#*:}"
  if run $cmd &>/dev/null; then record "$name" "PASS"; else record "$name" "FAIL" "not found"; fi
done

# ec CLI (work-profile only — guard on binary presence)
if [[ -x /usr/local/bin/ec ]]; then
  if run ec version &>/dev/null; then record "ec" "PASS"
  else record "ec" "FAIL" "not executable"; fi
fi

# GitHub CLI authenticated
if run gh auth status &>/dev/null 2>&1; then record "gh-auth" "PASS"
else record "gh-auth" "WARN" "not authenticated (interactive login required)"; fi

# YubiKey
if run ykman info &>/dev/null; then record "yubikey" "PASS"
else record "yubikey" "WARN" "not detected (plugged in?)"; fi

# Tailscale connectivity (cross-platform via CLI)
if run tailscale status &>/dev/null; then record "tailscale" "PASS"
else record "tailscale" "WARN" "not connected"; fi

# ssh-agent has a FIDO2 sk-ssh-ed25519 key loaded (use -L for full pubkey: -l shows ED25519-SK not sk-ssh-ed25519)
out=$(run ssh-add -L 2>&1 || true)
if echo "$out" | grep -q 'sk-ssh-ed25519'; then record "ssh-agent-key" "PASS"
elif [[ -n "$out" && "$out" != *"no identities"* && "$out" != *"Could not"* && "$out" != *"Error"* ]]; then
  record "ssh-agent-key" "WARN" "key loaded but not sk-ssh-ed25519 type"
else record "ssh-agent-key" "WARN" "no keys loaded in ssh-agent"; fi

# --- Dotfiles checks ---
for f in .zshrc .gitconfig .tmux.conf .vimrc .bashrc; do
  if grep -q "Ansible managed" "$HOME/$f" 2>/dev/null; then record "dotfile-$f" "PASS"
  else record "dotfile-$f" "FAIL" "not deployed or not Ansible-managed"; fi
done

# global gitignore (XDG path — read automatically by Git, no core.excludesfile needed)
_gi="$HOME/.config/git/ignore"
if grep -q "Ansible managed" "$_gi" 2>/dev/null; then record "dotfile-git-ignore" "PASS"
else record "dotfile-git-ignore" "FAIL" "not deployed or not Ansible-managed: $_gi"; fi
unset _gi

# ripgreprc (loaded via RIPGREP_CONFIG_PATH in zshrc)
_rg="$HOME/.config/ripgrep/config"
if grep -q "Ansible managed" "$_rg" 2>/dev/null; then record "dotfile-ripgreprc" "PASS"
else record "dotfile-ripgreprc" "FAIL" "not deployed or not Ansible-managed: $_rg"; fi
unset _rg

# environment.d containers.conf (KIND + Podman socket — pam_env injection for make kind)
if [[ "$(uname -s)" == "Linux" ]]; then
  _ecf="$HOME/.config/environment.d/containers.conf"
  if [[ -f "$_ecf" ]] && grep -q "DOCKER_HOST" "$_ecf" && grep -q "KIND_EXPERIMENTAL_PROVIDER" "$_ecf"; then
    record "env-d-containers" "PASS"
  elif [[ ! -f "$_ecf" ]]; then record "env-d-containers" "FAIL" "missing: $_ecf — 'make kind' will fail in OVN-K"
  else record "env-d-containers" "FAIL" "DOCKER_HOST or KIND_EXPERIMENTAL_PROVIDER missing in $_ecf"; fi
  unset _ecf
fi

# SSH config and permissions
if [[ -f "$HOME/.ssh/config" ]]; then
  perms=$(stat -c '%a' "$HOME/.ssh/config" 2>/dev/null || stat -f '%Lp' "$HOME/.ssh/config" 2>/dev/null)
  if [[ "$perms" == "600" ]]; then record "ssh-config" "PASS"
  else record "ssh-config" "FAIL" "permissions $perms, expected 600"; fi
else record "ssh-config" "FAIL" "not deployed"; fi

sshdir_perms=$(stat -c '%a' "$HOME/.ssh" 2>/dev/null || stat -f '%Lp' "$HOME/.ssh" 2>/dev/null || echo "?")
if [[ "$sshdir_perms" == "700" ]]; then record "ssh-dir-perms" "PASS"
else record "ssh-dir-perms" "FAIL" "permissions $sshdir_perms, expected 700"; fi

homedir_perms=$(stat -c '%a' "$HOME" 2>/dev/null || stat -f '%Lp' "$HOME" 2>/dev/null || echo "?")
if [[ "$homedir_perms" == "750" ]]; then record "home-dir-perms" "PASS"
else record "home-dir-perms" "FAIL" "permissions $homedir_perms, expected 750 (CIS)"; fi

# --- Git security checks ---
for check in "core.fsmonitor=false" "safe.bareRepository=explicit" "commit.gpgsign=true" "tag.gpgsign=true" "gpg.format=ssh" "gpg.ssh.allowedSignersFile=~/.config/git/allowed_signers" "user.signingkey=~/.ssh/id_ed25519_sk_signing.pub"; do
  key="${check%%=*}" expected="${check#*=}"
  actual=$(run git config --global "$key" 2>/dev/null || echo "")
  if [[ "$actual" == "$expected" ]]; then record "git-$key" "PASS"
  else record "git-$key" "FAIL" "got '$actual', expected '$expected'"; fi
done

# git allowed_signers file (required for SSH commit verification)
_as="$HOME/.config/git/allowed_signers"
if [[ ! -f "$_as" ]]; then record "git-allowed-signers" "FAIL" "file missing: $_as"
elif [[ ! -s "$_as" ]]; then record "git-allowed-signers" "FAIL" "file is empty: $_as"
elif ! grep -q 'sk-ssh-ed25519' "$_as"; then record "git-allowed-signers" "FAIL" "no sk-ssh-ed25519 key in $_as"
else record "git-allowed-signers" "PASS"; fi
unset _as

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
    if jq -e '.sandbox.failIfUnavailable' "$d/settings.json" &>/dev/null; then
      record "sandbox-failsafe($label)" "PASS"
    else record "sandbox-failsafe($label)" "FAIL" "sandbox.failIfUnavailable not true — sandbox bypass possible"; fi
    if jq -e '.sandbox.allowUnsandboxedCommands == false' "$d/settings.json" &>/dev/null; then
      record "sandbox-cmds($label)" "PASS"
    else record "sandbox-cmds($label)" "FAIL" "sandbox.allowUnsandboxedCommands not false — Bash escapes sandbox"; fi
    if jq -e '.enableAllProjectMcpServers == false' "$d/settings.json" &>/dev/null; then
      record "mcp-disabled($label)" "PASS"
    else record "mcp-disabled($label)" "WARN" "enableAllProjectMcpServers not false"; fi
  else
    if python3 -c "import json,sys; d=json.load(open('$d/settings.json')); sys.exit(0 if d.get('sandbox',{}).get('enabled') else 1)" 2>/dev/null; then
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
      record "notes-transcrypt" "WARN" "repo exists but transcrypt not initialized (run: make vault-edit to populate vault_notes_transcrypt_password)"
    fi
  else
    record "notes-transcrypt" "WARN" "transcrypt not installed"
  fi
else
  record "notes-repo" "WARN" "notes repo not cloned — run 'make notes'"
fi

# --- Wayland desktop tools (Linux only — not installed on macOS) ---
if [[ "$(uname -s)" == "Linux" ]]; then
  # cliphist: clipboard history manager — exec wl-paste --watch cliphist store in sway config;
  # clipboard contents die with source app if this is missing
  if command -v cliphist &>/dev/null; then record "cliphist" "PASS"
  else record "cliphist" "FAIL" "not found (clipboard history broken in sway — check desktop_sway_packages)"; fi
  # wl-paste: used by cliphist daemon and wl-copy used by cliphist picker keybinding
  if command -v wl-paste &>/dev/null; then record "wl-paste" "PASS"
  else record "wl-paste" "FAIL" "not found (wl-clipboard missing — cliphist daemon and clipboard copy broken)"; fi
fi

# ---- System-level checks (skipped with --user-only, --container, or macOS) ----
IS_LINUX=true
[[ "$(uname -s)" == "Darwin" ]] && IS_LINUX=false

# CSB detection: FQDN ends in .csb (mirrors Ansible's Fedora CSB path in csb_detect.yml)
CSB_HOST=false
[[ "$(hostname -f 2>/dev/null)" == *".csb" ]] && CSB_HOST=true

if ! $USER_ONLY && [[ -z "$CONTAINER" ]] && $IS_LINUX; then

  # DNS-over-TLS (skipped on CSB — Ansible intentionally omits the resolved config on CSB hosts
  # because Domains=~. catch-all would route all DNS to Cloudflare 1.1.1.1, which is blocked
  # on CSB corporate networks; DHCP DNS remains active and working via systemd-resolved)
  if resolvectl status 2>/dev/null | grep -qE '\+DNSOverTLS'; then
    record "dns-over-tls" "PASS"
  elif $CSB_HOST; then
    record "dns-over-tls" "WARN" "not active — expected on CSB (Cloudflare blocked; DHCP DNS in use)"
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
    else record "kernel-lockdown" "WARN" "lockdown not in integrity mode ($ld) — requires Secure Boot"; fi
  fi

  # Secure Boot (informational — not managed by Ansible, but critical to verify)
  if command -v mokutil &>/dev/null; then
    if mokutil --sb-state 2>/dev/null | grep -q "SecureBoot enabled"; then record "secure-boot" "PASS"
    else record "secure-boot" "WARN" "disabled — kernel lockdown weakened without Secure Boot chain of trust"; fi
  fi

  # LUKS disk encryption (informational — not managed by Ansible)
  if lsblk -o FSTYPE 2>/dev/null | grep -q "crypto_LUKS"; then record "luks-encryption" "PASS"
  else record "luks-encryption" "WARN" "no LUKS volumes found — full-disk encryption not confirmed"; fi
  # Stale rd.luks.key param: if cmdline references a keyfile but it doesn't exist on /boot,
  # that's a latent foothold — a keyfile dropped there unlocks LUKS without passphrase
  if grep -q 'rd.luks.key' /proc/cmdline 2>/dev/null; then
    if [[ -f /boot/keyfile ]]; then
      record "luks-no-keyfile" "FAIL" "rd.luks.key in cmdline AND /boot/keyfile exists — LUKS key exposed on unencrypted /boot"
    else
      record "luks-no-keyfile" "WARN" "stale rd.luks.key param in cmdline but no keyfile on /boot — clean up with grubby --remove-args"
    fi
  fi

  # Firewall default zone = drop, SSH port open, tailscale0 in trusted zone
  if command -v firewall-cmd &>/dev/null; then
    zone=$(firewall-cmd --get-default-zone 2>/dev/null || echo "?")
    if [[ "$zone" == "drop" ]]; then record "firewall-zone" "PASS"
    elif $CSB_HOST; then record "firewall-zone" "WARN" "zone='$zone' — IT manages zone policy on CSB; drop zone not applied"
    else record "firewall-zone" "FAIL" "'$zone', expected 'drop'"; fi
    _ssh_port=$(grep -oP '^Port \K[0-9]+' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null || echo "?")
    if [[ "$_ssh_port" == "722" ]]; then record "sshd-port" "PASS"
    else record "sshd-port" "FAIL" "Port='$_ssh_port' expected '722' (default ssh_port in playbook)"; fi
    if [[ "$zone" == "drop" ]]; then
      # Only check drop-zone-specific rules when the drop zone is actually active
      if firewall-cmd --zone=drop --query-port="${_ssh_port}/tcp" &>/dev/null; then record "firewall-ssh-port" "PASS"
      else record "firewall-ssh-port" "FAIL" "port ${_ssh_port}/tcp not open in drop zone"; fi
    elif $CSB_HOST; then record "firewall-ssh-port" "WARN" "skipped on CSB — drop zone not active (IT manages zones)"
    else record "firewall-ssh-port" "FAIL" "port ${_ssh_port}/tcp not open in drop zone"; fi
    if ip link show tailscale0 &>/dev/null; then
      ts_zone=$(firewall-cmd --get-zone-of-interface=tailscale0 2>/dev/null || echo "?")
      if [[ "$ts_zone" == "trusted" ]]; then record "firewall-tailscale-zone" "PASS"
      else record "firewall-tailscale-zone" "FAIL" "tailscale0 in zone '$ts_zone', expected 'trusted'"; fi
    fi
    # ICMP block-inversion must be enabled when drop zone is active
    if [[ "$zone" == "drop" ]]; then
      if firewall-cmd --zone=drop --query-icmp-block-inversion &>/dev/null; then record "firewall-icmp-inversion" "PASS"
      else record "firewall-icmp-inversion" "FAIL" "icmp-block-inversion not enabled in drop zone — NDP and PMTU discovery broken"; fi
    elif $CSB_HOST; then record "firewall-icmp-inversion" "WARN" "skipped on CSB — drop zone not active"
    else record "firewall-icmp-inversion" "FAIL" "icmp-block-inversion not enabled in drop zone"; fi
    # libvirt zone must not have ssh enabled (VMs could reach host sshd — lateral movement path)
    if firewall-cmd --get-zones 2>/dev/null | grep -q '\blibvirt\b'; then
      if firewall-cmd --zone=libvirt --query-service=ssh &>/dev/null 2>&1; then
        record "firewall-libvirt-no-ssh" "FAIL" "ssh service in libvirt zone — VMs on virbr0 can reach host sshd"
      else record "firewall-libvirt-no-ssh" "PASS"; fi
    fi
  fi

  # tailscaled service state (Linux systemd — on macOS tailscale uses launchd, handled by connectivity check above)
  if systemctl is-active tailscaled &>/dev/null; then record "tailscaled-active" "PASS"
  else record "tailscaled-active" "WARN" "tailscaled not running"; fi
  if systemctl is-enabled tailscaled &>/dev/null; then record "tailscaled-enabled" "PASS"
  else record "tailscaled-enabled" "WARN" "tailscaled not enabled (won't start on reboot, VPN tunnel lost)"; fi

  # USBGuard (verify both installed, active, and enabled)
  if command -v usbguard &>/dev/null; then
    if systemctl is-active usbguard &>/dev/null && systemctl is-enabled usbguard &>/dev/null; then
      record "usbguard" "PASS"
    elif systemctl is-active usbguard &>/dev/null; then
      record "usbguard" "WARN" "usbguard active but not enabled (won't start on reboot)"
    else record "usbguard" "FAIL" "installed but usbguard.service not active"; fi
  else record "usbguard" "FAIL" "not installed"; fi

  # auditd service enabled and running
  if systemctl is-active auditd &>/dev/null && systemctl is-enabled auditd &>/dev/null; then
    record "auditd-service" "PASS"
  else record "auditd-service" "FAIL" "auditd not active or not enabled"; fi

  # Crypto policy (skipped on CSB — IT may enforce FIPS/FUTURE; Ansible guard deliberately omits DEFAULT:NO-SHA1 there)
  if command -v update-crypto-policies &>/dev/null; then
    cp=$(update-crypto-policies --show 2>/dev/null || echo "?")
    if [[ "$cp" == "DEFAULT:NO-SHA1" ]]; then record "crypto-policy" "PASS"
    elif $CSB_HOST; then record "crypto-policy" "WARN" "skipped on CSB — policy is '$cp' (IT-managed; DEFAULT:NO-SHA1 not applied)"
    else record "crypto-policy" "FAIL" "'$cp', expected 'DEFAULT:NO-SHA1' (SHA1 accepted system-wide)"; fi
  fi

  # sshd hardening (verify key directives and value of MaxAuthTries ≤4)
  _max_auth=$(grep -oP '^MaxAuthTries \K[0-9]+' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null || echo "?")
  if grep -q '^PasswordAuthentication no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^KbdInteractiveAuthentication no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^PermitRootLogin no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^PermitEmptyPasswords no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^X11Forwarding no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -qP '^ClientAliveCountMax [1-9]$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^HostKeyAlgorithms ssh-ed25519$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^AllowAgentForwarding no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^AllowTcpForwarding no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^PermitUserEnvironment no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     [[ "$_max_auth" != "?" && "$_max_auth" -le 4 ]]; then
    record "sshd-hardening" "PASS"
  elif [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    record "sshd-hardening" "FAIL" "sshd drop-in has wrong directives — check PasswordAuthentication/AllowForwarding/PermitUserEnvironment/HostKeyAlgorithms (MaxAuthTries=$_max_auth)"
  else record "sshd-hardening" "FAIL" "sshd drop-in not deployed"; fi
  # AllowUsers must contain the actual user — empty or 'root' would lock everyone out
  if [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    _allow_users=$(grep -oP '^AllowUsers \K.*' /etc/ssh/sshd_config.d/00-hardening.conf | tr -d ' ' || true)
    if [[ "$_allow_users" == "$USER" ]]; then record "sshd-allowusers" "PASS"
    else record "sshd-allowusers" "FAIL" "AllowUsers='$_allow_users' expected '$USER'"; fi
  else record "sshd-allowusers" "FAIL" "sshd drop-in not deployed"; fi

  # auditd rules (verify immutability flag and sentinel watch rule)
  if grep -q '^-e 2' /etc/audit/rules.d/claude-code.rules 2>/dev/null && \
     grep -q ' -k claude-sensitive-write$' /etc/audit/rules.d/claude-code.rules 2>/dev/null; then
    record "auditd-rules" "PASS"
  else record "auditd-rules" "FAIL" "auditd rules not deployed, missing -e 2, or sentinel rule absent"; fi
  # auditd kernel state: verify -e 2 is active in running kernel (requires root/CAP_AUDIT_CONTROL)
  if [[ $EUID -eq 0 ]]; then
    if auditctl -s 2>/dev/null | grep -q '^enabled 2'; then record "auditd-immutable" "PASS"
    else record "auditd-immutable" "WARN" "auditd not in immutable mode (may need reboot after initial deploy)"; fi
  else
    record "auditd-immutable" "WARN" "auditctl requires root to check kernel state; re-run as root to verify"
  fi
  # Auditd watch keys for new paths deployed by the system role
  for _key in power-config device-policy kernel-params kernel-modules logins kernel-module-load kernel-module-unload perm_mod bpfman-config crypto-policy; do
    if grep -q " -k ${_key}$" /etc/audit/rules.d/claude-code.rules 2>/dev/null; then
      record "auditd-watch-${_key}" "PASS"
    else record "auditd-watch-${_key}" "WARN" "watch key ${_key} missing from claude-code.rules"; fi
  done
  # AIDE monitoring of security-critical conf.d directories (verify lineinfile tasks applied)
  if [[ -f /etc/aide.conf ]]; then
    for _path in "/etc/systemd/resolved.conf.d" "/etc/systemd/logind.conf.d" "/etc/tlp.d" "/etc/tlp.conf"; do
      label="aide-monitors-$(basename "$_path")"
      if grep -qF "$_path" /etc/aide.conf 2>/dev/null; then record "$label" "PASS"
      else record "$label" "WARN" "$_path not found in /etc/aide.conf"; fi
    done
  else record "aide-not-configured" "WARN" "/etc/aide.conf absent; run: aide --init && cp /var/lib/aide/aide.db.new.gz /var/lib/aide/aide.db.gz"; fi

  # kernel module blacklist (verify key blacklist entries)
  if grep -q '^install cramfs /bin/false' /etc/modprobe.d/hardening.conf 2>/dev/null && \
     grep -q '^blacklist usb_storage' /etc/modprobe.d/hardening.conf 2>/dev/null && \
     grep -q '^blacklist vivid' /etc/modprobe.d/hardening.conf 2>/dev/null && \
     grep -q '^blacklist n_hdlc' /etc/modprobe.d/hardening.conf 2>/dev/null; then
    record "modprobe-hardening" "PASS"
  else record "modprobe-hardening" "FAIL" "modprobe hardening not deployed or missing key blacklist entries"; fi

  # core dump disabled (verify Storage=none not just file existence)
  if grep -q '^Storage=none' /etc/systemd/coredump.conf.d/disable.conf 2>/dev/null; then
    record "coredump-disabled" "PASS"
  else record "coredump-disabled" "FAIL" "coredump Storage=none not configured"; fi

  # journald persistent storage (verify Storage=persistent, not just file existence)
  if grep -q '^Storage=persistent' /etc/systemd/journald.conf.d/99-hardening.conf 2>/dev/null; then
    record "journald-persistent" "PASS"
  else record "journald-persistent" "FAIL" "journald Storage=persistent not configured"; fi

  # cups-browsed masked (CVE-2024-47176 RCE vector)
  if systemctl is-masked cups-browsed.service &>/dev/null; then record "cups-browsed-masked" "PASS"
  else record "cups-browsed-masked" "FAIL" "not masked (CVE-2024-47176 RCE vector — must be masked)"; fi

  # cups.service disabled (not masked — cups.socket must remain for Flatpak on-demand activation)
  _cups_state=$(systemctl show -p UnitFileState --value cups.service 2>/dev/null)
  if [[ "$_cups_state" == "disabled" ]]; then record "cups-disabled" "PASS"
  else record "cups-disabled" "WARN" "cups.service state is '$_cups_state', expected 'disabled' (masked breaks Flatpak print)"; fi
  unset _cups_state

  # avahi-daemon masked
  if systemctl is-masked avahi-daemon.service &>/dev/null; then record "avahi-masked" "PASS"
  else record "avahi-masked" "FAIL" "not masked (mDNS service discovery leakage risk)"; fi
  # passim masked (fwupd dependency — unauthenticated HTTP on 0.0.0.0:27500 reachable via Tailscale trusted zone)
  if systemctl is-masked passim.service &>/dev/null; then record "passim-masked" "PASS"
  else record "passim-masked" "FAIL" "not masked (unauthenticated HTTP server on 0.0.0.0:27500)"; fi

  # AIDE file integrity (timer enabled+active AND database initialized)
  if systemctl is-enabled aide-check.timer &>/dev/null && systemctl is-active aide-check.timer &>/dev/null; then
    record "aide-timer" "PASS"
  elif systemctl is-enabled aide-check.timer &>/dev/null; then
    record "aide-timer" "WARN" "timer enabled but not active (reboot or: systemctl start aide-check.timer)"
  else record "aide-timer" "WARN" "timer not enabled"; fi
  if [[ -f /var/lib/aide/aide.db.gz ]]; then record "aide-db" "PASS"
  else record "aide-db" "WARN" "AIDE database not initialized (run: aide --init)"; fi

  # Chrony NTS: first verify config, then verify actual NTS cookies established
  # (port 4460 is required for NTS-KE; may be blocked on CSB corporate networks)
  if grep -qE '^(pool|server|peer).*\bnts\b' /etc/chrony.conf 2>/dev/null; then
    if chronyc -c authdata 2>/dev/null | awk -F, '$5 > 0 {found=1} END {exit !found}'; then
      record "chrony-nts" "PASS"
    else record "chrony-nts" "WARN" "NTS configured but no authenticated sources (port 4460 blocked? needs boot?)"; fi
  else record "chrony-nts" "WARN" "NTS not configured in chrony.conf"; fi
  if systemctl is-enabled chronyd &>/dev/null && systemctl is-active chronyd &>/dev/null; then
    record "chronyd-service" "PASS"
  elif systemctl is-enabled chronyd &>/dev/null; then
    record "chronyd-service" "FAIL" "chronyd enabled but not active (time sync required for FIDO2/TLS)"
  else record "chronyd-service" "FAIL" "chronyd not enabled or started (time sync absent = FIDO2/TLS breaks)"; fi
  # fwupd firmware update daemon
  if systemctl is-enabled fwupd &>/dev/null; then record "fwupd-enabled" "PASS"
  else record "fwupd-enabled" "WARN" "fwupd not enabled (firmware updates won't run automatically)"; fi
  if systemctl is-active fwupd-refresh.timer &>/dev/null; then record "fwupd-refresh-timer" "PASS"
  else record "fwupd-refresh-timer" "WARN" "fwupd-refresh.timer inactive — LVFS firmware metadata may be stale"; fi

  # pam_wheel.so
  if grep -qE '^auth.*required.*pam_wheel.so' /etc/pam.d/su 2>/dev/null; then record "pam-wheel" "PASS"
  else record "pam-wheel" "FAIL" "su not restricted to wheel group"; fi

  # Root account locked (passwd -S root requires root — WARN not FAIL when non-root)
  if [[ "$EUID" -eq 0 ]]; then
    if passwd -S root 2>/dev/null | grep -qE '\bLK\b|\bL\b'; then record "root-locked" "PASS"
    else record "root-locked" "FAIL" "root account not locked"; fi
  else record "root-locked" "WARN" "skipped — passwd -S root requires root (run with sudo for full check)"; fi

  # authselect PAM features (verifies faillock/pwhistory are wired into PAM stack, not just configured)
  if command -v authselect &>/dev/null; then
    # Profile must be 'sssd' — if drifted to 'local' or custom, features may behave differently
    if authselect current 2>/dev/null | grep -q 'sssd'; then record "authselect-profile" "PASS"
    else record "authselect-profile" "FAIL" "authselect profile is not sssd (faillock/pwhistory may not wire correctly)"; fi
    if authselect is-feature-enabled with-faillock 2>/dev/null; then record "authselect-faillock" "PASS"
    else record "authselect-faillock" "FAIL" "authselect with-faillock not enabled (faillock settings won't apply)"; fi
    if authselect is-feature-enabled with-pwhistory 2>/dev/null; then record "authselect-pwhistory" "PASS"
    else record "authselect-pwhistory" "FAIL" "authselect with-pwhistory not enabled (history reuse won't enforce)"; fi
    # authselect check verifies actual PAM files match profile+features — is-feature-enabled only checks state file
    if authselect check 2>/dev/null; then record "authselect-check" "PASS"
    else record "authselect-check" "FAIL" "authselect PAM files differ from profile — run: authselect select sssd --force"; fi
  fi

  # faillock.conf (deny=5, unlock_time=900, local_users_only for SSSD safety)
  if grep -q '^deny = 5' /etc/security/faillock.conf 2>/dev/null; then record "faillock-deny" "PASS"
  else record "faillock-deny" "FAIL" "faillock deny not set to 5"; fi
  if grep -q '^local_users_only' /etc/security/faillock.conf 2>/dev/null; then record "faillock-local-only" "PASS"
  else record "faillock-local-only" "FAIL" "faillock missing local_users_only (SSSD double-lockout risk)"; fi
  if grep -q '^unlock_time = 900' /etc/security/faillock.conf 2>/dev/null; then record "faillock-unlock-time" "PASS"
  else record "faillock-unlock-time" "FAIL" "faillock unlock_time not set to 900"; fi

  # pwquality.conf (minlen=14 + complexity settings — CIS 5.3.x)
  if grep -q '^minlen = 14' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-minlen" "PASS"
  else record "pwquality-minlen" "FAIL" "pwquality minlen not set to 14"; fi
  if grep -q '^dcredit = -1' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-dcredit" "PASS"
  else record "pwquality-dcredit" "FAIL" "pwquality dcredit not set to -1"; fi
  if grep -q '^ucredit = -1' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-ucredit" "PASS"
  else record "pwquality-ucredit" "FAIL" "pwquality ucredit not set to -1"; fi
  if grep -q '^lcredit = -1' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-lcredit" "PASS"
  else record "pwquality-lcredit" "FAIL" "pwquality lcredit not set to -1"; fi
  if grep -q '^ocredit = -1' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-ocredit" "PASS"
  else record "pwquality-ocredit" "FAIL" "pwquality ocredit not set to -1"; fi
  if grep -q '^difok = 4' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-difok" "PASS"
  else record "pwquality-difok" "FAIL" "pwquality difok not set to 4"; fi
  if grep -q '^maxrepeat = 3' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-maxrepeat" "PASS"
  else record "pwquality-maxrepeat" "FAIL" "pwquality maxrepeat not set to 3"; fi
  if grep -q '^enforce_for_root' /etc/security/pwquality.conf 2>/dev/null; then record "pwquality-enforce-root" "PASS"
  else record "pwquality-enforce-root" "FAIL" "pwquality enforce_for_root not set (CIS 5.3.4)"; fi

  # sudoers hardening drop-in (mode 0440 — unreadable by non-root; WARN not FAIL)
  if [[ "$EUID" -eq 0 ]]; then
    if grep -qE '^Defaults[[:space:]].*use_pty' /etc/sudoers.d/99-hardening 2>/dev/null && \
       grep -qE '^Defaults[[:space:]].*logfile=' /etc/sudoers.d/99-hardening 2>/dev/null && \
       grep -qE '^Defaults[[:space:]].*umask=' /etc/sudoers.d/99-hardening 2>/dev/null; then
      record "sudoers-hardening" "PASS"
    else record "sudoers-hardening" "FAIL" "sudoers hardening drop-in missing or incomplete"; fi
  else record "sudoers-hardening" "WARN" "skipped — /etc/sudoers.d/ is mode 0440 (run with sudo for full check)"; fi

  # pwhistory remember=24 (CIS 5.3.5)
  if grep -q '^remember = 24' /etc/security/pwhistory.conf 2>/dev/null; then record "pwhistory-remember" "PASS"
  else record "pwhistory-remember" "FAIL" "pwhistory remember not set to 24"; fi

  # yescrypt password hashing (CIS 5.3.6)
  if grep -q '^ENCRYPT_METHOD YESCRYPT' /etc/login.defs 2>/dev/null; then record "yescrypt" "PASS"
  else record "yescrypt" "FAIL" "ENCRYPT_METHOD YESCRYPT not set in login.defs"; fi

  # yescrypt cost factor (CIS 5.4.1) — use $ to avoid prefix match against e.g. 50
  if grep -qE '^YESCRYPT_COST_FACTOR 5$' /etc/login.defs 2>/dev/null; then record "yescrypt-cost" "PASS"
  else record "yescrypt-cost" "FAIL" "YESCRYPT_COST_FACTOR 5 not set in login.defs"; fi

  # fprintd masked (prevents fingerprint from bypassing faillock)
  if systemctl is-masked fprintd.service &>/dev/null; then record "fprintd-masked" "PASS"
  else record "fprintd-masked" "FAIL" "fprintd.service not masked (fingerprint can bypass faillock)"; fi

  # resolv.conf points to systemd-resolved stub (required for split DNS/MagicDNS)
  # Skipped on CSB — Ansible guard omits this because NM/VPN manages resolv.conf on corporate networks
  if [[ "$(readlink /etc/resolv.conf 2>/dev/null)" == "/run/systemd/resolve/stub-resolv.conf" ]]; then
    record "resolv-stub" "PASS"
  elif $CSB_HOST; then record "resolv-stub" "WARN" "skipped on CSB — NM/VPN manages resolv.conf on corporate networks"
  else record "resolv-stub" "WARN" "resolv.conf not symlinked to stub-resolv.conf"; fi

  # Basic DNS resolution (confirms DNS works regardless of DoT/DHCP source — critical on CSB)
  if getent hosts redhat.com &>/dev/null; then record "dns-resolves" "PASS"
  else record "dns-resolves" "FAIL" "DNS resolution failed for redhat.com"; fi

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

  # TMOUT session timeout (CIS 5.5.5) — verify readonly, numeric value, and <=900s upper bound
  _tmout_val=$(grep -oP '^readonly TMOUT=\K[0-9]+' /etc/profile.d/tmout.sh 2>/dev/null || echo "")
  if [[ -n "$_tmout_val" && "$_tmout_val" -gt 0 && "$_tmout_val" -le 900 ]]; then
    record "tmout" "PASS"
  elif [[ -z "$_tmout_val" ]]; then
    record "tmout" "FAIL" "tmout.sh missing, not readonly, or TMOUT not set"
  else
    record "tmout" "FAIL" "TMOUT=$_tmout_val exceeds CIS 5.5.5 maximum of 900s"
  fi

  # /tmp hardening (CIS 1.1.2.x) — noexec/nosuid/nodev all required
  _tmp_opts=$(findmnt -n -o OPTIONS /tmp 2>/dev/null || echo "")
  if echo "$_tmp_opts" | grep -q noexec && echo "$_tmp_opts" | grep -q nosuid && echo "$_tmp_opts" | grep -q nodev; then
    record "tmp-hardening" "PASS"
  else record "tmp-hardening" "FAIL" "/tmp missing hardening options: $_tmp_opts"; fi
  unset _tmp_opts
  _tmp_mode=$(stat -c '%a' /tmp 2>/dev/null || echo "?")
  if [[ "$_tmp_mode" == "1777" ]]; then record "tmp-sticky-bit" "PASS"
  else record "tmp-sticky-bit" "FAIL" "/tmp mode=$_tmp_mode expected 1777 (sticky bit)"; fi
  unset _tmp_mode

  # /dev/shm hardening (CIS 1.1.7.x) — noexec/nosuid/nodev all required
  _shm_opts=$(findmnt -n -o OPTIONS /dev/shm 2>/dev/null || echo "")
  if echo "$_shm_opts" | grep -q noexec && echo "$_shm_opts" | grep -q nosuid && echo "$_shm_opts" | grep -q nodev; then
    record "shm-hardening" "PASS"
  else record "shm-hardening" "FAIL" "/dev/shm missing hardening options: $_shm_opts"; fi
  unset _shm_opts

  # /var/tmp bind-mounted to /tmp with noexec (CIS 1.1.8)
  _vt_opts=$(findmnt -n -o OPTIONS /var/tmp 2>/dev/null || echo "")
  if echo "$_vt_opts" | grep -q bind && echo "$_vt_opts" | grep -q noexec; then record "var-tmp-bind" "PASS"
  else record "var-tmp-bind" "WARN" "/var/tmp not bind-mounted with noexec: $_vt_opts"; fi
  unset _vt_opts

  # kernel.core_pattern safety
  if [[ "$(sysctl -n kernel.core_pattern 2>/dev/null)" == "|/bin/false" ]]; then record "core-pattern" "PASS"
  else record "core-pattern" "FAIL" "kernel.core_pattern expected '|/bin/false' (pipe prefix required)"; fi

  # ctrl+alt+del disabled (physical security)
  if systemctl is-masked ctrl-alt-del.target &>/dev/null; then record "ctrl-alt-del-masked" "PASS"
  else record "ctrl-alt-del-masked" "FAIL" "ctrl-alt-del.target not masked"; fi

  # dnf-automatic
  timer="dnf-automatic.timer"
  command -v dnf5 &>/dev/null && timer="dnf5-automatic.timer"
  if systemctl is-enabled "$timer" &>/dev/null && systemctl is-active "$timer" &>/dev/null; then
    record "dnf-automatic" "PASS"
  elif systemctl is-enabled "$timer" &>/dev/null; then
    record "dnf-automatic" "WARN" "timer enabled but not active (reboot or: systemctl start $timer)"
  else record "dnf-automatic" "WARN" "timer not enabled"; fi

  # TLP power management (ThinkPad battery care)
  if systemctl is-enabled tlp.service &>/dev/null && systemctl is-active tlp.service &>/dev/null; then
    record "tlp-service" "PASS"
  elif systemctl is-enabled tlp.service &>/dev/null; then
    record "tlp-service" "WARN" "enabled but not active (reboot or: systemctl start tlp.service)"
  else record "tlp-service" "WARN" "tlp.service not enabled"; fi
  if [[ -f /etc/tlp.d/50-thinkpad.conf ]]; then record "tlp-config" "PASS"
  else record "tlp-config" "WARN" "ThinkPad TLP config not deployed (/etc/tlp.d/50-thinkpad.conf)"; fi
  # Battery charge threshold (ThinkPad sysfs — only present on supported hardware)
  if [[ -f /sys/class/power_supply/BAT0/charge_control_end_threshold ]]; then
    _bat_end=$(cat /sys/class/power_supply/BAT0/charge_control_end_threshold 2>/dev/null || echo "?")
    if [[ "$_bat_end" != "?" && "$_bat_end" -lt 100 ]] 2>/dev/null; then record "tlp-bat-threshold" "PASS"
    else record "tlp-bat-threshold" "WARN" "end threshold=$_bat_end (expected <100 for battery longevity)"; fi
    unset _bat_end
  fi
  if [[ -f /sys/class/power_supply/BAT0/charge_control_start_threshold ]]; then
    _bat_start=$(cat /sys/class/power_supply/BAT0/charge_control_start_threshold 2>/dev/null || echo "?")
    if [[ "$_bat_start" != "?" && "$_bat_start" -gt 0 && "$_bat_start" -lt 100 ]] 2>/dev/null; then
      record "tlp-bat-start-threshold" "PASS"
    else record "tlp-bat-start-threshold" "WARN" "start threshold=$_bat_start (expected >0 and <100)"; fi
    unset _bat_start
  fi

  # Chrome policies (verify key security settings, not just file existence)
  if python3 -c "
import json, sys
p = json.load(open('/etc/opt/chrome/policies/managed/security.json'))
assert p.get('ExtensionInstallBlocklist') == ['*'], 'ExtensionInstallBlocklist not [\"*\"]'
assert p.get('RemoteDebuggingAllowed') is False, 'RemoteDebuggingAllowed not false'
assert p.get('SafeBrowsingProtectionLevel', 0) >= 1, 'SafeBrowsingProtectionLevel not >= 1'
" 2>/dev/null; then
    record "chrome-policies" "PASS"
  elif [[ -f /etc/opt/chrome/policies/managed/security.json ]]; then
    record "chrome-policies" "FAIL" "deployed but critical security policies missing or wrong"
  else record "chrome-policies" "WARN" "not deployed"; fi

  # Unexpected listening ports (non-loopback)
  listeners=$(ss -tulnp 2>/dev/null | grep -vE "127\.[0-9]+\.[0-9]+\.[0-9]+|::1" | tail -n +2 || true)
  if [[ -z "$listeners" ]]; then record "no-open-ports" "PASS"
  else record "no-open-ports" "WARN" "$(echo "$listeners" | wc -l) non-loopback listeners"; fi

  # login.defs password aging (CIS 5.4.x)
  if grep -qE '^PASS_MAX_DAYS[[:space:]]+365$' /etc/login.defs 2>/dev/null; then record "pass-max-days" "PASS"
  else record "pass-max-days" "FAIL" "PASS_MAX_DAYS not set to 365 in login.defs"; fi
  if grep -qE '^UMASK[[:space:]]+027$' /etc/login.defs 2>/dev/null; then record "umask-login-defs" "PASS"
  else record "umask-login-defs" "FAIL" "UMASK not set to 027 in login.defs"; fi
  if grep -qE '^INACTIVE[[:space:]]+30$' /etc/login.defs 2>/dev/null; then record "inactive-lock" "PASS"
  else record "inactive-lock" "FAIL" "INACTIVE not set to 30 in login.defs"; fi
  if grep -qE '^PASS_MIN_DAYS[[:space:]]+1$' /etc/login.defs 2>/dev/null; then record "pass-min-days" "PASS"
  else record "pass-min-days" "FAIL" "PASS_MIN_DAYS not set to 1 in login.defs"; fi
  if grep -qE '^PASS_WARN_AGE[[:space:]]+7$' /etc/login.defs 2>/dev/null; then record "pass-warn-age" "PASS"
  else record "pass-warn-age" "FAIL" "PASS_WARN_AGE not set to 7 in login.defs"; fi
  if grep -qE '^HOME_MODE[[:space:]]+0750$' /etc/login.defs 2>/dev/null; then record "home-mode" "PASS"
  else record "home-mode" "FAIL" "HOME_MODE not set to 0750 in login.defs (CIS: explicit home dir permissions)"; fi

  # WiFi MAC address randomization (privacy)
  if grep -q '^wifi.scan-rand-mac-address=yes' /etc/NetworkManager/conf.d/99-wifi-mac-rand.conf 2>/dev/null; then
    record "wifi-mac-rand" "PASS"
  else record "wifi-mac-rand" "WARN" "WiFi MAC randomization not configured"; fi

  # NM dns=systemd-resolved (required for Tailscale MagicDNS split-DNS)
  # Skipped on CSB — paired with resolv.conf guard; Ansible deliberately omits both on CSB (DHCP DNS remains active)
  if grep -q '^dns=systemd-resolved' /etc/NetworkManager/conf.d/99-dns.conf 2>/dev/null; then
    record "nm-dns-resolved" "PASS"
  elif $CSB_HOST; then record "nm-dns-resolved" "WARN" "skipped on CSB — DHCP DNS active via systemd-resolved without NM override"
  else record "nm-dns-resolved" "FAIL" "NM dns=systemd-resolved not configured (/etc/NetworkManager/conf.d/99-dns.conf)"; fi

  # NM wifi-powersave=2 (prevents latency spikes and drops on ThinkPad)
  if grep -q '^wifi.powersave=2' /etc/NetworkManager/conf.d/99-wifi-powersave.conf 2>/dev/null; then
    record "nm-wifi-powersave" "PASS"
  else record "nm-wifi-powersave" "WARN" "WiFi power saving not disabled (/etc/NetworkManager/conf.d/99-wifi-powersave.conf)"; fi

  # Console keymap
  if grep -q '^KEYMAP=us' /etc/vconsole.conf 2>/dev/null; then record "vconsole-keymap" "PASS"
  else record "vconsole-keymap" "WARN" "KEYMAP=us not set in /etc/vconsole.conf"; fi

  # logind IdleAction=lock (physical security)
  if grep -q '^IdleAction=lock' /etc/systemd/logind.conf.d/99-hardening.conf 2>/dev/null; then
    record "logind-idle-lock" "PASS"
  else record "logind-idle-lock" "FAIL" "logind IdleAction not set to lock"; fi

  # Session lingering (required for rootless podman.socket to survive provisioning SSH sessions)
  if loginctl show-user "${SUDO_USER:-$USER}" --property=Linger 2>/dev/null | grep -q "^Linger=yes"; then
    record "session-linger" "PASS"
  else record "session-linger" "FAIL" "linger not enabled — podman.socket dies when provisioning SSH session ends"; fi
  # Podman user socket (required for 'kind create cluster' via DOCKER_HOST — 'make kind' in OVN-K/Submariner)
  if [[ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" ]]; then
    record "podman-socket" "PASS"
  else record "podman-socket" "WARN" "Podman user socket not present — kind create cluster will fail (re-login or restart podman.socket)"; fi

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
  _sysctl_check "net.ipv4.tcp_timestamps"            "0" "sysctl-tcp-timestamps"
  _sysctl_check "net.ipv4.conf.all.accept_redirects" "0" "sysctl-no-accept-redirects"
  _sysctl_check "net.ipv4.conf.all.send_redirects"   "0" "sysctl-no-send-redirects"
  # bridge-nf: WARN if br_netfilter module not loaded (persistent via modules-load.d; reboot activates)
  _bridge_nf=$(sysctl -n net.bridge.bridge-nf-call-iptables 2>/dev/null)
  if [[ "$_bridge_nf" == "1" ]]; then record "sysctl-bridge-nf-iptables" "PASS"
  elif [[ -z "$_bridge_nf" ]]; then
    record "sysctl-bridge-nf-iptables" "WARN" "br_netfilter not loaded — reboot or: modprobe br_netfilter && sysctl --system"
  else record "sysctl-bridge-nf-iptables" "FAIL" "net.bridge.bridge-nf-call-iptables=$_bridge_nf expected 1"; fi
  _sysctl_check "net.ipv6.conf.all.forwarding"       "1" "sysctl-ipv6-forwarding"

  # vsyscall=none kernel param (ROP gadget mitigation, requires reboot after grubby)
  if grep -q 'vsyscall=none' /proc/cmdline 2>/dev/null; then record "vsyscall-none" "PASS"
  else record "vsyscall-none" "WARN" "vsyscall=none not in cmdline (requires reboot if grubby ran)"; fi
  # IOMMU kernel param (AMD DMA protection)
  if grep -q 'amd_iommu=on' /proc/cmdline 2>/dev/null; then record "amd-iommu" "PASS"
  else record "amd-iommu" "WARN" "amd_iommu=on not in cmdline (requires reboot; AMD only)"; fi
  # Memory safety kernel params
  if grep -q 'init_on_free=1' /proc/cmdline 2>/dev/null; then record "init-on-free" "PASS"
  else record "init-on-free" "WARN" "init_on_free=1 not in cmdline (requires reboot)"; fi
  if grep -q 'page_alloc.shuffle=1' /proc/cmdline 2>/dev/null; then record "page-alloc-shuffle" "PASS"
  else record "page-alloc-shuffle" "WARN" "page_alloc.shuffle=1 not in cmdline (requires reboot)"; fi
  # AMD CPU power driver (amd-pstate-epp is default on Fedora 44 + Zen 4)
  if [[ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver ]]; then
    _pstate=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver)
    if [[ "$_pstate" == "amd-pstate-epp" ]]; then record "amd-pstate-epp" "PASS"
    elif [[ "$_pstate" == "amd-pstate" ]]; then
      record "amd-pstate-epp" "WARN" "guided mode ($_pstate) not EPP — check BIOS CPPC setting"
    elif [[ "$_pstate" == "acpi-cpufreq" ]]; then
      record "amd-pstate-epp" "FAIL" "legacy acpi-cpufreq — kernel regression or BIOS CPPC disabled"
    else record "amd-pstate-epp" "WARN" "driver=$_pstate (unexpected)"; fi
  fi
  # amdgpu runtime PM (-1=auto is correct; 0=off wastes power)
  if [[ -f /sys/module/amdgpu/parameters/runpm ]]; then
    _runpm=$(cat /sys/module/amdgpu/parameters/runpm)
    if [[ "$_runpm" == "-1" ]]; then record "amdgpu-runpm-auto" "PASS"
    elif [[ "$_runpm" == "0" ]]; then record "amdgpu-runpm-auto" "FAIL" "runpm=0 disables dGPU PM"
    else record "amdgpu-runpm-auto" "WARN" "runpm=$_runpm — prefer -1 (ACPI auto)"; fi
  fi
  # Additional sysctl checks (CIS + hardening)
  _sysctl_check "fs.protected_hardlinks"              "1" "sysctl-protected-hardlinks"
  _sysctl_check "fs.protected_symlinks"               "1" "sysctl-protected-symlinks"
  _sysctl_check "fs.protected_fifos"                  "1" "sysctl-protected-fifos"
  _sysctl_check "fs.protected_regular"                "2" "sysctl-protected-regular"
  # sysrq: read expected value from deployed config (system_sysrq in config.yml may override default 0).
  # Hardcoding 0 here would false-FAIL on machines with system_sysrq: 176 (OVN-K kernel debugging).
  _sysrq_expected=$(awk -F' *= *' '/^kernel\.sysrq/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "kernel.sysrq" "${_sysrq_expected:-0}" "sysctl-sysrq-disabled"
  # kernel.panic: hardcoded 10 (reboot-after-panic); no standalone override variable in config.yml.
  _sysctl_check "kernel.panic"                        "10" "sysctl-panic-reboot"
  _sysctl_check "net.ipv6.conf.all.accept_ra"         "0" "sysctl-no-accept-ra"
  _sysctl_check "net.ipv4.conf.all.rp_filter"         "2" "sysctl-rp-filter"
  _sysctl_check "net.ipv4.conf.default.rp_filter"     "2" "sysctl-rp-filter-default"
  _sysctl_check "net.ipv4.conf.all.log_martians"      "1" "sysctl-log-martians"
  _sysctl_check "net.ipv4.ip_forward"                 "1" "sysctl-ip-forward"
  # nf_conntrack_max: module-gated sysctl — WARN if nf_conntrack not yet loaded, FAIL if loaded but wrong
  _nfct=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null)
  if [[ -z "$_nfct" ]]; then record "sysctl-conntrack-max" "WARN" "nf_conntrack module not loaded (net.netfilter.nf_conntrack_max unavailable)"
  elif [[ "$_nfct" -ge "131072" ]]; then record "sysctl-conntrack-max" "PASS"
  else record "sysctl-conntrack-max" "FAIL" "net.netfilter.nf_conntrack_max=$_nfct expected >=131072"; fi
  _sysctl_check "net.core.rmem_max"                    "16777216" "sysctl-rmem-max"
  _sysctl_check "net.core.wmem_max"                    "16777216" "sysctl-wmem-max"

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
