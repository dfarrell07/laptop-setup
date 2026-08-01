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

# SSH auth to GitHub (bypass run() — GitHub's success message is on stderr, which run() discards)
out=$(timeout 10 ssh -T git@github.com 2>&1 || true)
if echo "$out" | grep -q "successfully authenticated"; then
  record "github-ssh-auth" "PASS"
else
  record "github-ssh-auth" "WARN" "ssh auth unconfirmed"
fi

# Dev tool presence
# In a Molecule CI container (MOLECULE_PROJECT_DIRECTORY set), many tools are intentionally
# absent (binary downloads skipped via packages_install_binaries: false, claude_install_method:
# skip, platform-only packages like bpfman/strace/gh absent on apt).  Demote to WARN so the
# smoke run exits 0 and the assert in verify-smoke.yml passes.
_tool_absent="FAIL"
[[ -n "${MOLECULE_PROJECT_DIRECTORY:-}" ]] && _tool_absent="WARN"
for tool in "oc:oc version --client" "kubectl:kubectl version --client" "podman:podman info" "claude:claude --version" "gh:gh --version" "kind:kind version" "helm:helm version --short" "kustomize:kustomize version" "jq:jq --version" "tmux:tmux -V" "go:go version" "rg:rg --version" "fzf:fzf --version" "tc:tc -V" "strace:strace --version" "cosign:cosign version" "tkn:tkn version --component=cli" "bpfman:bpfman --version" "sops:sops --version" "bpftool:bpftool version" "operator-sdk:operator-sdk version" "k9s:k9s version" "krew:kubectl krew version"; do
  name="${tool%%:*}"; cmd="${tool#*:}"
  if run $cmd &>/dev/null; then record "$name" "PASS"; else record "$name" "$_tool_absent" "not found"; fi
done
unset _tool_absent

# ec CLI (work-profile only — guard on binary presence)
if [[ -x /usr/local/bin/ec ]]; then
  if run ec version &>/dev/null; then record "ec" "PASS"
  else record "ec" "FAIL" "ec version command failed (binary present but not functional)"; fi
fi

# GitHub CLI authenticated (skip when gh binary is absent — tools loop already records FAIL)
if command -v gh &>/dev/null; then
  if run gh auth status &>/dev/null 2>&1; then record "gh-auth" "PASS"
  else record "gh-auth" "WARN" "not authenticated (interactive login required)"; fi
fi

# YubiKey
if run ykman info &>/dev/null; then record "yubikey" "PASS"
else record "yubikey" "WARN" "not detected (plugged in?)"; fi

# Tailscale connectivity (cross-platform via CLI)
if run tailscale status &>/dev/null; then record "tailscale" "PASS"
else record "tailscale" "WARN" "tailscaled not running or VPN not established (check: systemctl status tailscaled)"; fi

# ssh-agent has a FIDO2 sk-ssh-ed25519 key loaded (use -L for full pubkey: -l shows ED25519-SK not sk-ssh-ed25519)
out=$(run ssh-add -L 2>&1 || true)
if echo "$out" | grep -q 'sk-ssh-ed25519'; then record "ssh-agent-key" "PASS"
elif [[ -n "$out" && "$out" != *"no identities"* && "$out" != *"Could not"* && "$out" != *"Error"* ]]; then
  record "ssh-agent-key" "WARN" "key loaded but not sk-ssh-ed25519 type"
else record "ssh-agent-key" "WARN" "no keys loaded in ssh-agent"; fi

# --- Editor checks ---
if command -v vim >/dev/null 2>&1; then record "vim-binary" "PASS"
else record "vim-binary" "WARN" "vim not found"; fi

# --- Dotfiles checks ---
for f in .zshrc .vimrc .bashrc; do
  if [[ ! -f "$HOME/$f" ]]; then record "dotfile-$f" "FAIL" "not deployed — run: make all"
  elif ! grep -q 'Ansible managed' "$HOME/$f"; then record "dotfile-$f" "FAIL" "$f present but not Ansible-managed — check for manual overwrite"
  else record "dotfile-$f" "PASS"; fi
done
_tmux_conf="$HOME/.config/tmux/tmux.conf"
if [[ ! -f "$_tmux_conf" ]]; then record "dotfile-.config/tmux/tmux.conf" "FAIL" "not deployed — run: make all"
elif ! grep -q 'Ansible managed' "$_tmux_conf"; then record "dotfile-.config/tmux/tmux.conf" "FAIL" "$_tmux_conf present but not Ansible-managed — check for manual overwrite"
else record "dotfile-.config/tmux/tmux.conf" "PASS"; fi
_gc="$HOME/.config/git/config"
if [[ ! -f "$_gc" ]]; then record "dotfile-gitconfig" "FAIL" "missing — run: make dotfiles"
elif ! grep -q 'Ansible managed' "$_gc"; then record "dotfile-gitconfig" "FAIL" "present but not Ansible-managed (manually overwritten?) — inspect and re-run: make dotfiles"
else record "dotfile-gitconfig" "PASS"; fi
unset _gc

# global gitignore — also referenced via core.excludesfile in gitconfig.j2 (belt-and-suspenders: XDG path is read automatically, explicit setting survives non-XDG git invocations)
_gi="$HOME/.config/git/ignore"
if grep -q "Ansible managed" "$_gi" 2>/dev/null; then record "dotfile-git-ignore" "PASS"
else record "dotfile-git-ignore" "FAIL" "not deployed or not Ansible-managed: $_gi"; fi
unset _gi

# ripgreprc (loaded via RIPGREP_CONFIG_PATH in zshrc)
_rg="$HOME/.config/ripgrep/config"
if grep -q "Ansible managed" "$_rg" 2>/dev/null; then record "dotfile-ripgreprc" "PASS"
else record "dotfile-ripgreprc" "FAIL" "not deployed or not Ansible-managed: $_rg"; fi
unset _rg

# ~/.cargo/bin in PATH (added by dotfiles role — required for Rust/bpfman toolchain)
if grep -q '\.cargo/bin' "$HOME/.zshrc" "$HOME/.bashrc" 2>/dev/null; then record "cargo-path" "PASS"
else record "cargo-path" "FAIL" "$HOME/.cargo/bin not in PATH exports (.zshrc/.bashrc) — Rust toolchain binaries unavailable"; fi

# direnv: hook and toml content
_dtf="$HOME/.config/direnv/direnv.toml"
if command -v direnv >/dev/null 2>&1; then
  if direnv hook zsh >/dev/null 2>&1; then record "direnv-hook-zsh" "PASS"
  else record "direnv-hook-zsh" "FAIL" "'direnv hook zsh' failed — direnv may be broken"; fi
else record "direnv-hook-zsh" "WARN" "direnv not found — skipping hook check"; fi
if [[ -f "$_dtf" ]]; then
  if grep -q 'strict_env = true' "$_dtf" && grep -q 'disable_stdin = true' "$_dtf"; then
    record "direnv-toml" "PASS"
  else record "direnv-toml" "FAIL" "$_dtf missing strict_env = true or disable_stdin = true — run: make dotfiles"; fi
else record "direnv-toml" "FAIL" "$_dtf not deployed — run: make dotfiles"; fi
unset _dtf

# environment.d containers.conf (KIND + Podman socket — pam_env injection for make kind)
if [[ "$(uname -s)" == "Linux" ]]; then
  _ecf="$HOME/.config/environment.d/containers.conf"
  if [[ -f "$_ecf" ]] && grep -q "DOCKER_HOST" "$_ecf" && grep -q "KIND_EXPERIMENTAL_PROVIDER" "$_ecf"; then
    record "env-d-containers" "PASS"
  elif [[ ! -f "$_ecf" ]]; then record "env-d-containers" "FAIL" "missing: $_ecf — 'make kind' will fail in OVN-K"
  else record "env-d-containers" "FAIL" "DOCKER_HOST or KIND_EXPERIMENTAL_PROVIDER missing in $_ecf"; fi
  unset _ecf
  _saf="$HOME/.config/environment.d/ssh-agent.conf"
  if [[ -f "$_saf" ]] && grep -q "SSH_AUTH_SOCK" "$_saf"; then record "env-d-ssh-agent" "PASS"
  elif [[ ! -f "$_saf" ]]; then record "env-d-ssh-agent" "FAIL" "missing: $_saf — SSH_AUTH_SOCK not set in systemd session"
  else record "env-d-ssh-agent" "FAIL" "SSH_AUTH_SOCK missing from $_saf"; fi
  unset _saf
  if [[ "$SSH_AUTH_SOCK" == */ssh-agent.socket ]]; then
    record "ssh-auth-sock" "PASS"
  else
    record "ssh-auth-sock" "WARN" "SSH_AUTH_SOCK=$SSH_AUTH_SOCK does not point to custom ssh-agent (expected .../ssh-agent.socket)"
  fi
  # subuid/subgid required for rootless Podman user namespaces (/etc/subuid is world-readable)
  # Skipped with --user-only or --container: requires system role (not run in container scenarios)
  if ! $USER_ONLY && [[ -z "$CONTAINER" ]]; then
    if grep -q "^${USER}:" /etc/subuid 2>/dev/null && grep -q "^${USER}:" /etc/subgid 2>/dev/null; then
      record "subuid-subgid" "PASS"
    else record "subuid-subgid" "FAIL" "subuid/subgid not configured for $USER — rootless Podman will fail with cryptic namespace errors"; fi
  fi
fi

# SSH config and permissions
if [[ -f "$HOME/.ssh/config" ]]; then
  perms=$(stat -c '%a' "$HOME/.ssh/config" 2>/dev/null || stat -f '%Lp' "$HOME/.ssh/config" 2>/dev/null)
  if [[ "$perms" == "600" ]]; then record "ssh-config" "PASS"
  else record "ssh-config" "FAIL" "permissions $perms, expected 600"; fi
  # SSH config content assertions
  if grep -q 'HashKnownHosts yes' "$HOME/.ssh/config"; then record "ssh-config-hash-known-hosts" "PASS"
  else record "ssh-config-hash-known-hosts" "FAIL" "HashKnownHosts yes missing from ~/.ssh/config — host list exposed in plaintext"; fi
  if grep -q 'StrictHostKeyChecking accept-new' "$HOME/.ssh/config"; then record "ssh-config-strict-host-key" "PASS"
  else record "ssh-config-strict-host-key" "FAIL" "StrictHostKeyChecking accept-new missing from ~/.ssh/config"; fi
  if grep -q 'ControlMaster auto' "$HOME/.ssh/config"; then record "ssh-config-control-master" "PASS"
  else record "ssh-config-control-master" "WARN" "ControlMaster auto missing from ~/.ssh/config — connection multiplexing not configured"; fi
  if ! grep -q 'hmac-sha2-512,' "$HOME/.ssh/config" 2>/dev/null || ! grep -q 'MACs' "$HOME/.ssh/config"; then record "ssh-config-no-non-etm-macs" "PASS"
  else record "ssh-config-no-non-etm-macs" "WARN" "non-ETM MAC (hmac-sha2-512,) found in ~/.ssh/config — use ETM variants only"; fi
else record "ssh-config" "FAIL" "$HOME/.ssh/config not deployed — run: make all (ssh role)"; fi

# SSH signing key file (required for git commit signing — deployed by ssh role from vault)
if [[ -f "$HOME/.ssh/id_ed25519_sk_signing.pub" ]]; then record "ssh-signing-key-file" "PASS"
else record "ssh-signing-key-file" "WARN" "$HOME/.ssh/id_ed25519_sk_signing.pub missing — git commit signing unavailable (YubiKey enrollment required or vault has no keys)"; fi

sshdir_perms=$(stat -c '%a' "$HOME/.ssh" 2>/dev/null || stat -f '%Lp' "$HOME/.ssh" 2>/dev/null || echo "?")
if [[ "$sshdir_perms" == "700" ]]; then record "ssh-dir-perms" "PASS"
elif [[ "$sshdir_perms" == "?" ]]; then record "ssh-dir-perms" "FAIL" "$HOME/.ssh/ directory not deployed"
else record "ssh-dir-perms" "FAIL" "permissions $sshdir_perms, expected 700"; fi

# authorized_keys: exclusive:true removes stale keys on every re-provision — verify count and mode.
# File absent is a WARN not FAIL: the authorized_key task is guarded by
# `when: ssh_auth_key_pub | length > 0`, so first provision with plaintext vault leaves no file.
_ak="$HOME/.ssh/authorized_keys"
if [[ ! -f "$_ak" ]]; then
  record "authorized-keys-exists" "WARN" "$_ak missing — vault may have no auth key (ssh_auth_key_pub empty) or ssh role not yet run"
else
  _ak_perms=$(stat -c '%a' "$_ak" 2>/dev/null || stat -f '%Lp' "$_ak" 2>/dev/null || echo "?")
  if [[ "$_ak_perms" == "600" ]]; then record "authorized-keys-perms" "PASS"
  else record "authorized-keys-perms" "FAIL" "permissions $_ak_perms, expected 600"; fi
  _ak_total=$(grep -cvE '^[[:space:]]*$|^#' "$_ak" 2>/dev/null || echo "0")
  if [[ "$_ak_total" -gt 1 ]]; then
    record "authorized-keys-exclusive" "FAIL" "$_ak_total keys present, expected 1 — exclusive:true drift or manual key added; inspect: cat $_ak"
  elif [[ "$_ak_total" -eq 0 ]]; then
    record "authorized-keys-exclusive" "WARN" "$_ak exists but has no key entries"
  elif grep -qE '^(sk-)?ssh-ed25519' "$_ak" 2>/dev/null; then
    record "authorized-keys-exclusive" "PASS"
  else
    record "authorized-keys-exclusive" "WARN" "one key present but not sk-ssh-ed25519 type — unexpected key type in $_ak"
  fi
fi
unset _ak _ak_perms _ak_total

homedir_perms=$(stat -c '%a' "$HOME" 2>/dev/null || stat -f '%Lp' "$HOME" 2>/dev/null || echo "?")
# CIS intent: home dir should be no MORE permissive than 750 (owner=7, group≤5, others=0)
# Modes like 710 are acceptable (more restrictive than 750 — group has execute only)
# macOS defaults to 755 and the system role that hardens this is Linux-only — skip on Darwin
if [[ "$(uname -s)" == "Darwin" ]]; then
  record "home-dir-perms" "PASS" "macOS default 755 accepted (system role is Linux-only)"
elif [[ "$homedir_perms" =~ ^[0-9]?7[0145]0$ ]]; then record "home-dir-perms" "PASS"
else record "home-dir-perms" "FAIL" "permissions $homedir_perms, expected ≤750 (CIS — owner full, group no-write, others none)"; fi

# --- Git security checks ---
for check in "core.fsmonitor=false" "safe.bareRepository=explicit" "commit.gpgsign=true" "tag.gpgsign=true" "gpg.format=ssh" "gpg.ssh.allowedSignersFile=~/.config/git/allowed_signers" "user.signingkey=~/.ssh/id_ed25519_sk_signing.pub"; do
  key="${check%%=*}" expected="${check#*=}"
  actual=$(run git config --global "$key" 2>/dev/null || echo "")
  if [[ "$actual" == "$expected" ]]; then record "git-$key" "PASS"
  else record "git-$key" "FAIL" "got '$actual', expected '$expected'"; fi
done

# git allowed_signers file (required for SSH commit verification)
_as="$HOME/.config/git/allowed_signers"
if [[ ! -f "$_as" ]]; then record "git-allowed-signers" "WARN" "file missing: $_as — deploy signing key from vault to generate this file"
elif [[ ! -s "$_as" ]]; then record "git-allowed-signers" "FAIL" "file is empty: $_as"
elif ! grep -qE '(sk-)?ssh-ed25519' "$_as"; then record "git-allowed-signers" "FAIL" "no sk-ssh-ed25519 key in $_as"
else record "git-allowed-signers" "PASS"; fi
unset _as

# git safe.directory should be empty
if dirs=$(run git config --global --get-all safe.directory 2>/dev/null) && [[ -n "$dirs" ]]; then
  record "git-safe-directory" "FAIL" "set: $dirs"
else record "git-safe-directory" "PASS"; fi

# git hooksPath configured
if hp=$(run git config --global core.hooksPath 2>/dev/null) && [[ -n "$hp" ]]; then
  record "git-hooks-path" "PASS"
else record "git-hooks-path" "FAIL" "not configured — expected core.hooksPath=.githooks; run: make hooks"; fi

# Claude Code sandbox enabled (use jq if available, fall back to grep)
# Security config is in settings.local.json (survives /config writes); fall
# back to settings.json for instances provisioned before this change.
for d in "$HOME/.claude" "$HOME/.claude-work" "$HOME/.claude-personal"; do
  # Prefer settings.local.json (Ansible-managed security policy); fall back to settings.json
  sec_file="$d/settings.local.json"
  [[ -f "$sec_file" ]] || sec_file="$d/settings.json"
  [[ -f "$sec_file" ]] || continue
  label="${d##*/}"
  if command -v jq &>/dev/null; then
    if jq -e '.sandbox.enabled' "$sec_file" &>/dev/null; then
      record "sandbox($label)" "PASS"
    else record "sandbox($label)" "FAIL" "sandbox not enabled in $sec_file — run: make all to re-deploy claude role"; fi
    if jq -e '.sandbox.failIfUnavailable' "$sec_file" &>/dev/null; then
      record "sandbox-failsafe($label)" "PASS"
    else record "sandbox-failsafe($label)" "FAIL" "sandbox.failIfUnavailable not true in $sec_file — run: make all to re-deploy claude role"; fi
    if jq -e '.sandbox.allowUnsandboxedCommands == false' "$sec_file" &>/dev/null; then
      record "sandbox-cmds($label)" "PASS"
    else record "sandbox-cmds($label)" "FAIL" "sandbox.allowUnsandboxedCommands not false in $sec_file — run: make all to re-deploy claude role"; fi
    if jq -e '.enableAllProjectMcpServers == false' "$sec_file" &>/dev/null; then
      record "mcp-disabled($label)" "PASS"
    else record "mcp-disabled($label)" "WARN" "enableAllProjectMcpServers not false"; fi
  else
    if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("sandbox",{}).get("enabled") else 1)' "$sec_file" 2>/dev/null; then
      record "sandbox($label)" "PASS"
    else record "sandbox($label)" "FAIL" "not enabled in $sec_file — run: make all to re-deploy claude role"; fi
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
  else record "chrome-policy-json" "FAIL" "invalid JSON in /etc/opt/chrome/policies/managed/security.json — inspect with: python3 -m json.tool /etc/opt/chrome/policies/managed/security.json"; fi
fi

# --- Notes repo (transcrypt-encrypted) ---
if [[ -d "$HOME/notes/.git" ]]; then
  record "notes-repo" "PASS"
  if command -v transcrypt &>/dev/null; then
    if (cd "$HOME/notes" && transcrypt --display) &>/dev/null 2>&1; then
      # Spot-check: verify decryption actually works — if the password is wrong,
      # smudge-filtered files remain as encrypted blobs (non-text) in the working tree
      _any_text=false
      while IFS= read -r _f; do
        if [[ "$(file -b "$HOME/notes/$_f" 2>/dev/null)" == *text* ]]; then
          _any_text=true; break
        fi
      done < <(git -C "$HOME/notes" ls-files 2>/dev/null | head -20)
      if $_any_text; then
        record "notes-transcrypt" "PASS"
      else
        record "notes-transcrypt" "FAIL" "transcrypt password incorrect or repo locked — no plaintext files found in working tree"
      fi
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
  elif [[ "${XDG_CURRENT_DESKTOP:-}" != "sway" ]]; then
    record "cliphist" "PASS"
  else record "cliphist" "FAIL" "not found (clipboard history broken in sway — check desktop_sway_packages)"; fi
  # wl-paste/wl-copy (wl-clipboard): installed for ALL desktops via packages_containers
  # (tmux copy-pipe Wayland clipboard chain) and additionally via desktop_sway_packages
  # (cliphist daemon + clipboard picker keybinding in sway). Not sway-only — a FAIL here
  # means wl-clipboard was not installed even from the common packages_containers list.
  if command -v wl-paste &>/dev/null; then record "wl-paste" "PASS"
  elif [[ -n "${MOLECULE_PROJECT_DIRECTORY:-}" ]]; then
    record "wl-paste" "WARN" "not found (expected in molecule — packages_containers overridden to [] in converge)"
  else record "wl-paste" "FAIL" "not found (wl-clipboard missing — tmux clipboard chain and cliphist daemon broken)"; fi
fi

# ---- System-level checks (skipped with --user-only, --container, or macOS) ----
IS_LINUX=true
[[ "$(uname -s)" == "Darwin" ]] && IS_LINUX=false

# CSB detection: mirrors csb_detect.yml -- two paths require BOTH conditions:
# Fedora CSB: FQDN ends in .csb AND Red Hat internal CA cert present
# RHEL CSB: fapolicyd is installed AND Red Hat internal CA cert present
# The CA cert check prevents false-positives on machines that have fapolicyd
# installed manually without being on a Red Hat corporate network.
_rh_ca="/etc/pki/ca-trust/source/anchors/2022-IT-Root-CA.pem"
CSB_HOST=false
if [[ -f "$_rh_ca" ]]; then
  [[ "$(hostname -f 2>/dev/null)" == *".csb" ]] && CSB_HOST=true
  systemctl list-unit-files fapolicyd.service 2>/dev/null | grep -q "fapolicyd" && CSB_HOST=true
fi
unset _rh_ca

if ! $USER_ONLY && [[ -z "$CONTAINER" ]] && $IS_LINUX; then

  # DNS-over-TLS: check config is deployed (not runtime negotiation — DoT is opportunistic so
  # +DNSOverTLS flag may be absent on port-853-blocked networks without indicating a problem)
  if [[ -f /etc/systemd/resolved.conf.d/99-dot.conf ]]; then
    if resolvectl status 2>/dev/null | grep -qE '\+DNSOverTLS'; then
      record "dns-over-tls" "PASS"
    else record "dns-over-tls" "WARN" "config deployed but TLS not negotiated on this network (opportunistic — plain DNS in use)"; fi
  elif $CSB_HOST; then
    record "dns-over-tls" "WARN" "not deployed — expected on CSB (Cloudflare blocked; DHCP DNS in use)"
  else record "dns-over-tls" "FAIL" "99-dot.conf not deployed — run: make system"; fi

  # ptrace scope
  val=$(sysctl -n kernel.yama.ptrace_scope 2>/dev/null || echo "?")
  if [[ "$val" == "1" ]]; then record "ptrace-scope" "PASS"
  else record "ptrace-scope" "FAIL" "=$val, expected 1"; fi

  # SELinux
  if command -v getenforce &>/dev/null; then
    se=$(getenforce 2>/dev/null || echo "?")
    if [[ "$se" == "Enforcing" ]]; then record "selinux" "PASS"
    else record "selinux" "FAIL" "$se, expected Enforcing — fix: sudo setenforce 1 && sudo sed -i 's/^SELINUX=.*/SELINUX=enforcing/' /etc/selinux/config"; fi
  fi

  # Kernel lockdown
  if [[ -f /sys/kernel/security/lockdown ]]; then
    ld=$(cat /sys/kernel/security/lockdown)
    if echo "$ld" | grep -q '\[integrity\]'; then record "kernel-lockdown" "PASS"
    else record "kernel-lockdown" "WARN" "lockdown not in integrity mode ($ld) — requires Secure Boot"; fi
  fi

  # kernel-cmdline persistence (new kernels inherit from /etc/kernel/cmdline)
  if [[ -f /etc/kernel/cmdline ]]; then
    _kcmd=$(cat /etc/kernel/cmdline)
    if echo "$_kcmd" | grep -q "vsyscall=none" && echo "$_kcmd" | grep -q "init_on_free=1"; then
      record "kernel-cmdline" "PASS"
    else record "kernel-cmdline" "WARN" "security params missing from /etc/kernel/cmdline — new kernels may lack hardening"; fi
    unset _kcmd
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
    if [[ "$_ssh_port" != '?' && "$_ssh_port" -ne 22 ]]; then record "sshd-port" "PASS"
    else record "sshd-port" "FAIL" "Port='$_ssh_port' expected non-default port !=22"; fi
    if command -v semanage &>/dev/null; then
      if semanage port -l 2>/dev/null | grep -qE "ssh_port_t.*\b${_ssh_port}\b"; then record "selinux-ssh-port" "PASS"
      else record "selinux-ssh-port" "FAIL" "port ${_ssh_port} not labeled ssh_port_t — sshd cannot bind"; fi
    fi
    if ss -tlnp 2>/dev/null | grep -q ":${_ssh_port}"; then record "sshd-port-bound" "PASS"
    else record "sshd-port-bound" "FAIL" "sshd not bound on port ${_ssh_port}"; fi
    # Verify sshd is enabled for reboot persistence — bound now but not enabled = reboot lockout
    if systemctl is-enabled sshd.service &>/dev/null; then record "sshd-enabled" "PASS"
    else record "sshd-enabled" "FAIL" "sshd.service not enabled — reboot will leave machine unreachable"; fi
    if [[ "$zone" == "drop" ]]; then
      # Only check drop-zone-specific rules when the drop zone is actually active
      if firewall-cmd --zone=drop --query-port="${_ssh_port}/tcp" &>/dev/null; then record "firewall-ssh-port" "PASS"
      else record "firewall-ssh-port" "FAIL" "port ${_ssh_port}/tcp not open in drop zone"; fi
    elif $CSB_HOST; then record "firewall-ssh-port" "WARN" "skipped on CSB — drop zone not active (IT manages zones)"
    else record "firewall-ssh-port" "FAIL" "port ${_ssh_port}/tcp not open in drop zone"; fi
    # Check permanent rule regardless of whether tailscale0 is up (catches post-snapshot regressions)
    if firewall-cmd --permanent --zone=trusted --query-interface=tailscale0 &>/dev/null; then
      record "firewall-tailscale-permanent" "PASS"
    else record "firewall-tailscale-permanent" "FAIL" "tailscale0 not in permanent trusted zone config"; fi
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
  else
    record "firewall-present" "FAIL" "firewall-cmd not found — firewalld not installed or not in PATH; all firewall checks skipped"
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
  if command -v usbguard &>/dev/null; then
    grep -q '^ImplicitPolicyTarget=block' /etc/usbguard/usbguard-daemon.conf 2>/dev/null || record 'usbguard-implicit-policy' 'FAIL' 'ImplicitPolicyTarget is not block — all unmatched devices may be allowed'
    grep -q '^AuditBackend=LinuxAudit' /etc/usbguard/usbguard-daemon.conf 2>/dev/null || record 'usbguard-audit-backend' 'FAIL' 'AuditBackend is not LinuxAudit — USBGuard events not forwarded to auditd'
    grep -q '^IPCAllowedGroups=wheel' /etc/usbguard/usbguard-daemon.conf 2>/dev/null || record 'usbguard-ipc-groups' 'FAIL' 'IPCAllowedGroups is not wheel — non-root users cannot manage USBGuard'
    grep -q '1050:' /etc/usbguard/rules.conf 2>/dev/null || record 'usbguard-yubikey-rule' 'FAIL' 'YubiKey whitelist rule missing from rules.conf'
    grep -q '3297:1969' /etc/usbguard/rules.conf 2>/dev/null || record 'usbguard-moonlander-rule' 'WARN' 'Moonlander whitelist rule missing from rules.conf'
    grep -q '0483:df11' /etc/usbguard/rules.conf 2>/dev/null || record 'usbguard-stm32-dfu-rule' 'WARN' 'STM32 DFU whitelist rule missing from rules.conf (needed for Moonlander firmware flashing)'
  fi

  # bpfman.socket enabled (socket-activated daemon — socket must be enabled for bpfman load/list to work)
  if command -v bpfman &>/dev/null; then
    if systemctl is-enabled bpfman.socket &>/dev/null && systemctl is-active bpfman.socket &>/dev/null; then
      record "bpfman-socket" "PASS"
    elif systemctl is-enabled bpfman.socket &>/dev/null; then
      record "bpfman-socket" "WARN" "bpfman.socket enabled but not active (first client connect will start it)"
    else record "bpfman-socket" "FAIL" "bpfman.socket not enabled — bpfman load/list will fail at runtime"; fi
  else record "bpfman-socket" "FAIL" "bpfman not installed"; fi

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
     grep -qP '^ClientAliveCountMax [1-9][0-9]?$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^HostKeyAlgorithms ssh-ed25519$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -qP '^AllowAgentForwarding (yes|no|local|remote)$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -qP '^AllowTcpForwarding (yes|no|local|remote)$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -q '^PermitUserEnvironment no$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     grep -qP '^MaxSessions [0-9]+$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null && \
     [[ "$_max_auth" != "?" && "$_max_auth" -le 4 ]]; then
    record "sshd-hardening" "PASS"
  elif [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    record "sshd-hardening" "FAIL" "sshd drop-in has wrong directives — check PasswordAuthentication/AllowForwarding/PermitUserEnvironment/HostKeyAlgorithms (MaxAuthTries=$_max_auth)"
  else record "sshd-hardening" "FAIL" "sshd drop-in not deployed"; fi
  # AllowUsers must contain the actual user — empty or 'root' would lock everyone out
  if [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    _allow_users=$(grep -oP '^AllowUsers \K.*' /etc/ssh/sshd_config.d/00-hardening.conf | tr -d ' ' || true)
    _expected_user="${SUDO_USER:-$USER}"
    if [[ "$_allow_users" == "$_expected_user" ]]; then record "sshd-allowusers" "PASS"
    else record "sshd-allowusers" "FAIL" "AllowUsers='$_allow_users' expected '$_expected_user'"; fi
    unset _allow_users _expected_user
  else record "sshd-allowusers" "FAIL" "sshd drop-in not deployed"; fi

  # auditd rules (verify immutability flag and sentinel watch rule; skipped on CSB — IT manages audit rules)
  if $CSB_HOST; then
    record "auditd-rules" "WARN" "skipped on CSB — audit rules managed by IT/SIEM pipeline"
  elif grep -q '^-e 2' /etc/audit/rules.d/claude-code.rules 2>/dev/null && \
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
  # Auditd watch keys for new paths deployed by the system role (skipped on CSB — IT manages rules)
  if ! $CSB_HOST; then
    for _key in power-config device-policy kernel-params kernel-modules logins kernel-module-load kernel-module-unload perm_mod bpfman-config crypto-policy user-mgmt aide-integrity mac-policy network-config; do
      if grep -q " -k ${_key}$" /etc/audit/rules.d/claude-code.rules 2>/dev/null; then
        record "auditd-watch-${_key}" "PASS"
      else record "auditd-watch-${_key}" "WARN" "watch key ${_key} missing from claude-code.rules"; fi
    done
  fi
  # AIDE monitoring of security-critical conf.d directories (verify lineinfile tasks applied)
  if [[ -f /etc/aide.conf ]]; then
    for _path in "/usr/local/bin" "/etc/ssh/sshd_config.d" "/etc/NetworkManager/conf.d" "/etc/systemd/resolved.conf.d" "/etc/systemd/logind.conf.d" "/etc/tlp.d" "/etc/tlp.conf" "/etc/crypto-policies" "/etc/selinux" "/etc/bpfman" "/etc/usbguard" "/etc/audit" "/etc/aide.conf" "/boot"; do
      label="aide-monitors-$(basename "$_path")"
      if grep -qF "$_path" /etc/aide.conf 2>/dev/null; then record "$label" "PASS"
      else record "$label" "WARN" "$_path not found in /etc/aide.conf"; fi
    done
  else record "aide-not-configured" "WARN" "/etc/aide.conf absent; run: aide --init && cp /var/lib/aide/aide.db.new.gz /var/lib/aide/aide.db.gz"; fi

  # kernel module blacklist (verify key always-present blacklist entries)
  if grep -q '^install cramfs /bin/false' /etc/modprobe.d/hardening.conf 2>/dev/null && \
     grep -q '^blacklist vivid' /etc/modprobe.d/hardening.conf 2>/dev/null && \
     grep -q '^blacklist n_hdlc' /etc/modprobe.d/hardening.conf 2>/dev/null; then
    record "modprobe-hardening" "PASS"
  else record "modprobe-hardening" "FAIL" "modprobe hardening not deployed or missing key blacklist entries"; fi
  # USB storage blacklist is conditional on system_disable_usb_storage (default: true)
  if grep -q '^blacklist usb_storage' /etc/modprobe.d/hardening.conf 2>/dev/null; then
    record "usb-storage-blocked" "PASS"
  else record "usb-storage-blocked" "WARN" "usb_storage not kernel-blocked — USB drives may mount (expected if system_disable_usb_storage: false)"; fi

  # core dump disabled (verify Storage=none not just file existence)
  if grep -q '^Storage=none' /etc/systemd/coredump.conf.d/disable.conf 2>/dev/null; then
    record "coredump-disabled" "PASS"
  else record "coredump-disabled" "FAIL" "coredump Storage=none not configured"; fi
  if grep -q '^ProcessSizeMax=0' /etc/systemd/coredump.conf.d/disable.conf 2>/dev/null; then
    record "coredump-processsizemax" "PASS"
  else record "coredump-processsizemax" "FAIL" "coredump ProcessSizeMax=0 not configured"; fi

  # journald persistent storage (verify Storage=persistent, not just file existence)
  # Skipped on CSB — Ansible omits journald config to avoid suppressing SIEM-forwarded events
  # (RateLimitBurst in the drop-in could drop audit events before audisp-remote ships them)
  if grep -q '^Storage=persistent' /etc/systemd/journald.conf.d/99-hardening.conf 2>/dev/null; then
    record "journald-persistent" "PASS"
  elif $CSB_HOST; then record "journald-persistent" "WARN" "skipped on CSB — journald config not deployed (IT may forward to SIEM; Ansible guard intentional)"
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

  # NFS server and rpcbind masked (CIS 2.2.7 — workstation must not run an NFS server)
  if systemctl is-masked nfs-server.service &>/dev/null; then record "nfs-server-masked" "PASS"
  else record "nfs-server-masked" "FAIL" "nfs-server.service not masked (workstation should not serve NFS, CIS 2.2.7)"; fi
  if systemctl is-masked rpcbind.service &>/dev/null; then record "rpcbind-masked" "PASS"
  else record "rpcbind-masked" "FAIL" "rpcbind.service not masked (required by nfs-server; mask both per CIS 2.2.7)"; fi
  # Cockpit web console masked (port 9090 reachable from Tailscale peers if unmasked)
  if systemctl is-masked cockpit.service &>/dev/null; then record "cockpit-service-masked" "PASS"
  else record "cockpit-service-masked" "FAIL" "cockpit.service not masked — port 9090 reachable from Tailscale peers"; fi
  if systemctl is-masked cockpit.socket &>/dev/null; then record "cockpit-socket-masked" "PASS"
  else record "cockpit-socket-masked" "FAIL" "cockpit.socket not masked — web console activation possible"; fi

  # AIDE file integrity — only check if aide-check.timer is deployed (skips cleanly when AIDE disabled)
  if systemctl list-unit-files aide-check.timer &>/dev/null 2>&1; then
    if systemctl is-enabled aide-check.timer &>/dev/null && systemctl is-active aide-check.timer &>/dev/null; then
      record "aide-timer" "PASS"
    elif systemctl is-enabled aide-check.timer &>/dev/null; then
      record "aide-timer" "WARN" "timer enabled but not active (reboot or: systemctl start aide-check.timer)"
    else record "aide-timer" "WARN" "timer not enabled"; fi
    if [[ -f /var/lib/aide/aide.db.gz ]]; then record "aide-db" "PASS"
    else record "aide-db" "WARN" "AIDE database not initialized (run: aide --init)"; fi
    if grep -q '^RestrictAddressFamilies=none$' /etc/systemd/system/aide-check.service 2>/dev/null; then
      record "aide-service-no-network" "PASS"
    elif [[ -f /etc/systemd/system/aide-check.service ]]; then
      record "aide-service-no-network" "FAIL" "RestrictAddressFamilies=none missing from aide-check.service (AIDE can make network calls)"
    else record "aide-service-no-network" "WARN" "aide-check.service not deployed"; fi
  fi  # (AIDE not deployed — system_aide_enabled: false; checks skipped)

  # Chrony NTS: first verify config, then verify actual NTS cookies established
  # (port 4460 is required for NTS-KE; may be blocked on CSB corporate networks)
  if grep -qE '^(pool|server|peer).*\bnts\b' /etc/chrony.conf 2>/dev/null; then
    if chronyc -c authdata 2>/dev/null | awk -F, '$5 > 0 {found=1} END {exit !found}'; then
      record "chrony-nts" "PASS"
    else record "chrony-nts" "WARN" "NTS configured but no authenticated sources (port 4460 blocked? needs boot?)"; fi
  elif $CSB_HOST; then record "chrony-nts" "WARN" "skipped on CSB — IT manages chrony.conf (Kerberos NTP)"
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
  # Skipped on CSB — login.defs not modified; IPA/SSSD + IT group policy governs password hashing
  if grep -q '^ENCRYPT_METHOD YESCRYPT$' /etc/login.defs 2>/dev/null; then record "yescrypt" "PASS"
  elif $CSB_HOST; then record "yescrypt" "WARN" "skipped on CSB — login.defs not modified; IPA/SSSD governs password policy"
  else record "yescrypt" "FAIL" "ENCRYPT_METHOD YESCRYPT not set in login.defs"; fi

  # yescrypt cost factor (CIS 5.4.1) — use $ to avoid prefix match against e.g. 50
  # Skipped on CSB — login.defs not modified; IPA/SSSD + IT group policy governs password hashing
  if grep -qE '^YESCRYPT_COST_FACTOR 5$' /etc/login.defs 2>/dev/null; then record "yescrypt-cost" "PASS"
  elif $CSB_HOST; then record "yescrypt-cost" "WARN" "skipped on CSB — login.defs not modified; IPA/SSSD governs password policy"
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
  # Skipped on CSB — katello-agent, Insights client, and IT monitoring run cron jobs under
  # non-root system users; cron.allow=root-only would silently break those IT management jobs
  if grep -qx 'root' /etc/cron.allow 2>/dev/null; then record "cron-allow-root" "PASS"
  elif $CSB_HOST; then record "cron-allow-root" "WARN" "skipped on CSB — IT monitoring agents use cron; cron.allow not restricted to root"
  else record "cron-allow-root" "WARN" "/etc/cron.allow missing or not restricted to root"; fi
  # at.allow restricts 'at' command to root only (CIS 5.1.9)
  if grep -qx 'root' /etc/at.allow 2>/dev/null; then record "at-allow-root" "PASS"
  elif $CSB_HOST; then record "at-allow-root" "WARN" "skipped on CSB — IT monitoring agents may use at; at.allow not restricted to root"
  else record "at-allow-root" "WARN" "/etc/at.allow missing or not restricted to root"; fi

  # Login banner deployed to /etc/issue (CIS 1.7.1)
  # Skipped on CSB — IT deploys a mandated corporate legal banner; Ansible guard intentionally
  # omits this task so the IT-managed banner is not overwritten
  if grep -qi 'authorized users' /etc/issue 2>/dev/null; then record "login-banner" "PASS"
  elif $CSB_HOST; then record "login-banner" "WARN" "skipped on CSB — IT deploys mandated legal banner; Ansible does not write /etc/issue"
  else record "login-banner" "FAIL" "login banner not deployed or missing expected text (/etc/issue)"; fi

  # Critical file permissions (CIS 6.1.x)
  shadow_mode=$(stat -c '%a' /etc/shadow 2>/dev/null || echo "?")
  if [[ "$shadow_mode" == "0" ]]; then record "shadow-perms" "PASS"
  else record "shadow-perms" "FAIL" "permissions $shadow_mode, expected 0000"; fi
  gshadow_mode=$(stat -c '%a' /etc/gshadow 2>/dev/null || echo "?")
  if [[ "$gshadow_mode" == "0" ]]; then record "gshadow-perms" "PASS"
  else record "gshadow-perms" "FAIL" "permissions $gshadow_mode, expected 0000"; fi

  # TMOUT session timeout (CIS 5.5.5) — verify numeric value <=900s in bash (profile.d) and zsh (/etc/zshrc)
  _tmout_val=$(grep -oP '^TMOUT=\K[0-9]+' /etc/profile.d/tmout.sh 2>/dev/null || echo "")
  if [[ -n "$_tmout_val" && "$_tmout_val" -gt 0 && "$_tmout_val" -le 900 ]]; then
    record "tmout-bash" "PASS"
  elif [[ -z "$_tmout_val" ]]; then
    record "tmout-bash" "FAIL" "tmout.sh missing or TMOUT not set (expected TMOUT<=900)"
  else
    record "tmout-bash" "FAIL" "TMOUT=$_tmout_val exceeds CIS 5.5.5 maximum of 900s"
  fi
  _tmout_zsh=$(grep -oP '^TMOUT=\K[0-9]+' /etc/zshrc 2>/dev/null || echo "")
  if [[ -n "$_tmout_zsh" && "$_tmout_zsh" -gt 0 && "$_tmout_zsh" -le 900 ]]; then
    record "tmout-zsh" "PASS"
  elif [[ -z "$_tmout_zsh" ]]; then
    record "tmout-zsh" "FAIL" "/etc/zshrc missing TMOUT (zsh sessions have no inactivity timeout)"
  else
    record "tmout-zsh" "FAIL" "TMOUT=$_tmout_zsh in /etc/zshrc exceeds CIS 5.5.5 maximum of 900s"
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
  # On CSB, fstab is IT-managed so Ansible deploys dev-shm-harden.service to re-apply
  # hardening on each boot. Verify the service is enabled — if it isn't, a reboot loses
  # nosuid/nodev/noexec and the shm-hardening check above would pass until next reboot.
  if $CSB_HOST; then
    if systemctl is-enabled dev-shm-harden.service &>/dev/null; then record "dev-shm-harden-enabled" "PASS"
    else record "dev-shm-harden-enabled" "FAIL" "dev-shm-harden.service not enabled on CSB — /dev/shm hardening lost on reboot"; fi
  fi

  # /var/tmp bind-mounted to /tmp with noexec (CIS 1.1.8)
  _vt_opts=$(findmnt -n -o OPTIONS /var/tmp 2>/dev/null || echo "")
  if echo "$_vt_opts" | grep -q bind && echo "$_vt_opts" | grep -q noexec; then record "var-tmp-bind" "PASS"
  else record "var-tmp-bind" "WARN" "/var/tmp not bind-mounted with noexec: $_vt_opts"; fi

  # ~/tmp must exist and allow exec (GOTMPDIR — go test compiles binaries here, /tmp is noexec)
  if [[ ! -d "$HOME/tmp" ]]; then
    record "home-tmp-dir" "FAIL" "$HOME/tmp does not exist — run: mkdir -p ~/tmp (or make dotfiles)"
  else
    _htmp_test=$(mktemp "$HOME/tmp/smoke-exec-XXXXXX" 2>/dev/null) || _htmp_test=""
    if [[ -z "$_htmp_test" ]]; then
      record "home-tmp-dir" "WARN" "$HOME/tmp exists but cannot create temp file"
    else
      # Guard cleanup with trap so set -e abort doesn't leak the temp file
      trap 'rm -f "$_htmp_test"' EXIT
      cp /bin/true "$_htmp_test" && chmod +x "$_htmp_test"
      if "$_htmp_test" 2>/dev/null; then record "home-tmp-dir" "PASS"
      else record "home-tmp-dir" "FAIL" "$HOME/tmp is noexec — go test ./... will fail"; fi
      rm -f "$_htmp_test"
      trap - EXIT
    fi
    unset _htmp_test
  fi
  unset _vt_opts

  # /home nosuid (CIS 1.1.9) — nosuid prevents setuid binaries copied into $HOME from gaining elevated privileges
  # Skipped on CSB — IT manages /home mount (may be NFS/autofs for LDAP users; remounting with nosuid may break access)
  if findmnt -n /home &>/dev/null; then
    _home_opts=$(findmnt -n -o OPTIONS /home 2>/dev/null || echo "")
    if echo "$_home_opts" | grep -q nosuid; then record "home-nosuid" "PASS"
    elif $CSB_HOST; then record "home-nosuid" "WARN" "skipped on CSB — IT manages /home mount (may be NFS/autofs); nosuid not applied"
    else record "home-nosuid" "FAIL" "/home is a separate mount but nosuid not set: $_home_opts"; fi
    unset _home_opts
  else record "home-nosuid" "WARN" "/home is not a separate mountpoint — nosuid cannot be set independently (expected on single-partition installs)"; fi

  # kernel.core_pattern safety — must begin with | (pipe to handler), never a raw path
  _core_pattern="$(sysctl -n kernel.core_pattern 2>/dev/null)"
  if [[ "$_core_pattern" == "|"* ]]; then record "core-pattern" "PASS"
  else record "core-pattern" "FAIL" "kernel.core_pattern='$_core_pattern' does not start with | (pipe handler required; raw core files expose data)"; fi

  # inotify limits (required for IDE/file-watcher tools — system role sets these)
  _inotify_watches=$(sysctl -n fs.inotify.max_user_watches 2>/dev/null || echo "0")
  if [[ "$_inotify_watches" -ge 524288 ]] 2>/dev/null; then record "inotify-max-user-watches" "PASS"
  else record "inotify-max-user-watches" "FAIL" "fs.inotify.max_user_watches=$_inotify_watches, expected >=524288 (run: make all)"; fi
  unset _inotify_watches
  _inotify_instances=$(sysctl -n fs.inotify.max_user_instances 2>/dev/null || echo "0")
  if [[ "$_inotify_instances" -ge 512 ]] 2>/dev/null; then record "inotify-max-user-instances" "PASS"
  else record "inotify-max-user-instances" "FAIL" "fs.inotify.max_user_instances=$_inotify_instances, expected >=512 (run: make all)"; fi
  unset _inotify_instances

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
  elif [[ -d /sys/class/power_supply/BAT0 ]]; then
    record "tlp-service" "FAIL" "tlp.service not enabled on laptop hardware (battery threshold protection absent)"
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

  # dconf system policies (CIS 1.8.3-1.8.8)
  # Source key files checked directly — no D-Bus session required for smoke tests.
  # Compiled databases are produced by 'dconf update'; absence means it never ran.
  if [[ -f /etc/dconf/db/local ]]; then record "dconf-db-local" "PASS"
  else record "dconf-db-local" "FAIL" "compiled database /etc/dconf/db/local missing (run: dconf update)"; fi
  # GDM database — WARN not FAIL: Sway uses greetd, not GDM
  if [[ -f /etc/dconf/db/gdm ]]; then record "dconf-db-gdm" "PASS"
  else record "dconf-db-gdm" "WARN" "/etc/dconf/db/gdm missing (GDM not installed? Sway/greetd systems not affected)"; fi
  # Media-handling automount/autorun (CIS 1.8.6-1.8.8)
  if grep -q '^automount=false' /etc/dconf/db/local.d/50-hardening 2>/dev/null && \
     grep -q '^autorun-never=true' /etc/dconf/db/local.d/50-hardening 2>/dev/null; then
    record "dconf-media-handling" "PASS"
  elif [[ -f /etc/dconf/db/local.d/50-hardening ]]; then
    record "dconf-media-handling" "FAIL" "automount=false or autorun-never=true missing in /etc/dconf/db/local.d/50-hardening"
  else record "dconf-media-handling" "FAIL" "/etc/dconf/db/local.d/50-hardening not deployed"; fi
  # Media-handling policy locks (CIS 1.8.6-1.8.8)
  if grep -q '^/org/gnome/desktop/media-handling/automount$' /etc/dconf/db/local.d/locks/50-hardening 2>/dev/null; then
    record "dconf-media-locks" "PASS"
  else record "dconf-media-locks" "FAIL" "media-handling keys not locked in /etc/dconf/db/local.d/locks/50-hardening"; fi
  # Screensaver idle-delay + lock-enabled (CIS 1.8.4/1.8.5)
  if grep -q '^lock-enabled=true' /etc/dconf/db/local.d/51-screensaver 2>/dev/null && \
     grep -q '^idle-delay=uint32 300' /etc/dconf/db/local.d/51-screensaver 2>/dev/null; then
    record "dconf-screensaver" "PASS"
  elif [[ -f /etc/dconf/db/local.d/51-screensaver ]]; then
    record "dconf-screensaver" "FAIL" "lock-enabled=true or idle-delay=uint32 300 missing in /etc/dconf/db/local.d/51-screensaver"
  else record "dconf-screensaver" "FAIL" "/etc/dconf/db/local.d/51-screensaver not deployed"; fi
  # Screensaver policy locks (CIS 1.8.5)
  if grep -q '^/org/gnome/desktop/screensaver/lock-enabled$' /etc/dconf/db/local.d/locks/51-screensaver 2>/dev/null && \
     grep -q '^/org/gnome/desktop/session/idle-delay$' /etc/dconf/db/local.d/locks/51-screensaver 2>/dev/null; then
    record "dconf-screensaver-locks" "PASS"
  else record "dconf-screensaver-locks" "FAIL" "screensaver/idle keys not locked in /etc/dconf/db/local.d/locks/51-screensaver"; fi
  # GDM login screen: hide user list (CIS 1.8.3) — WARN not FAIL: Sway uses greetd, not GDM
  if grep -q '^disable-user-list=true' /etc/dconf/db/gdm.d/03-hardening 2>/dev/null; then
    record "dconf-gdm-user-list" "PASS"
  elif [[ ! -f /etc/dconf/db/gdm.d/03-hardening ]]; then
    record "dconf-gdm-user-list" "WARN" "gdm.d/03-hardening not deployed (GDM not installed? Sway/greetd systems not affected)"
  else record "dconf-gdm-user-list" "FAIL" "disable-user-list=true missing in /etc/dconf/db/gdm.d/03-hardening"; fi

  # Unexpected listening ports (non-loopback)
  listeners=$(ss -tulnp 2>/dev/null | grep -vE "127\.[0-9]+\.[0-9]+\.[0-9]+|::1" | tail -n +2 || true)
  if [[ -z "$listeners" ]]; then record "no-open-ports" "PASS"
  else record "no-open-ports" "WARN" "$(echo "$listeners" | wc -l) non-loopback listeners"; fi

  # login.defs password aging (CIS 5.4.x)
  # Skipped on CSB — login.defs is not modified on CSB; IPA/SSSD + IT group policy governs local
  # accounts. INACTIVE=30 in particular could lock IT-managed service accounts that don't rotate
  # passwords, so Ansible deliberately skips all login.defs writes when csb_detected is true.
  if grep -qE '^PASS_MAX_DAYS[[:space:]]+365$' /etc/login.defs 2>/dev/null; then record "pass-max-days" "PASS"
  elif $CSB_HOST; then record "pass-max-days" "WARN" "skipped on CSB — login.defs not modified; IT group policy governs password aging"
  else record "pass-max-days" "FAIL" "PASS_MAX_DAYS not set to 365 in login.defs"; fi
  if grep -qE '^UMASK[[:space:]]+027$' /etc/login.defs 2>/dev/null; then record "umask-login-defs" "PASS"
  elif $CSB_HOST; then record "umask-login-defs" "WARN" "skipped on CSB — login.defs not modified"
  else record "umask-login-defs" "FAIL" "UMASK not set to 027 in login.defs"; fi
  if grep -qE '^INACTIVE[[:space:]]+30$' /etc/login.defs 2>/dev/null; then record "inactive-lock" "PASS"
  elif $CSB_HOST; then record "inactive-lock" "WARN" "skipped on CSB — INACTIVE not set (would lock IT-managed service accounts with non-rotating passwords)"
  else record "inactive-lock" "FAIL" "INACTIVE not set to 30 in login.defs"; fi
  # CIS 5.5.1.5: verify chage -I 30 was applied to the existing user account (not just new-account default).
  # login.defs INACTIVE=30 only governs future accounts created with useradd — the retroactive chage
  # updates /etc/shadow field 7 directly for the pre-existing user. Requires root to read /etc/shadow.
  if [[ "$EUID" -eq 0 ]]; then
    _chage_user="${SUDO_USER:-$USER}"
    _shadow_line=$(awk -F: -v u="$_chage_user" '$1==u' /etc/shadow 2>/dev/null)
    if [[ -z "$_shadow_line" ]]; then
      record "chage-inactive-user" "WARN" "no shadow entry for '$_chage_user' (SSSD/IPA domain account?)"
    else
      _inactive_val=$(awk -F: '{print $7}' <<< "$_shadow_line")
      if [[ "$_inactive_val" == "30" ]]; then record "chage-inactive-user" "PASS"
      elif $CSB_HOST; then record "chage-inactive-user" "WARN" "skipped on CSB — Ansible intentionally omits chage -I 30 (IPA krbPwdPolicy manages inactive lockout centrally)"
      else record "chage-inactive-user" "FAIL" "shadow INACTIVE='$_inactive_val' for '$_chage_user', expected 30 (CIS 5.5.1.5 — run: chage -I 30 $_chage_user)"; fi
    fi
    unset _chage_user _inactive_val _shadow_line
  else record "chage-inactive-user" "WARN" "skipped — reading /etc/shadow requires root (re-run with sudo for full check)"; fi
  if grep -qE '^PASS_MIN_DAYS[[:space:]]+1$' /etc/login.defs 2>/dev/null; then record "pass-min-days" "PASS"
  elif $CSB_HOST; then record "pass-min-days" "WARN" "skipped on CSB — login.defs not modified; IT group policy governs password aging"
  else record "pass-min-days" "FAIL" "PASS_MIN_DAYS not set to 1 in login.defs"; fi
  if grep -qE '^PASS_WARN_AGE[[:space:]]+7$' /etc/login.defs 2>/dev/null; then record "pass-warn-age" "PASS"
  elif $CSB_HOST; then record "pass-warn-age" "WARN" "skipped on CSB — login.defs not modified; IT group policy governs password aging"
  else record "pass-warn-age" "FAIL" "PASS_WARN_AGE not set to 7 in login.defs"; fi
  if grep -qE '^HOME_MODE[[:space:]]+0750$' /etc/login.defs 2>/dev/null; then record "home-mode" "PASS"
  elif $CSB_HOST; then record "home-mode" "WARN" "skipped on CSB — login.defs not modified"
  else record "home-mode" "FAIL" "HOME_MODE not set to 0750 in login.defs (CIS: explicit home dir permissions)"; fi

  # WiFi MAC address randomization (privacy)
  if grep -q '^wifi.scan-rand-mac-address=yes' /etc/NetworkManager/conf.d/99-wifi-mac-rand.conf 2>/dev/null; then
    record "wifi-mac-rand" "PASS"
  elif $CSB_HOST; then record "wifi-mac-rand" "WARN" "skipped on CSB — IT may use MAC-based NAC; stable-ssid not deployed"
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
  elif $CSB_HOST; then record "nm-wifi-powersave" "WARN" "not deployed on CSB — run 'make all' to apply; Intel WiFi may suffer latency spikes under load until then"
  else record "nm-wifi-powersave" "WARN" "WiFi power saving not disabled (/etc/NetworkManager/conf.d/99-wifi-powersave.conf)"; fi

  # NM unmanaged-devices for Tailscale (prevents NM from managing kind/OVN/Tailscale interfaces)
  if grep -q 'interface-name:tailscale' /etc/NetworkManager/conf.d/tailscale.conf 2>/dev/null; then
    record "nm-tailscale-unmanaged" "PASS"
  else record "nm-tailscale-unmanaged" "FAIL" "tailscale.conf missing or incomplete (/etc/NetworkManager/conf.d/tailscale.conf) — NM may manage kind/OVN/Tailscale interfaces; run: make all"; fi

  # Console keymap
  if grep -q '^KEYMAP=us$' /etc/vconsole.conf 2>/dev/null; then record "vconsole-keymap" "PASS"
  else record "vconsole-keymap" "WARN" "KEYMAP=us not set in /etc/vconsole.conf"; fi

  # logind IdleAction=lock (physical security)
  if grep -q '^IdleAction=lock' /etc/systemd/logind.conf.d/99-hardening.conf 2>/dev/null; then
    record "logind-idle-lock" "PASS"
  else record "logind-idle-lock" "FAIL" "logind IdleAction not set to lock — check /etc/systemd/logind.conf.d/99-hardening.conf; fix: make all"; fi
  if grep -q '^IdleActionSec=' /etc/systemd/logind.conf.d/99-hardening.conf 2>/dev/null; then
    record "logind-idle-sec" "PASS"
  else record "logind-idle-sec" "FAIL" "logind IdleActionSec not configured (idle-lock timeout undefined) — check /etc/systemd/logind.conf.d/99-hardening.conf; fix: make all"; fi

  # Session lingering (required for rootless podman.socket to survive provisioning SSH sessions)
  if loginctl show-user "${SUDO_USER:-$USER}" --property=Linger 2>/dev/null | grep -q "^Linger=yes"; then
    record "session-linger" "PASS"
  else record "session-linger" "FAIL" "linger not enabled — podman.socket dies when provisioning SSH session ends"; fi
  # Podman user socket (required for 'kind create cluster' via DOCKER_HOST — 'make kind' in OVN-K/Submariner)
  if [[ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" ]]; then
    record "podman-socket" "PASS"
  else record "podman-socket" "WARN" "Podman user socket not present — kind create cluster will fail (re-login or restart podman.socket)"; fi
  # Critical kernel sysctl values
  _sysctl_check() {
    local k="$1" v="$2" n="$3"; local got
    got=$(sysctl -n "$k" 2>/dev/null || echo "?")
    if [[ "$got" == "$v" ]]; then record "$n" "PASS"
    elif [[ "$got" == "?" && "$EUID" -ne 0 ]]; then
      record "$n" "WARN" "$k unreadable as non-root (re-run with sudo to verify value=$v)"
    else record "$n" "FAIL" "$k=$got expected $v"; fi
  }
  _sysctl_check "kernel.kptr_restrict"               "1" "sysctl-kptr-restrict"
  _sysctl_check "kernel.kexec_load_disabled"         "1" "sysctl-kexec-disabled"
  _sysctl_check "kernel.io_uring_disabled"           "1" "sysctl-io-uring-disabled"
  _sysctl_check "kernel.dmesg_restrict"              "1" "sysctl-dmesg-restrict"
  # unprivileged_bpf: 1=disabled(write-once), 2=disabled(resettable). Both are valid.
  # Integrity lockdown mode locks the value to 2 and makes it read-only (permission denied).
  _bpf_disabled=$(sysctl -n kernel.unprivileged_bpf_disabled 2>/dev/null) || true
  if [[ "$_bpf_disabled" -ge "1" ]] 2>/dev/null; then record "sysctl-bpf-restrict" "PASS"
  elif [[ -z "$_bpf_disabled" && "$EUID" -ne 0 ]]; then record "sysctl-bpf-restrict" "WARN" "unreadable as non-root"
  else record "sysctl-bpf-restrict" "FAIL" "kernel.unprivileged_bpf_disabled=$_bpf_disabled expected >=1"; fi
  _sysctl_check "kernel.perf_event_paranoid"         "2" "sysctl-perf-paranoid"
  # bpf_jit_harden: read expected from deployed config (system_bpf_jit_harden in config.yml may override default 2).
  _bpf_jit_harden_expected=$(awk -F' *= *' '/^net\.core\.bpf_jit_harden/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "net.core.bpf_jit_harden" "${_bpf_jit_harden_expected:-2}" "sysctl-bpf-jit-harden"
  _sysctl_check "kernel.randomize_va_space"          "2" "sysctl-aslr"
  _sysctl_check "fs.suid_dumpable"                   "0" "sysctl-suid-dumpable"
  _sysctl_check "net.ipv4.tcp_syncookies"            "1" "sysctl-syncookies"
  # tcp_timestamps: CIS=0, but CSB sets it to 1 (IT network diagnostics require timestamps)
  _ts=$(sysctl -n net.ipv4.tcp_timestamps 2>/dev/null || echo "?")
  if [[ "$_ts" == "0" ]]; then record "sysctl-tcp-timestamps" "PASS"
  elif $CSB_HOST && [[ "$_ts" == "1" ]]; then record "sysctl-tcp-timestamps" "WARN" "CSB: tcp_timestamps=1 (IT network diagnostics override CIS default 0)"
  else record "sysctl-tcp-timestamps" "FAIL" "net.ipv4.tcp_timestamps=$_ts expected 0 (or 1 on CSB)"; fi
  _sysctl_check "net.ipv4.conf.all.accept_redirects" "0" "sysctl-no-accept-redirects"
  _sysctl_check "net.ipv6.conf.all.accept_redirects" "0" "sysctl-no-accept-redirects-v6"
  _sysctl_check "net.ipv4.conf.all.send_redirects"   "0" "sysctl-no-send-redirects"
  _sysctl_check "net.ipv4.conf.all.accept_source_route" "0" "sysctl-no-source-route"
  _sysctl_check "net.ipv6.conf.all.accept_source_route" "0" "sysctl-no-source-route-v6"
  # bridge-nf: WARN if br_netfilter module not loaded (persistent via modules-load.d; reboot activates)
  _bridge_nf=$(sysctl -n net.bridge.bridge-nf-call-iptables 2>/dev/null) || true
  if [[ "$_bridge_nf" == "1" ]]; then record "sysctl-bridge-nf-iptables" "PASS"
  elif [[ -z "$_bridge_nf" ]]; then
    record "sysctl-bridge-nf-iptables" "WARN" "br_netfilter not loaded — reboot or: modprobe br_netfilter && sysctl --system"
  else record "sysctl-bridge-nf-iptables" "FAIL" "net.bridge.bridge-nf-call-iptables=$_bridge_nf expected 1"; fi
  _bridge_nf6=$(sysctl -n net.bridge.bridge-nf-call-ip6tables 2>/dev/null) || true
  if [[ "$_bridge_nf6" == "1" ]]; then record "sysctl-bridge-nf-ip6tables" "PASS"
  elif [[ -z "$_bridge_nf6" ]]; then
    record "sysctl-bridge-nf-ip6tables" "WARN" "br_netfilter not loaded — IPv6 NetworkPolicy enforcement broken for OVN-K"
  else record "sysctl-bridge-nf-ip6tables" "FAIL" "net.bridge.bridge-nf-call-ip6tables=$_bridge_nf6 expected 1"; fi
  _sysctl_check "net.ipv6.conf.all.forwarding"       "1" "sysctl-ipv6-forwarding"

  # vsyscall=none kernel param (ROP gadget mitigation, requires reboot after grubby)
  if grep -q 'vsyscall=none' /proc/cmdline 2>/dev/null; then record "vsyscall-none" "PASS"
  else record "vsyscall-none" "WARN" "vsyscall=none not in cmdline (requires reboot if grubby ran)"; fi
  # IOMMU kernel param (AMD DMA protection)
  if grep -q 'amd_iommu=on' /proc/cmdline 2>/dev/null; then record "amd-iommu" "PASS"
  else record "amd-iommu" "WARN" "amd_iommu=on not in cmdline (requires reboot; AMD only)"; fi
  if grep -q 'iommu=pt' /proc/cmdline 2>/dev/null; then record "iommu-pt" "PASS"
  else record "iommu-pt" "WARN" "iommu=pt not in cmdline (requires reboot; needed for IOMMU passthrough)"; fi
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
    elif [[ "$_runpm" == "0" ]]; then record "amdgpu-runpm-auto" "FAIL" "runpm=0 disables GPU runtime PM"
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
  # accept_ra: read expected value from deployed config (system_ipv6_accept_ra in config.yml may override default 0).
  _accept_ra_expected=$(awk -F' *= *' '/^net\.ipv6\.conf\.all\.accept_ra/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "net.ipv6.conf.all.accept_ra" "${_accept_ra_expected:-0}" "sysctl-no-accept-ra"
  _sysctl_check "net.ipv4.conf.all.rp_filter"         "2" "sysctl-rp-filter"
  _sysctl_check "net.ipv4.conf.default.rp_filter"     "2" "sysctl-rp-filter-default"
  _sysctl_check "net.ipv4.tcp_rfc1337"                "1" "sysctl-tcp-rfc1337"
  _sysctl_check "net.ipv4.conf.all.log_martians"      "1" "sysctl-log-martians"
  _sysctl_check "net.ipv4.ip_forward"                 "1" "sysctl-ip-forward"
  # nf_conntrack_max: module-gated sysctl — WARN if nf_conntrack not yet loaded, FAIL if loaded but wrong
  _nfct=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null) || true
  if [[ -z "$_nfct" ]]; then record "sysctl-conntrack-max" "WARN" "nf_conntrack module not loaded (net.netfilter.nf_conntrack_max unavailable)"
  elif [[ "$_nfct" -ge "524288" ]]; then record "sysctl-conntrack-max" "PASS"
  else record "sysctl-conntrack-max" "FAIL" "net.netfilter.nf_conntrack_max=$_nfct expected >=524288"; fi
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
