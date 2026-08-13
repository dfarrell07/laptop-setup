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
    -h|--help)   printf 'Usage: %s [--json] [--user-only] [--container <name>]\nExit: 0 = all pass, 1 = any failures\n' "$0"; exit 0 ;;
    *)           echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

IS_LINUX=true
[[ "$(uname -s)" == "Darwin" ]] && IS_LINUX=false

if [[ -t 1 ]] && ! $JSON; then
  P="\033[32m" W="\033[33m" F="\033[31m" R="\033[0m"
else
  P="" W="" F="" R=""
fi

declare -a RESULTS=()
FAILURES=0
WARNS=0

record() { # name status [detail]
  local n="$1" s="$2" d="${3:-}"
  local dj="${d//\\/\\\\}"; dj="${dj//$'\n'/\\n}"; dj="${dj//$'\t'/\\t}"
  RESULTS+=("$(printf '{"name":"%s","status":"%s","detail":"%s"}' "$n" "$s" "${dj//\"/\\\"}")")
  case "$s" in
    PASS) $JSON || printf "${P}PASS${R}  %s\n" "$n" ;;
    WARN) $JSON || printf "${W}WARN${R}  %s — %s\n" "$n" "$d"; WARNS=$((WARNS + 1)) ;;
    FAIL) $JSON || printf "${F}FAIL${R}  %s — %s\n" "$n" "$d"; FAILURES=$((FAILURES + 1)) ;;
  esac
}

# Detect profile from config.yml so work-only checks are correctly gated.
# 'profile' is an Ansible variable never exported to the shell; read it here.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
_cfg="$SCRIPT_DIR/../config.yml"
profile="work"
grep -qE '^profile:[[:space:]]*personal([[:space:]]|$)' "$_cfg" 2>/dev/null && profile="personal"
_system_umask=$(grep -oP '^system_umask:[[:space:]]*["'"'"']?\K[0-9]+' "$_cfg" 2>/dev/null || true)
_system_umask="${_system_umask:-027}"
_notes_enabled=false
grep -qE '^notes_enabled:[[:space:]]*true' "$_cfg" 2>/dev/null && _notes_enabled=true
_cfg_ssh_port=$(grep -oP '^ssh_port:[[:space:]]*\K[0-9]+' "$_cfg" 2>/dev/null || echo "722")
unset _cfg

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

_sysctl_check() {
  local k="$1" v="$2" n="$3"; local got
  got=$(sysctl -n "$k" 2>/dev/null || echo "?")
  if [[ "$got" == "$v" ]]; then record "$n" "PASS"
  elif [[ "$got" == "?" && "$EUID" -ne 0 ]]; then
    record "$n" "WARN" "$k unreadable as non-root (re-run with sudo to verify value=$v)"
  else record "$n" "FAIL" "$k=$got expected $v"; fi
}

# ---- User-level checks (always run) ----

# SSH auth to GitHub (bypass run() — GitHub's success message is on stderr, which run() discards)
_ssh_ret=0
timeout 10 ssh -T git@github.com &>/dev/null || _ssh_ret=$?
if [[ "$_ssh_ret" -eq 1 ]]; then
  record "github-ssh-auth" "PASS"
else
  record "github-ssh-auth" "WARN" "ssh auth unconfirmed (exit $_ssh_ret)"
fi
unset _ssh_ret

# Dev tool presence
# In a Molecule CI container (MOLECULE_PROJECT_DIRECTORY set), many tools are intentionally
# absent (binary downloads skipped via packages_install_binaries: false, claude_install_method:
# skip, platform-only packages like bpfman/strace/gh absent on apt).  Demote to WARN so the
# smoke run exits 0 and the assert in verify-smoke.yml passes.
_tool_absent="FAIL"
[[ -n "${MOLECULE_PROJECT_DIRECTORY:-}" ]] && _tool_absent="WARN"
for tool in "kubectl:kubectl version --client" "podman:podman info" "claude:claude --version" "gh:gh --version" "kind:kind version" "helm:helm version" "kustomize:kustomize version" "jq:jq --version" "tmux:tmux -V" "go:go version" "rg:rg --version" "fzf:fzf --version" "sops:sops --version" "k9s:k9s version" "transcrypt:transcrypt --version" "gitleaks:gitleaks version" "direnv:direnv --version"; do
  name="${tool%%:*}"; cmd="${tool#*:}"
  # shellcheck disable=SC2086  # intentional word-split: cmd is "binary arg1 arg2"
  if run $cmd &>/dev/null; then record "$name" "PASS"; else record "$name" "$_tool_absent" "not found"; fi
done
if [[ -x "$HOME/.krew/bin/krew" ]]; then
  if run "$HOME/.krew/bin/krew" version &>/dev/null; then record "krew" "PASS"
  else record "krew" "FAIL" "krew installed but version failed"; fi
else record "krew" "$_tool_absent" "not found"; fi
unset _tool_absent

# Go install tools (guard on binary presence — emit nothing when absent, FAIL when present-but-broken)
for tool in "gofumpt:--version" "gopls:version" "stern:--version" "govulncheck:-version" "gci:--version" "golangci-lint:--version" "subctl:version"; do
  name="${tool%%:*}"; args="${tool#*:}"; _gobin="$HOME/go/bin/$name"
  [[ -x "$_gobin" ]] && {
    # shellcheck disable=SC2086  # intentional word-split: args is a short arg list
    if run "$_gobin" $args &>/dev/null; then record "go-$name" "PASS"
    else record "go-$name" "FAIL" "$_gobin present but command failed"; fi
  }
done
# Versioned subctl binaries (subctl18, subctlRH* — guard on binary presence, emit nothing when absent)
for _bin in "$HOME"/.local/bin/subctl[0-9]* "$HOME"/.local/bin/subctlRH*; do
  if [[ -x "$_bin" ]]; then
    if run "$_bin" version &>/dev/null; then record "$(basename "$_bin")" "PASS"
    else record "$(basename "$_bin")" "FAIL" "$_bin present but version failed"; fi
  fi
done

if [[ -x /usr/local/bin/oc ]]; then
  if run oc version --client &>/dev/null; then record "oc" "PASS"
  else record "oc" "FAIL" "oc binary present but 'oc version --client' failed"; fi
  # kubectl comes from the same OCP tarball; if oc is present, kubectl must be present too
  if [[ ! -x /usr/local/bin/kubectl ]]; then
    record "kubectl-ocp" "FAIL" "/usr/local/bin/kubectl absent despite oc present — OCP tarball extraction incomplete"
  elif run /usr/local/bin/kubectl version --client &>/dev/null; then
    record "kubectl-ocp" "PASS"
  else
    record "kubectl-ocp" "FAIL" "OCP kubectl present but version check failed"
  fi
fi
# work-profile binaries (guard on presence, emit nothing when absent)
for _b in "cosign:cosign version" "tkn:tkn version --component=client" \
           "operator-sdk:operator-sdk version" "opm:opm version" "ec:ec version"; do
  _bname="${_b%%:*}"; _bvcmd="${_b#*:}"
  if [[ -x "/usr/local/bin/$_bname" ]]; then
    # shellcheck disable=SC2086
    if run $_bvcmd &>/dev/null; then record "$_bname" "PASS"
    else record "$_bname" "FAIL" "$_bname binary present but version command failed"; fi
  fi
done

# GitHub CLI authenticated (skip when gh binary is absent — tools loop already records FAIL)
if command -v gh &>/dev/null; then
  if run gh auth status &>/dev/null; then record "gh-auth" "PASS"
  else record "gh-auth" "WARN" "not authenticated (interactive login required)"; fi
fi

# registry.redhat.io authenticated (work-profile only — RH subctl oc image extract silently fails when unauth'd)
if [[ "$profile" == "work" ]] && command -v podman &>/dev/null && [[ -x /usr/local/bin/oc ]]; then
  if podman login --get-login registry.redhat.io &>/dev/null; then
    record "registry-redhat-auth" "PASS"
  else
    record "registry-redhat-auth" "WARN" "not authenticated — RH subctl oc image extract will silently fail (run: podman login registry.redhat.io)"
  fi
fi

# YubiKey
if run ykman info &>/dev/null; then record "yubikey" "PASS"
else record "yubikey" "WARN" "not detected (plugged in?)"; fi

# vault-pass.sh stub (skip in molecule — stub is intentional in CI)
if [[ -z "${MOLECULE_PROJECT_DIRECTORY:-}" ]]; then
  _vp="$SCRIPT_DIR/vault-pass.sh"
  if [[ -x "$_vp" ]]; then
    _vp_out=$(bash "$_vp" 2>/dev/null) || true
    if [[ "$_vp_out" == *ci-dummy-vault-password* ]]; then
      record "vault-pass-stub" "WARN" "vault-pass.sh is still the CI dummy stub — replace with YubiKey HMAC-SHA1 implementation before encrypting vault.yml (see SECURITY.md 'Setting Up vault-pass.sh')"
    else
      record "vault-pass-stub" "PASS"
    fi
    unset _vp_out
  fi
  unset _vp
fi

# Tailscale connectivity (cross-platform via CLI)
if ! command -v tailscale &>/dev/null; then
  record "tailscale" "WARN" "tailscale not installed (run: make packages)"
elif run tailscale status &>/dev/null; then record "tailscale" "PASS"
else record "tailscale" "WARN" "tailscaled not running or VPN not established (check: systemctl status tailscaled)"; fi

# ssh-agent has a FIDO2 sk-ssh-ed25519 key loaded (use -L for full pubkey: -l shows ED25519-SK not sk-ssh-ed25519)
out=$(ssh-add -L 2>&1 || true)
if [[ "$out" == *sk-ssh-ed25519* ]]; then record "ssh-agent-key" "PASS"
elif grep -qE 'Error connecting|Connection refused|Could not open' <<< "$out"; then
  record "ssh-agent-key" "WARN" "ssh-agent socket unreachable (SSH_AUTH_SOCK=${SSH_AUTH_SOCK:-<unset>} — log out and back in)"
elif [[ -n "$out" && "$out" != *"no identities"* && "$out" != *"Could not"* && "$out" != *"Error"* ]]; then
  record "ssh-agent-key" "WARN" "key loaded but not sk-ssh-ed25519 type"
else record "ssh-agent-key" "WARN" "no keys loaded in ssh-agent"; fi

# --- Editor checks ---
if command -v vim &>/dev/null; then record "vim-binary" "PASS"
else record "vim-binary" "WARN" "vim not found"; fi

# --- pipx tools (yamllint, ansible-lint — installed by packages role via pipx) ---
for tool in yamllint ansible-lint; do
  if command -v "$tool" &>/dev/null; then
    if run "$tool" --version &>/dev/null; then
      record "pipx-$tool" "PASS"
    else
      record "pipx-$tool" "FAIL" "$tool present but --version failed"
    fi
  else
    record "pipx-$tool" "WARN" "$tool not installed (pipx install may have failed — run: make packages)"
  fi
done

# --- Dotfiles checks ---
for f in .zshrc .vimrc .bashrc; do
  if [[ ! -f "$HOME/$f" ]]; then record "dotfile-$f" "FAIL" "not deployed — run: make dotfiles"
  elif ! grep -q 'Ansible managed' "$HOME/$f"; then record "dotfile-$f" "FAIL" "$f present but not Ansible-managed — inspect and re-run: make dotfiles"
  else record "dotfile-$f" "PASS"; fi
done
_tmux_conf="$HOME/.config/tmux/tmux.conf"
if [[ ! -f "$_tmux_conf" ]]; then record "dotfile-.config/tmux/tmux.conf" "FAIL" "not deployed — run: make dotfiles"
elif ! grep -q 'Ansible managed' "$_tmux_conf"; then record "dotfile-.config/tmux/tmux.conf" "FAIL" "$_tmux_conf present but not Ansible-managed — inspect and re-run: make dotfiles"
else record "dotfile-.config/tmux/tmux.conf" "PASS"; fi
unset _tmux_conf
_gc="$HOME/.config/git/config"
if [[ ! -f "$_gc" ]]; then record "dotfile-gitconfig" "FAIL" "missing — run: make dotfiles"
elif ! grep -q 'Ansible managed' "$_gc"; then record "dotfile-gitconfig" "FAIL" "present but not Ansible-managed (manually overwritten?) — inspect and re-run: make dotfiles"
else record "dotfile-gitconfig" "PASS"; fi
if [[ -f "$HOME/.gitconfig" ]]; then record "legacy-gitconfig" "FAIL" "~/.gitconfig exists alongside XDG config — run: make dotfiles"; else record "legacy-gitconfig" "PASS"; fi
# git identity must be set to non-placeholder values
_git_name=$(git config --global user.name 2>/dev/null || echo "")
_git_email=$(git config --global user.email 2>/dev/null || echo "")
if [[ -z "$_git_name" || "$_git_name" == "CHANGE_ME" ]]; then record "git-user-name" "FAIL" "git user.name='$_git_name' — set dotfiles_user_name in config.yml and re-run: make dotfiles"
else record "git-user-name" "PASS"; fi
if [[ -z "$_git_email" || "$_git_email" == "CHANGE_ME" ]]; then record "git-user-email" "FAIL" "git user.email='$_git_email' — set dotfiles_user_email_work/personal in config.yml and re-run: make dotfiles"
else record "git-user-email" "PASS"; fi
unset _gc _git_name _git_email
# git identity files and includeIf routing
_gc_work="$HOME/.config/git/config-work"
_gc_personal="$HOME/.config/git/config-personal"
if [[ ! -f "$_gc_work" ]]; then record "dotfile-gitconfig-work" "WARN" "~/.config/git/config-work missing — work identity not deployed; run: make dotfiles"
elif ! grep -q 'Ansible managed' "$_gc_work"; then record "dotfile-gitconfig-work" "WARN" "config-work present but not Ansible-managed — manual overwrite?"
else record "dotfile-gitconfig-work" "PASS"; fi
if [[ ! -f "$_gc_personal" ]]; then record "dotfile-gitconfig-personal" "WARN" "~/.config/git/config-personal missing — personal identity not deployed; run: make dotfiles"
elif ! grep -q 'Ansible managed' "$_gc_personal"; then record "dotfile-gitconfig-personal" "WARN" "config-personal present but not Ansible-managed — manual overwrite?"
else record "dotfile-gitconfig-personal" "PASS"; fi
if [[ "$profile" == "work" ]]; then
  if ! grep -q 'config-work' "$HOME/.config/git/config" 2>/dev/null; then
    record "gitconfig-includeif" "FAIL" "no includeIf referencing config-work in ~/.config/git/config — work identity routing absent; check dotfiles_work_src_dirs in config.yml and re-run: make dotfiles"
  else record "gitconfig-includeif" "PASS"; fi
else record "gitconfig-includeif" "PASS"; fi  # personal profile: dotfiles_work_src_dirs may be empty by design
unset _gc_work _gc_personal

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

# alacritty config (terminal emulator — only expected on Sway/i3 desktop machines)
_alc="$HOME/.config/alacritty/alacritty.toml"
if command -v sway &>/dev/null || command -v i3 &>/dev/null; then
  if [[ ! -f "$_alc" ]]; then record "dotfile-alacritty" "FAIL" "not deployed — run: make desktop"
  elif ! grep -q 'Ansible managed' "$_alc"; then record "dotfile-alacritty" "WARN" "$_alc present but not Ansible-managed"
  else
    record "dotfile-alacritty" "PASS"
    if ! grep -q 'xterm-256color' "$_alc"; then record "dotfile-alacritty-term" "WARN" "TERM=xterm-256color not set in $_alc — SSH into remote hosts may fail"; fi
  fi
fi
unset _alc

# i3-specific tool checks (only on i3 desktop machines)
if command -v i3 &>/dev/null; then
  for _i3t in \
    'playerctl:playerctl not installed — XF86Audio media keys non-functional in i3 (run: make desktop)' \
    'clipit:clipit not installed — clipboard contents lost on app close in i3 (run: make desktop)' \
    'wpctl:wpctl not installed — XF86AudioRaiseVolume/LowerVolume/Mute keybindings non-functional (run: make desktop)'; do
    _i3t_name="${_i3t%%:*}"; _i3t_msg="${_i3t#*:}"
    if command -v "$_i3t_name" &>/dev/null; then record "i3-${_i3t_name}" 'PASS'
    else record "i3-${_i3t_name}" 'FAIL' "$_i3t_msg"; fi
  done
fi
if command -v sway &>/dev/null || command -v i3 &>/dev/null; then
  if command -v meld &>/dev/null; then record "meld" "PASS"
  else record "meld" "WARN" "meld not installed — graphical diff/merge tool absent (run: make desktop)"; fi
fi

# GTK theme config (only on Sway desktop — no GNOME settings daemon)
if command -v sway &>/dev/null; then
  _gtk3="$HOME/.config/gtk-3.0/settings.ini"
  if [[ -f "$_gtk3" ]]; then record "gtk-3-settings" "PASS"
  else record "gtk-3-settings" "WARN" "GTK 3.0 settings.ini not deployed — GTK apps use system default theme in Sway"; fi
  unset _gtk3
  # Sway also uses wpctl for audio keybindings (same as i3 after cycle-13 alignment)
  if command -v wpctl &>/dev/null; then record "sway-wpctl" "PASS"
  else record "sway-wpctl" "FAIL" "wpctl not installed — XF86AudioRaiseVolume/LowerVolume/Mute keybindings non-functional in Sway (run: make desktop)"; fi
fi

# vimrc quality (termguicolors + background=dark for correct colors)
if command -v vim &>/dev/null; then
  if grep -q 'termguicolors' "$HOME/.vimrc" 2>/dev/null; then record "vimrc-termguicolors" "PASS"
  else record "vimrc-termguicolors" "WARN" "termguicolors not set in ~/.vimrc — 24-bit colors disabled"; fi
fi

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

# ~/.cargo/bin in PATH (added by dotfiles role — required for Rust/bpfman toolchain)
if grep -q '\.cargo/bin' "$HOME/.zshrc" "$HOME/.bashrc" 2>/dev/null; then record "cargo-path" "PASS"
else record "cargo-path" "FAIL" "$HOME/.cargo/bin not in PATH exports (.zshrc/.bashrc) — Rust toolchain binaries unavailable"; fi

# oh-my-zsh XDG path (dotfiles role clones to ~/.local/share/oh-my-zsh; legacy ~/.oh-my-zsh removed)
if [[ -f "$HOME/.local/share/oh-my-zsh/oh-my-zsh.sh" ]]; then record "omz-xdg-dir" "PASS"
else record "omz-xdg-dir" "FAIL" "~/.local/share/oh-my-zsh/oh-my-zsh.sh missing — zsh plugins unavailable; run: make dotfiles"; fi

# Required user directories (dotfiles + git_repos roles)
for _d in "bin:$HOME/bin" ".local-bin:$HOME/.local/bin" "src:$HOME/src"; do
  _name="${_d%%:*}"; _path="${_d#*:}"
  [[ -d "$_path" ]] && record "dir-$_name" 'PASS' || record "dir-$_name" 'FAIL' "$_path missing — run: make dotfiles repos"
done
unset _d _name _path


# direnv: hook and toml content
_dtf="$HOME/.config/direnv/direnv.toml"
if [[ -f "$_dtf" ]]; then
  if grep -q 'strict_env = true' "$_dtf" && grep -q 'disable_stdin = true' "$_dtf"; then
    record "direnv-toml" "PASS"
  else record "direnv-toml" "FAIL" "$_dtf missing strict_env = true or disable_stdin = true — run: make dotfiles"; fi
else record "direnv-toml" "FAIL" "$_dtf not deployed — run: make dotfiles"; fi
unset _dtf
_drc="$HOME/.config/direnv/direnvrc"
if [[ -f "$_drc" ]]; then
  if grep -q 'layout_go' "$_drc" 2>/dev/null; then record "direnvrc" "PASS"
  else record "direnvrc" "FAIL" "$_drc deployed but layout_go missing — run: make dotfiles"; fi
else record "direnvrc" "FAIL" "$_drc not deployed — run: make dotfiles"; fi
unset _drc

# environment.d containers.conf (KIND + Podman socket — pam_env injection for make kind)
if $IS_LINUX; then
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
  if [[ "${SSH_AUTH_SOCK:-}" == */ssh-agent.socket ]]; then
    record "ssh-auth-sock" "PASS"
  elif [[ -f "$HOME/.config/environment.d/ssh-agent.conf" ]] && \
       grep -q "SSH_AUTH_SOCK" "$HOME/.config/environment.d/ssh-agent.conf"; then
    record "ssh-auth-sock" "WARN" "SSH_AUTH_SOCK not yet updated — environment.d/ssh-agent.conf is deployed; log out and back in to activate (current: ${SSH_AUTH_SOCK:-<unset>})"
  else
    record "ssh-auth-sock" "WARN" "SSH_AUTH_SOCK=${SSH_AUTH_SOCK:-<unset>} does not point to custom ssh-agent (expected .../ssh-agent.socket) — run: make dotfiles"
  fi
  # subuid/subgid required for rootless Podman user namespaces (/etc/subuid is world-readable)
  # Skipped with --user-only or --container: requires system role (not run in container scenarios)
  if ! $USER_ONLY && [[ -z "$CONTAINER" ]]; then
    _subuid_user="${SUDO_USER:-$USER}"
    if grep -q "^${_subuid_user}:" /etc/subuid 2>/dev/null && grep -q "^${_subuid_user}:" /etc/subgid 2>/dev/null; then
      record "subuid-subgid" "PASS"
    else record "subuid-subgid" "FAIL" "subuid/subgid not configured for ${_subuid_user} — rootless Podman will fail with cryptic namespace errors"; fi
    unset _subuid_user
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
  if ! grep -q 'MACs' "$HOME/.ssh/config"; then record "ssh-config-no-non-etm-macs" "WARN" "MACs line absent — client uses OpenSSH defaults (may include non-ETM); run: make dotfiles"
  elif grep -qE 'hmac-sha2-(512|256)($|[^-])' "$HOME/.ssh/config"; then record "ssh-config-no-non-etm-macs" "WARN" "non-ETM MAC found in ~/.ssh/config MACs line — use ETM variants (hmac-sha2-512-etm@openssh.com, hmac-sha2-256-etm@openssh.com) only"
  else record "ssh-config-no-non-etm-macs" "PASS"; fi
else record "ssh-config" "FAIL" "$HOME/.ssh/config not deployed — run: make dotfiles"; fi

# SSH signing key file (required for git commit signing — deployed by ssh role from vault)
if [[ -f "$HOME/.ssh/id_ed25519_sk_signing.pub" ]]; then record "ssh-signing-key-file" "PASS"
else record "ssh-signing-key-file" "WARN" "$HOME/.ssh/id_ed25519_sk_signing.pub missing — git commit signing unavailable (YubiKey enrollment required or vault has no keys)"; fi

sshdir_perms=$(stat -c '%a' "$HOME/.ssh" 2>/dev/null || stat -f '%Lp' "$HOME/.ssh" 2>/dev/null || echo "?")
if [[ "$sshdir_perms" == "700" ]]; then record "ssh-dir-perms" "PASS"
elif [[ "$sshdir_perms" == "?" ]]; then record "ssh-dir-perms" "FAIL" "$HOME/.ssh/ directory not deployed"
else record "ssh-dir-perms" "FAIL" "permissions $sshdir_perms, expected 700"; fi

# authorized_keys: verify count and mode.
# File absent is a WARN not FAIL: the authorized_key task is guarded by
# `when: ssh_auth_key_pub | length > 0`, so first provision with plaintext vault leaves no file.
_ak="$HOME/.ssh/authorized_keys"
if [[ ! -f "$_ak" ]]; then
  record "authorized-keys-exists" "WARN" "$_ak missing — vault may have no auth key (ssh_auth_key_pub empty) or ssh role not yet run"
else
  _ak_perms=$(stat -c '%a' "$_ak" 2>/dev/null || stat -f '%Lp' "$_ak" 2>/dev/null || echo "?")
  if [[ "$_ak_perms" == "600" ]]; then record "authorized-keys-perms" "PASS"
  else record "authorized-keys-perms" "FAIL" "permissions $_ak_perms, expected 600"; fi
  _ak_total=$(grep -cvE '^[[:space:]]*$|^#' "$_ak" 2>/dev/null || true)
  if [[ "$_ak_total" -gt 1 ]]; then
    record "authorized-keys-exclusive" "WARN" "$_ak_total keys present, expected 1 — extra keys beyond vault key; inspect: cat $_ak"
  elif [[ "$_ak_total" -eq 0 ]]; then
    record "authorized-keys-exclusive" "WARN" "$_ak exists but has no key entries"
  elif grep -qE '^(sk-)?ssh-ed25519' "$_ak" 2>/dev/null; then
    record "authorized-keys-exclusive" "PASS"
  else
    record "authorized-keys-exclusive" "WARN" "one key present but not sk-ssh-ed25519 type — unexpected key type in $_ak"
  fi
fi
unset _ak _ak_perms _ak_total

_kh="$HOME/.ssh/known_hosts"
if [[ -f "$_kh" ]]; then
  _kh_perms=$(stat -c '%a' "$_kh" 2>/dev/null || stat -f '%Lp' "$_kh" 2>/dev/null || echo "?")
  if [[ "$_kh_perms" == "600" ]]; then record "known-hosts-perms" "PASS"
  else record "known-hosts-perms" "FAIL" "permissions $_kh_perms, expected 600 (HashKnownHosts hashes leak if world-readable)"; fi
  if ssh-keygen -F github.com -f "$_kh" &>/dev/null; then record 'known-hosts-github' 'PASS'
  else record 'known-hosts-github' 'WARN' 'github.com not in known_hosts — run: make ssh'; fi
fi
unset _kh _kh_perms

homedir_perms=$(stat -c '%a' "$HOME" 2>/dev/null || stat -f '%Lp' "$HOME" 2>/dev/null || echo "?")
# CIS intent: home dir should be no MORE permissive than 750 (owner=7, group≤5, others=0)
# Modes like 710 are acceptable (more restrictive than 750 — group has execute only)
# macOS defaults to 755 and the system role that hardens this is Linux-only — skip on Darwin
if ! $IS_LINUX; then
  record "home-dir-perms" "PASS" "macOS default 755 accepted (system role is Linux-only)"
elif [[ "$homedir_perms" =~ ^[0-9]?7[0145]0$ ]]; then record "home-dir-perms" "PASS"
elif [[ -f /etc/pki/ca-trust/source/anchors/RH-IT-Root-CA.pem || -f /etc/pki/ca-trust/source/anchors/2022-IT-Root-CA.pem || -f /etc/pki/ca-trust/source/anchors/Eng-CA.crt ]] \
     && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then
  record "home-dir-perms" "WARN" "RHEL CSB: IPA/oddjob-mkhomedir manages home perms ($homedir_perms); 0750 not enforced by Ansible (not csb_rhel guard)"
elif $USER_ONLY && grep -qiE '^ID(_LIKE)?=.*rhel' /etc/os-release 2>/dev/null; then
  record "home-dir-perms" "WARN" "RHEL-compatible in user-only mode: home perms ($homedir_perms) may be managed by csb_rhel path; system role skips chmod when csb_rhel=true"
else record "home-dir-perms" "FAIL" "permissions $homedir_perms, expected ≤750 (CIS — owner full, group no-write, others none)"; fi

# --- Git security checks ---
# Non-signing checks always FAIL if wrong. Signing checks downgrade to WARN when
# the signing key is absent (expected on first provision with plaintext vault).
_signing_key_present=false
[[ -f "$HOME/.ssh/id_ed25519_sk_signing.pub" ]] && _signing_key_present=true
for check in "core.fsmonitor=false" "safe.bareRepository=explicit" "commit.gpgsign=true" "tag.gpgsign=true" "gpg.format=ssh" "gpg.ssh.allowedSignersFile=~/.config/git/allowed_signers" "user.signingkey=~/.ssh/id_ed25519_sk_signing.pub" "protocol.allow=never" "protocol.https.allow=always" "protocol.ssh.allow=always" "push.autoSetupRemote=true" "fetch.prune=true" "alias.revert-s=revert -s" "core.excludesfile=~/.config/git/ignore"; do
  key="${check%%=*}" expected="${check#*=}"
  # Skip per-key signing checks when key absent — consolidated WARN emitted below
  if { [[ "$key" =~ ^(commit|tag|gpg)\. ]] || [[ "$key" == "user.signingkey" ]]; } && [[ "$_signing_key_present" == "false" ]]; then
    continue
  fi
  actual=$(run git config --global "$key" 2>/dev/null || echo "")
  if [[ "$actual" == "$expected" ]]; then record "git-$key" "PASS"
  else record "git-$key" "FAIL" "got '$actual', expected '$expected'"; fi
done
if [[ "$_signing_key_present" == "false" ]]; then
  record "git-signing-config" "WARN" "signing key absent — commit.gpgsign, tag.gpgsign, gpg.format, gpg.ssh.allowedSignersFile, user.signingkey skipped (populate vault + re-provision)"
fi
unset _signing_key_present

# git allowed_signers file (required for SSH commit verification)
_as="$HOME/.config/git/allowed_signers"
if [[ ! -f "$_as" ]]; then record "git-allowed-signers" "WARN" "file missing: $_as — deploy signing key from vault to generate this file"
elif [[ ! -s "$_as" ]]; then record "git-allowed-signers" "FAIL" "file is empty: $_as"
elif ! grep -qE '(sk-)?ssh-ed25519' "$_as"; then record "git-allowed-signers" "FAIL" "no sk-ssh-ed25519 key in $_as"
else record "git-allowed-signers" "PASS"; fi
unset _as

# git safe.directory must not contain dangerous wildcards (* ** /)
if dirs=$(run git config --global --get-all safe.directory 2>/dev/null | grep -E '^\*{1,2}$|^/$'); then
  record "git-safe-directory" "FAIL" "unsafe wildcard entries: $dirs"
else record "git-safe-directory" "PASS"; fi

# git hooksPath configured to correct path (git returns tilde-literal, not expanded $HOME)
_expected_hooks="~/.config/git/template/hooks"
hp=$(run git config --global core.hooksPath 2>/dev/null || echo "")
if [[ "$hp" == "$_expected_hooks" ]]; then
  record "git-hooks-path" "PASS"
else
  record "git-hooks-path" "FAIL" "core.hooksPath='${hp:-<unset>}' expected '$_expected_hooks' — run: make dotfiles"
fi
unset _expected_hooks

for _hook in pre-commit commit-msg prepare-commit-msg pre-push; do
  if [[ -x "$HOME/.config/git/template/hooks/$_hook" ]]; then
    record "git-hooks-$_hook" "PASS"
  else
    record "git-hooks-$_hook" "FAIL" "$_hook hook missing or not executable: $HOME/.config/git/template/hooks/$_hook — run: make dotfiles"
  fi
done

if command -v git-lfs &>/dev/null; then
  lfs_smudge=$(git config --global filter.lfs.smudge 2>/dev/null || echo "")
  if [[ -n "$lfs_smudge" ]]; then record "git-lfs-filter" "PASS"
  else record "git-lfs-filter" "FAIL" "filter.lfs.smudge not in global gitconfig — git lfs install --skip-repo has not run; run: make repos"; fi
fi

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
    else record "sandbox($label)" "FAIL" "sandbox not enabled in $sec_file — run: make claude"; fi
    if jq -e '.sandbox.failIfUnavailable' "$sec_file" &>/dev/null; then
      record "sandbox-failsafe($label)" "PASS"
    else record "sandbox-failsafe($label)" "FAIL" "sandbox.failIfUnavailable not true in $sec_file — run: make claude"; fi
    if jq -e '.sandbox.allowUnsandboxedCommands == false' "$sec_file" &>/dev/null; then
      record "sandbox-cmds($label)" "PASS"
    else record "sandbox-cmds($label)" "FAIL" "sandbox.allowUnsandboxedCommands not false in $sec_file — run: make claude"; fi
    if jq -e '.enableAllProjectMcpServers == false' "$sec_file" &>/dev/null; then
      record "mcp-disabled($label)" "PASS"
    else record "mcp-disabled($label)" "WARN" "enableAllProjectMcpServers not false"; fi
  else
    if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("sandbox",{}).get("enabled") else 1)' "$sec_file" 2>/dev/null; then
      record "sandbox($label)" "PASS"
    else record "sandbox($label)" "FAIL" "not enabled in $sec_file — run: make claude"; fi
  fi
done

# Claude directory permissions
for d in "$HOME/.claude" "$HOME/.claude-work" "$HOME/.claude-personal"; do
  [[ -d "$d" ]] || continue
  perms=$(stat -c '%a' "$d" 2>/dev/null || stat -f '%Lp' "$d" 2>/dev/null || echo "?")
  if [[ "$perms" == "700" ]]; then record "perms(${d##*/})" "PASS"
  else record "perms(${d##*/})" "FAIL" "permissions $perms, expected 700"; fi
done
_ctf="$HOME/.config/user-tmpfiles.d/claude-privacy.conf"
if $IS_LINUX; then
  if [[ -f "$_ctf" ]]; then record "claude-privacy-tmpfiles" "PASS"
  else record "claude-privacy-tmpfiles" "FAIL" "claude-privacy.conf tmpfiles not deployed — run: make claude"; fi
fi
unset _ctf

# Claude project CLAUDE.md presence
for _cd in "$HOME/.claude-personal" "$HOME/.claude-work"; do
  [[ -d "$_cd" ]] || continue
  label="${_cd##*/}"
  if [[ -f "$_cd/CLAUDE.md" ]]; then record "claude-md($label)" 'PASS'
  else record "claude-md($label)" 'FAIL' "CLAUDE.md missing from $_cd — run: make claude"; fi
done
unset _cd label

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
    if (cd "$HOME/notes" && transcrypt --display) &>/dev/null; then
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
  if $_notes_enabled; then
    record "notes-repo" "WARN" "notes repo not cloned — run 'make notes'"
  else
    record "notes-repo" "PASS" "notes disabled — set notes_enabled: true in config.yml to enable"
  fi
fi

# --- Wayland desktop tools (Linux only — not installed on macOS) ---
if $IS_LINUX; then
  # cliphist: clipboard history manager — exec wl-paste --watch cliphist store in sway config;
  # clipboard contents die with source app if this is missing
  if command -v cliphist &>/dev/null; then record "cliphist" "PASS"
  elif command -v sway &>/dev/null; then record "cliphist" "FAIL" "not found (clipboard history broken in sway — check desktop_sway_packages)"; fi
  # wl-paste/wl-copy (wl-clipboard): installed via desktop_sway_packages (sway desktop);
  # not present on non-sway Fedora. tmux copy-pipe benefits from it on any Wayland desktop,
  # so its absence on non-sway is a WARN rather than a hard failure.
  if command -v wl-paste &>/dev/null; then record "wl-paste" "PASS"
  elif [[ -n "${MOLECULE_PROJECT_DIRECTORY:-}" ]]; then
    record "wl-paste" "WARN" "not found (expected in molecule — desktop_sway_packages overridden to [] in converge)"
  elif ! command -v sway &>/dev/null; then
    record "wl-paste" "WARN" "not found (non-sway machine — wl-clipboard only required on Wayland/sway; see desktop_sway_packages)"
  else record "wl-paste" "FAIL" "not found (wl-clipboard missing — tmux clipboard chain and cliphist daemon broken)"; fi
  # swaylock config: deployed via desktop/tasks/main.yml copy task; without it swaylock
  # falls back to defaults (no show-failed-attempts, no indicator-caps-lock).
  # Guard on `command -v sway` (installed) rather than XDG_CURRENT_DESKTOP (active session)
  # so the FAIL is reachable from SSH/TTY — the recommended smoke-test environment per CLAUDE.md.
  if command -v sway &>/dev/null; then
    if [[ -f "$HOME/.config/sway/config" ]]; then
      if grep -q 'Ansible managed' "$HOME/.config/sway/config"; then
        record "sway-config" "PASS"
        if grep -q 'swayidle' "$HOME/.config/sway/config"; then record "sway-idle-lock" "PASS"
        else record "sway-idle-lock" "FAIL" "swayidle not configured in sway config — screen will not lock; run: make desktop"; fi
      else record "sway-config" "WARN" "present but not Ansible-managed"; fi
    else
      record "sway-config" "FAIL" "~/.config/sway/config not deployed — run: make desktop"
    fi
    if [[ -f "$HOME/.config/swaylock/config" ]]; then
      record "swaylock-config" "PASS"
    else
      record "swaylock-config" "FAIL" "~/.config/swaylock/config not deployed — run: make desktop"
    fi
  fi
fi

# kernel-cmdline persistence (new kernels inherit from /etc/kernel/cmdline)
# File is 0644 root-owned; no elevated privilege required. Guard makes this a
# no-op on macOS and Debian containers where the file is absent.
if [[ -f /etc/kernel/cmdline ]]; then
  _kcmd=$(<"/etc/kernel/cmdline")
  _kcmd_ok=true
  [[ "$_kcmd" == *"vsyscall=none"* ]]       || _kcmd_ok=false
  [[ "$_kcmd" == *"page_alloc.shuffle=1"* ]] || _kcmd_ok=false
  [[ "$_kcmd" == *"init_on_free=1"* ]]       || _kcmd_ok=false
  if grep -q "AuthenticAMD" /proc/cpuinfo 2>/dev/null; then
    [[ "$_kcmd" == *"amd_iommu=on"* ]]       || _kcmd_ok=false
  elif grep -q "GenuineIntel" /proc/cpuinfo 2>/dev/null; then
    [[ "$_kcmd" == *"intel_iommu=on"* ]]     || _kcmd_ok=false
  fi
  grep -q 'iommu=pt' /proc/cmdline 2>/dev/null && { [[ "$_kcmd" == *"iommu=pt"* ]] || _kcmd_ok=false; }
  if $_kcmd_ok; then record "kernel-cmdline" "PASS"
  else record "kernel-cmdline" "FAIL" "security params missing from /etc/kernel/cmdline — new kernels may lack hardening"; fi
  unset _kcmd _kcmd_ok
fi

# ---- System-level checks (skipped with --user-only, --container, or macOS) ----

# CSB detection: mirrors csb_detect.yml -- two paths require BOTH conditions:
# Fedora CSB: FQDN ends in .csb AND Red Hat internal CA cert present
# RHEL CSB: fapolicyd is installed AND Red Hat internal CA cert present
# The CA cert check prevents false-positives on machines that have fapolicyd
# installed manually without being on a Red Hat corporate network.
# Mirrors csb_detect.yml OR logic: any of the three RH CA cert files triggers.
CSB_HOST=false
_is_rhel=false
grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null && _is_rhel=true
_has_rh_cert=false
if [[ -f /etc/pki/ca-trust/source/anchors/2022-IT-Root-CA.pem ]] \
   || [[ -f /etc/pki/ca-trust/source/anchors/Eng-CA.crt ]] \
   || [[ -f /etc/pki/ca-trust/source/anchors/RH-IT-Root-CA.pem ]]; then
  _has_rh_cert=true
fi
if $_is_rhel; then
  # RHEL CSB: cert AND fapolicyd installed
  $_has_rh_cert && systemctl list-unit-files fapolicyd.service &>/dev/null && CSB_HOST=true
else
  # Fedora CSB: cert AND FQDN ends in .csb
  $_has_rh_cert && [[ "$(hostname -f 2>/dev/null)" == *".csb" ]] && CSB_HOST=true
fi
unset _is_rhel _has_rh_cert

if ! $USER_ONLY && [[ -z "$CONTAINER" ]] && $IS_LINUX; then

  _csb_non_fedora=false
  $CSB_HOST && ! grep -qiE '^ID=fedora' /etc/os-release 2>/dev/null && _csb_non_fedora=true

  # DNS-over-TLS: check config is deployed (not runtime negotiation — DoT is opportunistic so
  # +DNSOverTLS flag may be absent on port-853-blocked networks without indicating a problem)
  if [[ -f /etc/systemd/resolved.conf.d/99-dot.conf ]]; then
    if resolvectl status 2>/dev/null | grep -qE '\+DNSOverTLS'; then
      record "dns-over-tls" "PASS"
    else record "dns-over-tls" "WARN" "config deployed but TLS not negotiated on this network (opportunistic — plain DNS in use)"; fi
  elif $CSB_HOST; then
    record "dns-over-tls" "WARN" "not deployed — expected on CSB (Cloudflare blocked; DHCP DNS in use)"
  else record "dns-over-tls" "FAIL" "99-dot.conf not deployed — run: make system"; fi

  # ptrace scope — read expected value from deployed config (system_ptrace_scope defaults to 0 in default.config.yml)
  _ptrace_expected=$(awk -F' *= *' '/^kernel\.yama\.ptrace_scope/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "kernel.yama.ptrace_scope" "${_ptrace_expected:-0}" "sysctl-ptrace-scope"

  # SELinux
  if command -v getenforce &>/dev/null; then
    se=$(getenforce 2>/dev/null || echo "?")
    if [[ "$se" == "Enforcing" ]]; then record "selinux" "PASS"
    else record "selinux" "FAIL" "$se, expected Enforcing — fix: sudo setenforce 1 && sudo sed -i 's/^SELINUX=.*/SELINUX=enforcing/' /etc/selinux/config"; fi
  fi

  # Kernel lockdown (system_kernel_lockdown: '' in config.yml removes the param — silent skip then)
  if [[ -f /sys/kernel/security/lockdown ]]; then
    ld=$(<"/sys/kernel/security/lockdown")
    if grep -qE '\[integrity\]|\[confidentiality\]' <<< "$ld"; then record "kernel-lockdown" "PASS"
    elif grep -q 'lockdown=' /etc/kernel/cmdline 2>/dev/null; then
      record "kernel-lockdown" "WARN" "lockdown in /etc/kernel/cmdline but not active — reboot to activate"
    fi
    # Silent skip: lockdown= absent from /etc/kernel/cmdline means intentionally disabled
    # (system_kernel_lockdown: '' in config.yml) or CSB (IT manages boot config)
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
  # that's a latent foothold — a keyfile dropped there unlocks LUKS without passphrase.
  # UUID-device form (rd.luks.key=/path:UUID=...) means keyfile is on a separate device —
  # intentional and active; do not warn.
  if grep -q 'rd.luks.key' /proc/cmdline 2>/dev/null; then
    if [[ -f /boot/keyfile ]]; then
      record "luks-no-keyfile" "FAIL" "rd.luks.key in cmdline AND /boot/keyfile exists — LUKS key exposed on unencrypted /boot"
    elif grep -qE 'rd\.luks\.key=[^[:space:]:]+:(UUID=|/dev/)' /proc/cmdline; then
      record "luks-no-keyfile" "PASS"
    else
      record "luks-no-keyfile" "WARN" "stale rd.luks.key param in cmdline but no keyfile on /boot — clean up with grubby --remove-args"
    fi
  fi

  _ssh_port=""  # empty so ${_ssh_port:-$_cfg_ssh_port} at the listeners check substitutes 722 on non-root runs
  # Firewall default zone = drop, SSH port open, tailscale0 in trusted zone
  if command -v firewall-cmd &>/dev/null; then
    zone=$(firewall-cmd --get-default-zone 2>/dev/null || echo "?")
    if [[ "$zone" == "drop" ]]; then record "firewall-zone" "PASS"
    elif $CSB_HOST; then record "firewall-zone" "WARN" "zone='$zone' — IT manages zone policy on CSB; drop zone not applied"
    else record "firewall-zone" "FAIL" "'$zone', expected 'drop'"; fi
    _primary_iface=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')
    if [[ -n "$_primary_iface" && "$zone" == "drop" ]]; then
      _iface_zone=$(firewall-cmd --get-zone-of-interface="$_primary_iface" 2>/dev/null || true)
      if [[ "$_iface_zone" == "drop" ]]; then
        record "firewall-primary-iface-zone" "PASS"
      elif [[ -z "$_iface_zone" ]]; then
        # No explicit zone: interface inherits default zone (already confirmed drop) — PASS
        record "firewall-primary-iface-zone" "PASS"
      else
        record "firewall-primary-iface-zone" "FAIL" \
          "$_primary_iface in zone '$_iface_zone', expected 'drop' — NIC may use FedoraWorkstation rules despite default zone being drop"
      fi
      unset _iface_zone
    fi
    unset _primary_iface
    if [[ "$EUID" -ne 0 ]]; then
      record "sshd-config-readable" "WARN" "skipped — /etc/ssh/sshd_config.d/ requires root (re-run with sudo for full sshd checks)"
    else
      # Verify sshd_config loads the drop-in directory (absent Include = drop-in deployed but never read)
      if grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config 2>/dev/null; then record "sshd-include" "PASS"
      elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then record "sshd-include" "WARN" "sshd drop-in not loaded on RHEL CSB — IT manages sshd_config"
      else record "sshd-include" "FAIL" "/etc/ssh/sshd_config missing Include /etc/ssh/sshd_config.d/*.conf — 00-hardening.conf not loaded; run: make system"; fi
      _ssh_port=$(grep -oP '^Port \K[0-9]+' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null)
      if [[ -n "$_ssh_port" && "$_ssh_port" -ne 22 ]]; then record "sshd-port" "PASS"
      elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then record "sshd-port" "WARN" "sshd drop-in not deployed on RHEL CSB — IT manages sshd port (likely 22)"
      else record "sshd-port" "FAIL" "Port='${_ssh_port:-missing}' expected non-default port !=22 (config absent or port=22); run: make system (console/tmux, not over SSH)"; fi
      if [[ -n "$_ssh_port" ]] && command -v semanage &>/dev/null; then
        if semanage port -l 2>/dev/null | grep -qE "ssh_port_t.*\b${_ssh_port}\b"; then record "selinux-ssh-port" "PASS"
        else record "selinux-ssh-port" "FAIL" "port ${_ssh_port} not labeled ssh_port_t — sshd cannot bind"; fi
      fi
      if [[ -n "$_ssh_port" ]]; then
        if ss -tlnp 2>/dev/null | grep -q ":${_ssh_port}"; then record "sshd-port-bound" "PASS"
        else record "sshd-port-bound" "FAIL" "sshd not bound on port ${_ssh_port}"; fi
      fi
    fi
    if [[ "$zone" == "drop" ]]; then
      # Only check drop-zone-specific rules when the drop zone is actually active
      if firewall-cmd --zone=drop --query-port="${_ssh_port:-$_cfg_ssh_port}/tcp" &>/dev/null; then record "firewall-ssh-port" "PASS"
      else record "firewall-ssh-port" "FAIL" "port ${_ssh_port:-$_cfg_ssh_port}/tcp not open in drop zone"; fi
    elif $CSB_HOST; then record "firewall-ssh-port" "WARN" "skipped on CSB — drop zone not active (IT manages zones)"
    else record "firewall-ssh-port" "FAIL" "port ${_ssh_port:-$_cfg_ssh_port}/tcp not open in drop zone"; fi
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
      if firewall-cmd --zone=libvirt --query-service=ssh &>/dev/null; then
        record "firewall-libvirt-no-ssh" "FAIL" "ssh service in libvirt zone — VMs on virbr0 can reach host sshd"
      else record "firewall-libvirt-no-ssh" "PASS"; fi
    fi
  else
    if grep -qiE '^ID=debian' /etc/os-release 2>/dev/null; then
      record "firewall-present" "WARN" "firewalld not installed on Debian — apt systems use nftables without firewalld; firewall checks skipped"
    else
      record "firewall-present" "FAIL" "firewall-cmd not found — firewalld not installed or not in PATH; all firewall checks skipped"
    fi
  fi
  # Verify sshd is enabled for reboot persistence — bound now but not enabled = reboot lockout
  if systemctl is-enabled sshd.service &>/dev/null; then record "sshd-enabled" "PASS"
  else record "sshd-enabled" "FAIL" "sshd.service not enabled — reboot will leave machine unreachable"; fi
  # Verify sshd.socket masked — prevents socket activation from reopening port 22
  # Note: systemctl is-masked is not a valid verb in systemd 259; use show UnitFileState.
  if systemctl list-unit-files sshd.socket &>/dev/null; then
    if [[ "$(systemctl show -p UnitFileState --value sshd.socket 2>/dev/null)" == "masked" ]]; then
      record "sshd-socket-masked" "PASS"
    elif $CSB_HOST; then
      record "sshd-socket-masked" "WARN" "sshd.socket not masked on CSB — IT manages sshd; port-722 drop-in not deployed"
    else
      record "sshd-socket-masked" "FAIL" "sshd.socket not masked — socket activation can reopen port 22, bypassing 00-hardening.conf"
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
  elif grep -qiE '^ID=debian' /etc/os-release 2>/dev/null; then
    record "usbguard" "WARN" "skipped on Debian — Ansible USBGuard block is Fedora/RHEL-only (dnf/dnf5 required)"
  else record "usbguard" "FAIL" "not installed"; fi
  if command -v usbguard &>/dev/null; then
    if grep -q '^ImplicitPolicyTarget=block' /etc/usbguard/usbguard-daemon.conf 2>/dev/null; then record 'usbguard-implicit-policy' 'PASS'
    else record 'usbguard-implicit-policy' 'FAIL' 'ImplicitPolicyTarget is not block — all unmatched devices may be allowed'; fi
    if grep -q '^AuditBackend=LinuxAudit' /etc/usbguard/usbguard-daemon.conf 2>/dev/null; then record 'usbguard-audit-backend' 'PASS'
    else record 'usbguard-audit-backend' 'FAIL' 'AuditBackend is not LinuxAudit — USBGuard events not forwarded to auditd'; fi
    if grep -q '^IPCAllowedGroups=wheel' /etc/usbguard/usbguard-daemon.conf 2>/dev/null; then record 'usbguard-ipc-groups' 'PASS'
    else record 'usbguard-ipc-groups' 'FAIL' 'IPCAllowedGroups is not wheel — non-root users cannot manage USBGuard'; fi
    if grep -q '1050:' /etc/usbguard/rules.conf 2>/dev/null; then record 'usbguard-yubikey-rule' 'PASS'
    else record 'usbguard-yubikey-rule' 'FAIL' 'YubiKey whitelist rule missing from rules.conf'; fi
    if grep -q '3297:1969' /etc/usbguard/rules.conf 2>/dev/null; then record 'usbguard-moonlander-rule' 'PASS'
    else record 'usbguard-moonlander-rule' 'WARN' 'Moonlander whitelist rule missing from rules.conf'; fi
    if grep -q '0483:df11' /etc/usbguard/rules.conf 2>/dev/null; then record 'usbguard-stm32-dfu-rule' 'PASS'
    else record 'usbguard-stm32-dfu-rule' 'WARN' 'STM32 DFU whitelist rule missing from rules.conf (needed for Moonlander firmware flashing)'; fi
  fi

  # bpfman.socket enabled (socket-activated daemon — socket must be enabled for bpfman load/list to work)
  if command -v bpfman &>/dev/null; then
    if systemctl is-enabled bpfman.socket &>/dev/null && systemctl is-active bpfman.socket &>/dev/null; then
      record "bpfman-socket" "PASS"
    elif systemctl is-enabled bpfman.socket &>/dev/null; then
      record "bpfman-socket" "WARN" "bpfman.socket enabled but not active (first client connect will start it)"
    else record "bpfman-socket" "FAIL" "bpfman.socket not enabled — bpfman load/list will fail at runtime"; fi
  fi  # bpfman absent = not installed on this profile — no record emitted

  # tc/strace/bpftool (work-profile only — guard on binary presence, emit nothing when absent)
  for _t in "tc:-V" "strace:--version" "bpftool:version"; do
    _tname="${_t%%:*}"; _targ="${_t#*:}"
    if command -v "$_tname" &>/dev/null; then
      # shellcheck disable=SC2086
      if run "$_tname" $_targ &>/dev/null; then record "$_tname" "PASS"
      else record "$_tname" "FAIL" "$_tname binary present but $_targ failed"; fi
    fi
  done

  # auditd service enabled and running
  if systemctl is-active auditd &>/dev/null && systemctl is-enabled auditd &>/dev/null; then
    record "auditd-service" "PASS"
  else record "auditd-service" "FAIL" "auditd not active or not enabled"; fi

  # Crypto policy (skipped on CSB — IT may enforce FIPS/FUTURE; Ansible guard deliberately omits DEFAULT:NO-SHA1 there)
  if command -v update-crypto-policies &>/dev/null; then
    _cp=$(update-crypto-policies --show 2>/dev/null || echo "?")
    if [[ "$_cp" == "DEFAULT:NO-SHA1" ]]; then record "crypto-policy" "PASS"
    elif $CSB_HOST; then record "crypto-policy" "WARN" "skipped on CSB — policy is '$_cp' (IT-managed; DEFAULT:NO-SHA1 not applied)"
    else record "crypto-policy" "FAIL" "'$_cp', expected 'DEFAULT:NO-SHA1' (SHA1 accepted system-wide)"; fi
  fi

  # sshd hardening (verify key directives and value of MaxAuthTries ≤4)
  if [[ "$EUID" -ne 0 ]]; then
    record "sshd-hardening" "WARN" "skipped — /etc/ssh/sshd_config.d/ requires root"
    record "sshd-allowusers" "WARN" "skipped — requires root"
  else
    _max_auth=$(grep -oP '^MaxAuthTries \K[0-9]+' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null || echo "?")
    _bad_directive=""
    if [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
      while IFS=: read -r _f _d _p; do
        case "$_f" in
          P) grep -qP "^${_d} ${_p}$" /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null ;;
          *) grep -q "^${_d} ${_p}$" /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null ;;
        esac || { _bad_directive="$_d"; break; }
      done <<'EOF'
q:PasswordAuthentication:no
q:KbdInteractiveAuthentication:no
q:PermitRootLogin:no
q:PermitEmptyPasswords:no
q:X11Forwarding:no
P:ClientAliveCountMax:[1-9][0-9]?
P:ClientAliveInterval:[1-9][0-9]*
q:TCPKeepAlive:no
q:LogLevel:VERBOSE
q:HostKeyAlgorithms:ssh-ed25519
q:PubkeyAuthentication:yes
P:AllowAgentForwarding:no
P:AllowTcpForwarding:(no|local|remote|yes|all)
q:PermitUserEnvironment:no
P:MaxSessions:[1-9][0-9]*
q:MaxStartups:10:30:60
q:LoginGraceTime:30
q:HostbasedAuthentication:no
q:IgnoreRhosts:yes
q:GSSAPIAuthentication:no
q:AuthorizedKeysFile:.ssh/authorized_keys
EOF
      [[ "$_max_auth" != "?" && "$_max_auth" -le 4 ]] || _bad_directive="${_bad_directive:-MaxAuthTries}"
    fi
    if [[ -z "$_bad_directive" && -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
      record "sshd-hardening" "PASS"
    elif [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
      record "sshd-hardening" "FAIL" "${_bad_directive:-unknown} directive wrong or absent in sshd drop-in (MaxAuthTries=$_max_auth)"
    elif $CSB_HOST; then
      record "sshd-hardening" "WARN" "sshd drop-in not deployed on CSB — IT manages sshd; non-FIPS algorithms skipped"
    else record "sshd-hardening" "FAIL" "sshd drop-in not deployed"; fi
    # AllowUsers must contain the actual user — empty or 'root' would lock everyone out
    if [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
      _allow_users=$(grep -oP '^AllowUsers \K.*' /etc/ssh/sshd_config.d/00-hardening.conf | tr -d ' ' || true)
      _expected_user="${SUDO_USER:-$USER}"
      if [[ "$_allow_users" == "$_expected_user" ]]; then record "sshd-allowusers" "PASS"
      else record "sshd-allowusers" "FAIL" "AllowUsers='$_allow_users' expected '$_expected_user'"; fi
      unset _allow_users _expected_user
    elif $CSB_HOST; then
      record "sshd-allowusers" "WARN" "sshd drop-in not deployed on CSB — IT manages sshd"
    else record "sshd-allowusers" "FAIL" "sshd drop-in not deployed"; fi
  fi
  # Ciphers, MACs, KexAlgorithms, and PubkeyAcceptedAlgorithms must be present and hardened in the drop-in
  if [[ "$EUID" -ne 0 ]]; then
    record "sshd-algorithms" "WARN" "skipped — /etc/ssh/sshd_config.d/ requires root"
  elif [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    _bad_alg=""
    grep -q '^Ciphers aes256-gcm@openssh.com,chacha20-poly1305@openssh.com,aes256-ctr,aes192-ctr,aes128-ctr$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null || _bad_alg="${_bad_alg:-Ciphers}"
    grep -q '^MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null || _bad_alg="${_bad_alg:-MACs}"
    grep -qE '^KexAlgorithms.*(mlkem768x25519|curve25519)' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null || _bad_alg="${_bad_alg:-KexAlgorithms}"
    grep -q '^PubkeyAcceptedAlgorithms.*sk-ssh-ed25519@openssh.com' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null || _bad_alg="${_bad_alg:-PubkeyAcceptedAlgorithms}"
    if [[ -z "$_bad_alg" ]]; then record "sshd-algorithms" "PASS"
    else record "sshd-algorithms" "FAIL" "${_bad_alg} not hardened in sshd drop-in (check 00-hardening.conf)"; fi
  elif $CSB_HOST; then
    record "sshd-algorithms" "WARN" "sshd drop-in not deployed on CSB — IT manages sshd; FIPS rejects non-FIPS algorithms"
  else record "sshd-algorithms" "FAIL" "sshd drop-in not deployed"; fi

  # SSH Banner directive and /etc/issue.net content (NIST AC-8 login notice)
  if [[ "$EUID" -ne 0 ]]; then
    record "sshd-banner" "WARN" "skipped — /etc/ssh/sshd_config.d/ requires root"
  elif [[ -f /etc/ssh/sshd_config.d/00-hardening.conf ]]; then
    if $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then
      if grep -q '^Banner ' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null; then
        record "sshd-banner" "WARN" "Banner directive present on CSB — IT manages SSH banner; may conflict with corporate policy"
      else record "sshd-banner" "PASS"; fi
    else
      if grep -q '^Banner /etc/issue.net$' /etc/ssh/sshd_config.d/00-hardening.conf 2>/dev/null; then
        if [[ -s /etc/issue.net ]]; then record "sshd-banner" "PASS"
        else record "sshd-banner" "FAIL" "Banner directive present but /etc/issue.net is empty — NIST AC-8 login notice not shown; run: make system"; fi
      else record "sshd-banner" "FAIL" "Banner /etc/issue.net absent from sshd drop-in — NIST AC-8 login warning notice missing; run: make system"; fi
    fi
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then
    record "sshd-banner" "WARN" "sshd drop-in not deployed on CSB — IT manages SSH banner"
  else record "sshd-banner" "FAIL" "sshd drop-in not deployed"; fi

  # auditd rules (verify immutability flag and sentinel watch rule; skipped on RHEL CSB — IT manages audit rules)
  if $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then
    record "auditd-rules" "WARN" "skipped on RHEL CSB — audit rules managed by IT/SIEM pipeline"
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
  # Auditd watch keys for new paths deployed by the system role (skipped on RHEL CSB — IT manages rules)
  if ! $CSB_HOST || grep -qiE '^ID=fedora' /etc/os-release 2>/dev/null; then
    for _key in power-config device-policy kernel-params kernel-modules logins kernel-module-load kernel-module-unload perm_mod bpfman-config crypto-policy user-mgmt aide-integrity mac-policy network-config; do
      if grep -q " -k ${_key}$" /etc/audit/rules.d/claude-code.rules 2>/dev/null; then
        record "auditd-watch-${_key}" "PASS"
      else record "auditd-watch-${_key}" "WARN" "watch key ${_key} missing from claude-code.rules"; fi
    done
  fi
  if systemctl list-unit-files aide-check.timer &>/dev/null; then
    # AIDE monitoring of security-critical conf.d directories (verify lineinfile tasks applied)
    if [[ -f /etc/aide.conf ]]; then
      for _path in "/usr/local/bin" "/etc/ssh/sshd_config.d" "/etc/NetworkManager/conf.d" "/etc/systemd/resolved.conf.d" "/etc/systemd/logind.conf.d" "/etc/crypto-policies" "/etc/selinux" "/etc/bpfman" "/etc/usbguard" "/etc/audit" "/etc/aide.conf" "/boot" "/etc/sysctl.d" "/etc/kernel" "/etc/modprobe.d" "/etc/sudoers.d" "/etc/dconf" "/etc/systemd/system" "/etc/systemd/journald.conf.d" "/etc/systemd/coredump.conf.d"; do
        label="aide-monitors-$(basename "$_path")"
        if grep -qF "$_path" /etc/aide.conf 2>/dev/null; then record "$label" "PASS"
        else record "$label" "WARN" "$_path not found in /etc/aide.conf"; fi
      done
    else record "aide-not-configured" "WARN" "/etc/aide.conf absent — run: make system (enable system_aide_enabled: true in config.yml first); after deploy, initialise with: aide --init"; fi
    # TLP paths only added to aide.conf when TLP is enabled (system_tlp_enabled=true for laptops)
    if [[ -f /etc/tlp.conf ]]; then
      for _path in "/etc/tlp.d" "/etc/tlp.conf"; do
        label="aide-monitors-$(basename "$_path")"
        if grep -qF "$_path" /etc/aide.conf 2>/dev/null; then record "$label" "PASS"
        else record "$label" "WARN" "$_path not found in /etc/aide.conf — expected when system_tlp_enabled: true"; fi
      done
    fi
  fi

  # USB storage blacklist is conditional on system_disable_usb_storage (default: true)
  if grep -q '^blacklist usb_storage' /etc/modprobe.d/hardening.conf 2>/dev/null; then
    record "usb-storage-blocked" "PASS"
  else record "usb-storage-blocked" "WARN" "usb_storage not kernel-blocked — USB drives may mount (expected if system_disable_usb_storage: false)"; fi

  # coredump config — verify drop-in is deployed with a Storage= directive (default is external, not none)
  _cd_storage=$(grep -oP '^Storage=\K.*' /etc/systemd/coredump.conf.d/coredump.conf 2>/dev/null)
  if [[ -n "$_cd_storage" ]]; then record "coredump-configured" "PASS" "Storage=$_cd_storage"
  else record "coredump-configured" "FAIL" "coredump drop-in missing or Storage not set"; fi
  _cd_size=$(grep -oP '^ProcessSizeMax=\K.*' /etc/systemd/coredump.conf.d/coredump.conf 2>/dev/null)
  if [[ -n "$_cd_size" ]]; then record "coredump-processsizemax" "PASS" "ProcessSizeMax=$_cd_size"
  else record "coredump-processsizemax" "FAIL" "coredump ProcessSizeMax not configured"; fi
  unset _cd_storage _cd_size

  # journald persistent storage (verify Storage=persistent, not just file existence)
  # Skipped on CSB — Ansible omits journald config to avoid suppressing SIEM-forwarded events
  # (RateLimitBurst in the drop-in could drop audit events before audisp-remote ships them)
  if grep -q '^Storage=persistent' /etc/systemd/journald.conf.d/99-hardening.conf 2>/dev/null; then
    record "journald-persistent" "PASS"
  elif $CSB_HOST; then record "journald-persistent" "WARN" "skipped on CSB — journald config not deployed (IT may forward to SIEM; Ansible guard intentional)"
  else record "journald-persistent" "FAIL" "journald Storage=persistent not configured — run: make system"; fi
  if grep -q '^SystemMaxUse=4G' /etc/systemd/journald.conf.d/99-hardening.conf 2>/dev/null; then
    record "journald-maxuse" "PASS"
  elif $CSB_HOST; then record "journald-maxuse" "WARN" "skipped on CSB — journald config not deployed (IT may forward to SIEM; Ansible guard intentional)"
  else record "journald-maxuse" "FAIL" "journald SystemMaxUse=4G not configured — run: make system"; fi

  # cups-browsed masked (CVE-2024-47176 RCE vector)
  _cups_browsed_state=$(systemctl show -p UnitFileState --value cups-browsed.service 2>/dev/null)
  if [[ "$_cups_browsed_state" == "masked" ]]; then record "cups-browsed-masked" "PASS"
  elif [[ -z "$_cups_browsed_state" ]] && grep -qiE '^ID=debian' /etc/os-release 2>/dev/null; then
    record "cups-browsed-masked" "WARN" "cups-browsed not installed on Debian (cups-filters absent) — no CVE-2024-47176 exposure"
  else record "cups-browsed-masked" "FAIL" "not masked (state: '$_cups_browsed_state', CVE-2024-47176 RCE vector — must be masked)"; fi
  unset _cups_browsed_state

  # cups.service disabled; cups.socket and cups.path masked (prevent socket/path activation of CUPS)
  # Set system_disable_printing: false in config.yml to leave cups.socket unmasked for Flatpak print dialogs
  _cups_state=$(systemctl show -p UnitFileState --value cups.service 2>/dev/null)
  if [[ "$_cups_state" == "disabled" ]]; then record "cups-disabled" "PASS"
  else record "cups-disabled" "WARN" "cups.service state is '$_cups_state', expected 'disabled' (masked breaks Flatpak print)"; fi
  unset _cups_state
  if [[ "$(systemctl show -p UnitFileState --value cups.socket 2>/dev/null)" == "masked" ]]; then record "cups-socket-masked" "PASS"
  else record "cups-socket-masked" "WARN" "cups.socket not masked — socket activation can start CUPS despite cups.service being disabled (expected masked when system_disable_printing: true)"; fi
  if [[ "$(systemctl show -p UnitFileState --value cups.path 2>/dev/null)" == "masked" ]]; then record "cups-path-masked" "PASS"
  else record "cups-path-masked" "WARN" "cups.path not masked — path activation can start CUPS despite cups.service being disabled (expected masked when system_disable_printing: true)"; fi

  # avahi-daemon masked (or intentionally enabled via system_disable_avahi: false)
  _avahi_state=$(systemctl show -p UnitFileState --value avahi-daemon.service 2>/dev/null)
  if [[ "$_avahi_state" == "masked" ]]; then record "avahi-masked" "PASS"
  elif [[ "$_avahi_state" == "enabled" || "$_avahi_state" == "static" ]]; then
    record "avahi-masked" "WARN" "avahi enabled (state: $_avahi_state) — confirm system_disable_avahi: false is intentional"
  else record "avahi-masked" "FAIL" "not masked (state: $_avahi_state) — mDNS service discovery leakage risk"; fi
  unset _avahi_state
  _avahi_sock=$(systemctl show -p UnitFileState --value avahi-daemon.socket 2>/dev/null)
  if [[ "$_avahi_sock" == "masked" ]]; then record "avahi-socket-masked" "PASS"
  elif [[ "$_avahi_sock" == "enabled" || "$_avahi_sock" == "static" ]]; then
    record "avahi-socket-masked" "WARN" "avahi-daemon.socket enabled (state: $_avahi_sock) — confirm system_disable_avahi: false is intentional"
  else record "avahi-socket-masked" "FAIL" "avahi-daemon.socket not masked (state: $_avahi_sock) — mDNS port 5353 may be open"; fi
  unset _avahi_sock
  # passim masked (fwupd dependency — unauthenticated HTTP on 0.0.0.0:27500 reachable via Tailscale trusted zone)
  _passim_state=$(systemctl show -p UnitFileState --value passim.service 2>/dev/null)
  if [[ "$_passim_state" == "masked" ]]; then record "passim-masked" "PASS"
  elif [[ -z "$_passim_state" ]]; then record "passim-masked" "WARN" "passim unit not found — package not installed (no port-27500 exposure)"
  elif $_csb_non_fedora; then record "passim-masked" "WARN" "passim masking skipped on RHEL CSB (state: $_passim_state) — IT manages fwupd/firmware; Ansible not csb_rhel gate"
  else record "passim-masked" "FAIL" "not masked (state: $_passim_state) — unauthenticated HTTP server on 0.0.0.0:27500"; fi
  unset _passim_state

  # NFS server and rpcbind masked (CIS 2.2.7 — workstation must not run an NFS server)
  for _unit in \
    'nfs-server.service:nfs-server-masked:nfs-server.service not masked (workstation should not serve NFS, CIS 2.2.7)' \
    'rpcbind.service:rpcbind-masked:rpcbind.service not masked (required by nfs-server; mask both per CIS 2.2.7)' \
    'rpcbind.socket:rpcbind-socket-masked:rpcbind.socket not masked — socket activation can start rpcbind (CIS 2.2.7)'; do
    _u_svc="${_unit%%:*}"; _rest="${_unit#*:}"; _u_key="${_rest%%:*}"; _u_msg="${_rest#*:}"
    if [[ "$(systemctl show -p UnitFileState --value "$_u_svc" 2>/dev/null)" == 'masked' ]]; then record "$_u_key" 'PASS'
    else record "$_u_key" 'FAIL' "$_u_msg"; fi
  done
  unset _unit _u_svc _rest _u_key _u_msg
  # Cockpit web console masked (or intentionally enabled via system_enable_cockpit: true)
  _ck_svc=$(systemctl show -p UnitFileState --value cockpit.service 2>/dev/null)
  if [[ "$_ck_svc" == "masked" ]]; then record "cockpit-service-masked" "PASS"
  elif systemctl is-active cockpit.service &>/dev/null; then
    record "cockpit-service-masked" "WARN" "cockpit.service active — confirm system_enable_cockpit: true is intentional (port 9090)"
  else record "cockpit-service-masked" "FAIL" "cockpit.service not masked (state: $_ck_svc) — port 9090 reachable from Tailscale peers"; fi
  unset _ck_svc
  _ck_sock=$(systemctl show -p UnitFileState --value cockpit.socket 2>/dev/null)
  if [[ "$_ck_sock" == "masked" ]]; then record "cockpit-socket-masked" "PASS"
  elif systemctl is-active cockpit.socket &>/dev/null; then
    record "cockpit-socket-masked" "WARN" "cockpit.socket active — confirm system_enable_cockpit: true is intentional"
  else record "cockpit-socket-masked" "FAIL" "cockpit.socket not masked (state: $_ck_sock) — web console activation possible"; fi
  unset _ck_sock

  # ABRT crash reporter services masked (redundant with systemd-coredump; skipped on RHEL CSB where IT manages via Satellite)
  if ! $CSB_HOST || grep -qiE '^ID=fedora' /etc/os-release 2>/dev/null; then
    for _abrt_svc in abrtd.service abrt-journal-core.service abrt-oops.service abrt-vmcore.service abrt-xorg.service; do
      _abrt_state=$(systemctl show -p UnitFileState --value "$_abrt_svc" 2>/dev/null)
      [[ -z "$_abrt_state" || "$_abrt_state" == "not-found" ]] && continue
      _abrt_base="${_abrt_svc%.service}"; _abrt_key="${_abrt_base}-masked"
      if [[ "$_abrt_state" == "masked" ]]; then record "$_abrt_key" "PASS"
      else record "$_abrt_key" "FAIL" "$_abrt_svc not masked (state: $_abrt_state) — ABRT should be masked in favour of systemd-coredump"; fi
    done
    unset _abrt_svc _abrt_state _abrt_key _abrt_base
  fi

  # thermald: masked on non-Intel; enabled on Intel (Intel-only thermal daemon)
  if [[ -f /proc/cpuinfo ]] && grep -q 'GenuineIntel' /proc/cpuinfo 2>/dev/null; then
    _thermald_state="$(systemctl show -p UnitFileState --value thermald 2>/dev/null)"
    if [[ "$_thermald_state" == "masked" ]]; then
      record "thermald-unmasked-on-intel" "WARN" "thermald is masked on Intel hardware — run: make system"
    else record "thermald-unmasked-on-intel" "PASS"; fi
    unset _thermald_state
  elif [[ -f /proc/cpuinfo ]]; then
    if [[ "$(systemctl show -p UnitFileState --value thermald 2>/dev/null)" == "masked" ]]; then
      record "thermald-masked" "PASS"
    else
      record "thermald-masked" "WARN" "thermald not masked on non-Intel hardware (exits immediately but wastes a unit slot)"
    fi
  fi

  # AIDE file integrity — only check if aide-check.timer is deployed (skips cleanly when AIDE disabled)
  if systemctl list-unit-files aide-check.timer &>/dev/null; then
    if systemctl is-enabled aide-check.timer &>/dev/null && systemctl is-active aide-check.timer &>/dev/null; then
      record "aide-timer" "PASS"
    elif systemctl is-enabled aide-check.timer &>/dev/null; then
      record "aide-timer" "WARN" "timer enabled but not active (reboot or: systemctl start aide-check.timer)"
    else record "aide-timer" "WARN" "timer not enabled"; fi
    if [[ -f /var/lib/aide/aide.db.gz ]]; then record "aide-db" "PASS"
    else record "aide-db" "WARN" "AIDE database not initialized (run: sudo bash -c 'aide --init && mv /var/lib/aide/aide.db.new.gz /var/lib/aide/aide.db.gz')"; fi
    if grep -q '^RestrictAddressFamilies=none$' /etc/systemd/system/aide-check.service 2>/dev/null; then
      record "aide-service-no-network" "PASS"
    elif [[ -f /etc/systemd/system/aide-check.service ]]; then
      record "aide-service-no-network" "FAIL" "RestrictAddressFamilies=none missing from aide-check.service (AIDE can make network calls)"
    else record "aide-service-no-network" "WARN" "aide-check.service not deployed"; fi
  fi  # (AIDE not deployed — system_aide_enabled: false; checks skipped)

  # Chrony NTS: first verify config, then verify actual NTS cookies established
  # (port 4460 is required for NTS-KE; may be blocked on CSB corporate networks)
  _chrony_conf="/etc/chrony.conf"
  [[ -f /etc/chrony/chrony.conf ]] && _chrony_conf="/etc/chrony/chrony.conf"
  if grep -qE '^(pool|server|peer).*\bnts\b' "$_chrony_conf" 2>/dev/null; then
    if chronyc -c authdata 2>/dev/null | awk -F, '$5 > 0 {found=1} END {exit !found}'; then
      record "chrony-nts" "PASS"
    else record "chrony-nts" "WARN" "NTS configured but no authenticated sources (port 4460 blocked? needs boot?)"; fi
  elif $CSB_HOST; then record "chrony-nts" "WARN" "skipped on CSB — IT manages chrony.conf (Kerberos NTP)"
  else record "chrony-nts" "WARN" "NTS not configured in chrony.conf"; fi
  _chrony_svc="chronyd"
  grep -qiE '^ID=debian' /etc/os-release 2>/dev/null && _chrony_svc="chrony"
  if systemctl is-enabled "$_chrony_svc" &>/dev/null && systemctl is-active "$_chrony_svc" &>/dev/null; then
    record "chronyd-service" "PASS"
  elif systemctl is-enabled "$_chrony_svc" &>/dev/null; then
    record "chronyd-service" "FAIL" "$_chrony_svc enabled but not active (time sync required for FIDO2/TLS)"
  else record "chronyd-service" "FAIL" "$_chrony_svc not enabled or started (time sync absent = FIDO2/TLS breaks)"; fi
  unset _chrony_svc
  # fwupd firmware update daemon
  if systemctl is-enabled fwupd &>/dev/null; then record "fwupd-enabled" "PASS"
  else record "fwupd-enabled" "WARN" "fwupd not enabled (firmware updates won't run automatically)"; fi
  if systemctl is-active fwupd-refresh.timer &>/dev/null; then record "fwupd-refresh-timer" "PASS"
  else record "fwupd-refresh-timer" "WARN" "fwupd-refresh.timer inactive — LVFS firmware metadata may be stale"; fi

  # pam_wheel.so
  if grep -qE '^auth.*required.*pam_wheel.so' /etc/pam.d/su 2>/dev/null; then record "pam-wheel" "PASS"
  elif grep -qiE '^ID=debian' /etc/os-release 2>/dev/null; then
    record "pam-wheel" "WARN" "skipped on Debian — 'sudo' group used instead of 'wheel'; pam_wheel.so not deployed by Ansible on apt systems"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then record "pam-wheel" "WARN" "skipped on RHEL CSB — IT manages pam_wheel.so via Satellite/SCAP"
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
    elif $_csb_non_fedora; then
      record "authselect-profile" "WARN" "skipped on RHEL CSB — IT may use custom authselect profile via Satellite/IPA/SCAP; not required to be named sssd"
    else record "authselect-profile" "FAIL" "authselect profile is not sssd (faillock/pwhistory may not wire correctly)"; fi
    if authselect is-feature-enabled with-faillock 2>/dev/null; then record "authselect-faillock" "PASS"
    elif $_csb_non_fedora; then
      record "authselect-faillock" "WARN" "skipped on RHEL CSB — Ansible intentionally omits enable-feature on RHEL (IPA/SSSD owns PAM policy)"
    else record "authselect-faillock" "FAIL" "authselect with-faillock not enabled (faillock settings won't apply)"; fi
    if authselect is-feature-enabled with-pwhistory 2>/dev/null; then record "authselect-pwhistory" "PASS"
    elif $_csb_non_fedora; then
      record "authselect-pwhistory" "WARN" "skipped on RHEL CSB — Ansible intentionally omits enable-feature on RHEL (IPA/SSSD owns PAM policy)"
    else record "authselect-pwhistory" "FAIL" "authselect with-pwhistory not enabled (history reuse won't enforce)"; fi
    # authselect check verifies actual PAM files match profile+features — is-feature-enabled only checks state file
    if authselect check 2>/dev/null; then record "authselect-check" "PASS"
    elif $_csb_non_fedora; then record "authselect-check" "WARN" "skipped on RHEL CSB — IT manages PAM via IPA/SCAP; authselect check may detect intentional drift"
    else record "authselect-check" "FAIL" "authselect PAM files differ from profile — run: authselect select sssd --force"; fi
  fi

  # faillock.conf (deny=5, unlock_time=900, local_users_only for SSSD safety)
  # CSB RHEL: IPA/SSSD owns PAM policy; faillock.conf not written by Ansible — WARN not FAIL
  _skip_faillock=false
  $_csb_non_fedora && _skip_faillock=true
  for _flk in \
    '^deny = 5$:faillock-deny:faillock deny not set to 5' \
    '^local_users_only:faillock-local-only:faillock missing local_users_only (SSSD double-lockout risk)' \
    '^unlock_time = 900$:faillock-unlock-time:faillock unlock_time not set to 900'; do
    _pat=${_flk%%:*}; _rest=${_flk#*:}; _key=${_rest%%:*}; _msg=${_rest#*:}
    if grep -qE "$_pat" /etc/security/faillock.conf 2>/dev/null; then record "$_key" "PASS"
    elif $_skip_faillock; then record "$_key" "WARN" "skipped on RHEL CSB — IT policy governs faillock thresholds"
    else record "$_key" "FAIL" "$_msg"; fi
  done
  unset _skip_faillock
  # even_deny_root: absence is the expected state for single-user workstations (root SSH blocked by sshd).
  # Only emit a record when the option is explicitly present (system_faillock_even_deny_root: true).
  if grep -q '^even_deny_root' /etc/security/faillock.conf 2>/dev/null; then record "faillock-even-deny-root" "PASS"; fi

  # pwquality.conf (minlen=14 + complexity settings — CIS 5.3.x)
  # CSB RHEL: IPA/SSSD owns password policy; pwquality.conf not written by Ansible — WARN not FAIL
  _skip_pwquality=false
  $_csb_non_fedora && _skip_pwquality=true
  for _pwq in \
    '^minlen = 14:pwquality-minlen:pwquality minlen not set to 14' \
    '^dcredit = -1:pwquality-dcredit:pwquality dcredit not set to -1' \
    '^ucredit = -1:pwquality-ucredit:pwquality ucredit not set to -1' \
    '^lcredit = -1:pwquality-lcredit:pwquality lcredit not set to -1' \
    '^ocredit = -1:pwquality-ocredit:pwquality ocredit not set to -1' \
    '^difok = 4:pwquality-difok:pwquality difok not set to 4' \
    '^maxrepeat = 3:pwquality-maxrepeat:pwquality maxrepeat not set to 3' \
    '^enforce_for_root:pwquality-enforce-root:pwquality enforce_for_root not set (CIS 5.3.4)'; do
    _pat=${_pwq%%:*}; _rest=${_pwq#*:}; _key=${_rest%%:*}; _msg=${_rest#*:}
    if grep -q "$_pat" /etc/security/pwquality.conf 2>/dev/null; then record "$_key" "PASS"
    elif $_skip_pwquality; then record "$_key" "WARN" "skipped on RHEL CSB — IPA/SSSD owns PAM policy"
    else record "$_key" "FAIL" "$_msg"; fi
  done
  unset _skip_pwquality

  # sudoers hardening drop-in (mode 0440 — unreadable by non-root; WARN not FAIL)
  if [[ "$EUID" -eq 0 ]]; then
    if grep -qE '^Defaults[[:space:]].*use_pty' /etc/sudoers.d/99-hardening 2>/dev/null && \
       grep -qE '^Defaults[[:space:]].*logfile=' /etc/sudoers.d/99-hardening 2>/dev/null && \
       grep -qE '^Defaults[[:space:]].*umask=' /etc/sudoers.d/99-hardening 2>/dev/null; then
      record "sudoers-hardening" "PASS"
    elif $_csb_non_fedora; then record "sudoers-hardening" "WARN" "skipped on RHEL CSB — IT manages sudoers via Satellite/SCAP; 99-hardening not deployed"
    else record "sudoers-hardening" "FAIL" "sudoers hardening drop-in missing or incomplete"; fi
  else record "sudoers-hardening" "WARN" "skipped — /etc/sudoers.d/ is mode 0440 (run with sudo for full check)"; fi

  # pwhistory remember=24 + enforce_for_root (CIS 5.3.5) — skipped on RHEL CSB (IPA/SSSD owns PAM policy; pwhistory.conf not written)
  _skip_pwhistory=false
  $_csb_non_fedora && _skip_pwhistory=true
  for _pwh in \
    '^remember = 24:pwhistory-remember:pwhistory remember not set to 24' \
    '^enforce_for_root:pwhistory-enforce-root:pwhistory enforce_for_root not set — root can reuse passwords despite remember=24 (CIS 5.3.5)'; do
    _pat="${_pwh%%:*}"; _rest="${_pwh#*:}"; _key="${_rest%%:*}"; _msg="${_rest#*:}"
    if grep -q "$_pat" /etc/security/pwhistory.conf 2>/dev/null; then record "$_key" 'PASS'
    elif $_skip_pwhistory; then record "$_key" 'WARN' 'skipped on RHEL CSB — IPA/SSSD owns PAM policy; pwhistory.conf not written by Ansible'
    else record "$_key" 'FAIL' "$_msg"; fi
  done
  unset _skip_pwhistory

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

  # fprintd masked (prevents fingerprint from bypassing faillock; or intentionally enabled via system_disable_fingerprint: false)
  _fprintd_state=$(systemctl show -p UnitFileState --value fprintd.service 2>/dev/null)
  if [[ "$_fprintd_state" == "masked" ]]; then record "fprintd-masked" "PASS"
  elif [[ "$_fprintd_state" == "enabled" || "$_fprintd_state" == "static" ]]; then
    record "fprintd-masked" "WARN" "fprintd.service enabled (state: $_fprintd_state) — confirm system_disable_fingerprint: false is intentional (faillock bypass risk)"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then
    record "fprintd-masked" "WARN" "fprintd masking skipped on RHEL CSB — IT manages fingerprint auth; Ansible not csb_rhel gate"
  else record "fprintd-masked" "FAIL" "fprintd.service not masked (state: $_fprintd_state) — fingerprint can bypass faillock"; fi
  unset _fprintd_state

  # resolv.conf points to systemd-resolved stub (required for split DNS/MagicDNS)
  # Skipped on CSB — Ansible guard omits this because NM/VPN manages resolv.conf on corporate networks
  if [[ "$(readlink /etc/resolv.conf 2>/dev/null)" == "/run/systemd/resolve/stub-resolv.conf" ]]; then
    record "resolv-stub" "PASS"
  elif $CSB_HOST; then record "resolv-stub" "WARN" "skipped on CSB — NM/VPN manages resolv.conf on corporate networks"
  else record "resolv-stub" "FAIL" "resolv.conf not symlinked to stub-resolv.conf — Tailscale MagicDNS broken; fix: sudo ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf"; fi

  # systemd-resolved service must be active for stub socket at 127.0.0.53 to work
  if systemctl is-active systemd-resolved &>/dev/null; then record "systemd-resolved-active" "PASS"
  else record "systemd-resolved-active" "FAIL" "systemd-resolved.service not active — stub socket 127.0.0.53 down; fix: sudo systemctl enable --now systemd-resolved"; fi

  # Connectivity sanity check — verifies DoT/stub-resolver config did not break DNS (not provisioning-specific).
  if getent hosts redhat.com &>/dev/null; then record "dns-resolves" "PASS"
  else record "dns-resolves" "FAIL" "DNS resolution failed for redhat.com"; fi

  # cron.allow restricts cron to root only (CIS 5.1.8)
  # Skipped on RHEL CSB — katello-agent, Insights client, and IT monitoring run cron jobs under
  # non-root system users; cron.allow=root-only would silently break those IT management jobs
  if grep -qx 'root' /etc/cron.allow 2>/dev/null; then record "cron-allow-root" "PASS"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then record "cron-allow-root" "WARN" "skipped on RHEL CSB — IT monitoring agents use cron; cron.allow not restricted to root"
  else record "cron-allow-root" "FAIL" "/etc/cron.allow missing or not restricted to root (CIS 5.1.8)"; fi
  # at.allow restricts 'at' command to root only (CIS 5.1.9)
  if grep -qx 'root' /etc/at.allow 2>/dev/null; then record "at-allow-root" "PASS"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then record "at-allow-root" "WARN" "skipped on RHEL CSB — IT monitoring agents may use at; at.allow not restricted to root"
  else record "at-allow-root" "FAIL" "/etc/at.allow missing or not restricted to root (CIS 5.1.9)"; fi

  # splunk system account shell (CIS 5.6) — CSB hosts only; splunk absent on standard Fedora
  if $CSB_HOST && _splunk_entry=$(getent passwd splunk 2>/dev/null) && [[ -n "$_splunk_entry" ]]; then
    _splunk_shell=$(echo "$_splunk_entry" | cut -d: -f7)
    if [[ "$_splunk_shell" == "/sbin/nologin" ]]; then record "splunk-shell" "PASS"
    else record "splunk-shell" "FAIL" "splunk shell='$_splunk_shell', expected /sbin/nologin (CIS 5.6) — run: make redhat"; fi
    unset _splunk_shell _splunk_entry
  fi

  # Login banner deployed to /etc/issue (CIS 1.7.1)
  # Skipped on RHEL CSB only — IT deploys a mandated corporate legal banner; Ansible does not
  # write /etc/issue on RHEL CSB (csb_rhel guard). On Fedora hybrid CSB, Ansible does deploy
  # the banner (not csb_rhel is true), so a missing banner is a real FAIL there.
  if grep -qi 'authorized users' /etc/issue 2>/dev/null; then record "login-banner" "PASS"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then record "login-banner" "WARN" "skipped on RHEL CSB — IT deploys mandated legal banner; Ansible does not write /etc/issue"
  else record "login-banner" "FAIL" "login banner not deployed or missing expected text (/etc/issue)"; fi

  # Critical file permissions (CIS 6.1.x)
  shadow_mode=$(stat -c '%a' /etc/shadow 2>/dev/null || echo "?")
  if [[ "$shadow_mode" == "0" ]]; then record "shadow-perms" "PASS"
  elif grep -qiE '^ID=debian' /etc/os-release 2>/dev/null && [[ "$shadow_mode" == "640" ]]; then record "shadow-perms" "PASS"
  else record "shadow-perms" "FAIL" "permissions $shadow_mode, expected 0000 (Fedora/RHEL) or 0640 (Debian)"; fi
  gshadow_mode=$(stat -c '%a' /etc/gshadow 2>/dev/null || echo "?")
  if [[ "$gshadow_mode" == "0" ]]; then record "gshadow-perms" "PASS"
  elif grep -qiE '^ID=debian' /etc/os-release 2>/dev/null && [[ "$gshadow_mode" == "640" ]]; then record "gshadow-perms" "PASS"
  else record "gshadow-perms" "FAIL" "permissions $gshadow_mode, expected 0000 (Fedora/RHEL) or 0640 (Debian)"; fi

  # TMOUT session timeout (CIS 5.5.5) — verify numeric value <=900s in bash (profile.d) and zsh (/etc/zshrc)
  _tmout_val=$(grep -oP '^(readonly\s+)?TMOUT=\K[0-9]+' /etc/profile.d/tmout.sh 2>/dev/null || echo "")
  if [[ -n "$_tmout_val" && "$_tmout_val" -gt 0 && "$_tmout_val" -le 900 ]]; then
    record "tmout-bash" "PASS"
  elif [[ -z "$_tmout_val" ]]; then
    record "tmout-bash" "WARN" "tmout.sh missing or TMOUT not set — may be intentional (system_tmout: 0 disables)"
  else
    record "tmout-bash" "FAIL" "TMOUT=$_tmout_val exceeds CIS 5.5.5 maximum of 900s"
  fi
  # Debian's zsh binary reads /etc/zsh/zshrc, not /etc/zshrc (which is silently ignored there)
  _zsh_tmout_file="/etc/zshrc"
  grep -qiE '^ID=debian' /etc/os-release 2>/dev/null && _zsh_tmout_file="/etc/zsh/zshrc"
  _tmout_zsh=$(grep -oP '^(typeset -xr\s+|readonly\s+)?TMOUT=\K[0-9]+' "$_zsh_tmout_file" 2>/dev/null || echo "")
  if [[ -n "$_tmout_zsh" && "$_tmout_zsh" -gt 0 && "$_tmout_zsh" -le 900 ]]; then
    record "tmout-zsh" "PASS"
  elif [[ -z "$_tmout_zsh" ]]; then
    record "tmout-zsh" "WARN" "$_zsh_tmout_file missing TMOUT — may be intentional (system_tmout: 0 disables)"
  else
    record "tmout-zsh" "FAIL" "TMOUT=$_tmout_zsh in $_zsh_tmout_file exceeds CIS 5.5.5 maximum of 900s"
  fi
  unset _zsh_tmout_file

  # /tmp hardening (CIS 1.1.2.x) — noexec/nosuid/nodev all required
  _tmp_opts=$(findmnt -n -o OPTIONS /tmp 2>/dev/null || echo "")
  _tmp_dropin="/etc/systemd/system/tmp.mount.d/hardening.conf"
  if [[ "$_tmp_opts" != *nosuid* ]] || [[ "$_tmp_opts" != *nodev* ]]; then
    record "tmp-hardening" "FAIL" "/tmp missing nosuid/nodev: $_tmp_opts"
  elif [[ "$_tmp_opts" == *noexec* ]]; then
    record "tmp-hardening" "PASS"
  elif [[ ! -f "$_tmp_dropin" ]]; then
    record "tmp-hardening" "WARN" "tmp.mount.d/hardening.conf missing — system role may not have run"
  elif grep -q noexec "$_tmp_dropin" 2>/dev/null; then
    record "tmp-hardening" "FAIL" "/tmp noexec in drop-in but not mounted: $_tmp_opts"
  else
    record "tmp-hardening" "WARN" "/tmp noexec disabled — system_tmp_noexec: false (intentional)"
  fi
  unset _tmp_opts _tmp_dropin

  # /dev/shm hardening (CIS 1.1.7.x) — noexec/nosuid/nodev all required
  _shm_opts=$(findmnt -n -o OPTIONS /dev/shm 2>/dev/null || echo "")
  if [[ "$_shm_opts" == *noexec* ]] && [[ "$_shm_opts" == *nosuid* ]] && [[ "$_shm_opts" == *nodev* ]]; then
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
  # 'bind' is MS_BIND syscall flag, never stored in kernel option strings — compare MAJ:MIN instead
  _vt_opts=$(findmnt -n -o OPTIONS /var/tmp 2>/dev/null || echo "")
  _vt_min=$(findmnt -n -o MAJ:MIN /var/tmp 2>/dev/null | tr -d ' ' || echo "")
  _tmp_min=$(findmnt -n -o MAJ:MIN /tmp 2>/dev/null | tr -d ' ' || echo "")
  if [[ -n "$_vt_min" && "$_vt_min" = "$_tmp_min" ]] && [[ "$_vt_opts" == *noexec* ]]; then record "var-tmp-bind" "PASS"
  elif [[ ! -f /etc/systemd/system/var-tmp.mount ]]; then record "var-tmp-bind" "WARN" "var-tmp.mount not deployed — system_var_tmp_noexec may be false (intentional)"
  elif $CSB_HOST; then record "var-tmp-bind" "WARN" "/var/tmp not bind-mounted with noexec on CSB: $_vt_opts"
  else record "var-tmp-bind" "FAIL" "/var/tmp not bind-mounted with noexec: $_vt_opts"; fi

  if [[ -f /etc/systemd/system/var-tmp.mount ]]; then
    if systemctl is-enabled var-tmp.mount &>/dev/null; then record "var-tmp-mount-enabled" "PASS"
    else record "var-tmp-mount-enabled" "FAIL" "var-tmp.mount not enabled — /var/tmp hardening lost on reboot"; fi
  fi

  unset _vt_opts _vt_min _tmp_min

  # /home nosuid (CIS 1.1.9) — nosuid prevents setuid binaries copied into $HOME from gaining elevated privileges
  # Skipped on CSB — IT manages /home mount (may be NFS/autofs for LDAP users; remounting with nosuid may break access)
  if findmnt -n /home &>/dev/null; then
    _home_opts=$(findmnt -n -o OPTIONS /home 2>/dev/null || echo "")
    if [[ "$_home_opts" == *nosuid* ]] && [[ "$_home_opts" == *nodev* ]]; then record "home-nosuid" "PASS"
    elif $_csb_non_fedora; then record "home-nosuid" "WARN" "skipped on RHEL CSB — IT manages /home mount (may be NFS/autofs); nosuid not applied"
    else record "home-nosuid" "FAIL" "/home is a separate mount but nosuid/nodev not set: $_home_opts"; fi
    unset _home_opts
  else record "home-nosuid" "PASS"; fi

  # /boot hardening — nosuid,nodev,noexec (guard: /boot may not be a separate mountpoint)
  if findmnt -n /boot &>/dev/null; then
    _boot_opts=$(findmnt -n -o OPTIONS /boot 2>/dev/null || echo "")
    if [[ "$_boot_opts" == *nosuid* ]] && [[ "$_boot_opts" == *nodev* ]] && [[ "$_boot_opts" == *noexec* ]]; then
      record "boot-hardening" "PASS"
    elif $CSB_HOST; then record "boot-hardening" "WARN" "/boot missing hardening options on CSB (IT manages fstab; boot-harden.service re-applies on reboot): $_boot_opts"
    else record "boot-hardening" "FAIL" "/boot missing hardening options: $_boot_opts"; fi
    unset _boot_opts
    # On CSB, fstab is IT-managed so Ansible deploys boot-harden.service to re-apply
    # hardening on each boot. Verify the service is enabled — if it isn't, a reboot loses
    # nosuid/nodev/noexec and the boot-hardening check above would pass until next reboot.
    if $CSB_HOST; then
      if systemctl is-enabled boot-harden.service &>/dev/null; then record "boot-harden-enabled" "PASS"
      else record "boot-harden-enabled" "FAIL" "boot-harden.service not enabled on CSB — /boot hardening lost on reboot"; fi
    fi
  fi

  # /boot/efi hardening — nosuid,noexec (vfat does not support nodev)
  if findmnt -n /boot/efi &>/dev/null; then
    _efi_opts=$(findmnt -n -o OPTIONS /boot/efi 2>/dev/null || echo "")
    if [[ "$_efi_opts" == *nosuid* ]] && [[ "$_efi_opts" == *noexec* ]]; then
      record "boot-efi-hardening" "PASS"
    elif $CSB_HOST; then
      record "boot-efi-hardening" "WARN" "/boot/efi missing nosuid/noexec on CSB: $_efi_opts (IT manages fstab; may reset on reboot)"
    else
      record "boot-efi-hardening" "FAIL" "/boot/efi missing nosuid/noexec: $_efi_opts"
    fi
    unset _efi_opts
    # On CSB, Ansible deploys boot-efi-harden.service to re-apply options each boot.
    # Verify it is enabled — silent enable failure (ignore_errors: true) leaves no other signal.
    if $CSB_HOST; then
      if systemctl is-enabled boot-efi-harden.service &>/dev/null; then
        record "boot-efi-harden-enabled" "PASS"
      else
        record "boot-efi-harden-enabled" "FAIL" "boot-efi-harden.service not enabled on CSB — /boot/efi hardening lost on reboot"
      fi
    fi
  fi

  # kernel.core_pattern safety — must begin with | (pipe to handler), never a raw path
  _core_pattern="$(sysctl -n kernel.core_pattern 2>/dev/null)"
  if [[ "$_core_pattern" == "|"* ]]; then record "core-pattern" "PASS"
  else record "core-pattern" "FAIL" "kernel.core_pattern='$_core_pattern' does not start with | (pipe handler required; raw core files expose data)"; fi

  # inotify limits (required for IDE/file-watcher tools — system role sets these)
  _inotify_watches=$(sysctl -n fs.inotify.max_user_watches 2>/dev/null || echo "0")
  if [[ "$_inotify_watches" -ge 1048576 ]] 2>/dev/null; then record "inotify-max-user-watches" "PASS"
  else record "inotify-max-user-watches" "FAIL" "fs.inotify.max_user_watches=$_inotify_watches, expected >=1048576 (run: make system)"; fi
  unset _inotify_watches
  _inotify_instances=$(sysctl -n fs.inotify.max_user_instances 2>/dev/null || echo "0")
  if [[ "$_inotify_instances" -ge 8192 ]] 2>/dev/null; then record "inotify-max-user-instances" "PASS"
  else record "inotify-max-user-instances" "FAIL" "fs.inotify.max_user_instances=$_inotify_instances, expected >=8192 (run: make system)"; fi
  unset _inotify_instances

  # ctrl+alt+del disabled (physical security)
  if [[ "$(systemctl show -p UnitFileState --value ctrl-alt-del.target 2>/dev/null)" == "masked" ]]; then record "ctrl-alt-del-masked" "PASS"
  else record "ctrl-alt-del-masked" "FAIL" "ctrl-alt-del.target not masked"; fi

  # dnf-automatic
  timer="dnf-automatic.timer"
  command -v dnf5 &>/dev/null && timer="dnf5-automatic.timer"
  if systemctl is-enabled "$timer" &>/dev/null && systemctl is-active "$timer" &>/dev/null; then
    record "dnf-automatic" "PASS"
  elif systemctl is-enabled "$timer" &>/dev/null; then
    record "dnf-automatic" "WARN" "timer enabled but not active (reboot or: systemctl start $timer)"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then
    record "dnf-automatic" "WARN" "skipped on RHEL CSB — IT manages updates via Satellite/RHSM; Ansible not-csb_rhel guard"
  else record "dnf-automatic" "WARN" "timer not enabled"; fi

  # RPM Fusion free repo (Fedora only; mirrors repos_dnf not csb_rhel guard)
  if grep -qiE '^ID=fedora' /etc/os-release 2>/dev/null; then
    if dnf repolist 2>/dev/null | grep -q '^rpmfusion-free\b'; then record "rpmfusion-free-repo" "PASS"
    else record "rpmfusion-free-repo" "WARN" "rpmfusion-free repo not enabled (run: make repos_dnf; default repo_rpmfusion_free=true)"; fi
  fi

  # TLP power management (ThinkPad battery care)
  if systemctl is-enabled tlp.service &>/dev/null && systemctl is-active tlp.service &>/dev/null; then
    record "tlp-service" "PASS"
  elif systemctl is-enabled tlp.service &>/dev/null; then
    record "tlp-service" "WARN" "enabled but not active (reboot or: systemctl start tlp.service)"
  elif grep -qiE '^ID=debian' /etc/os-release 2>/dev/null && [[ -d /sys/class/power_supply/BAT0 ]]; then
    record "tlp-service" "WARN" "TLP not installed on Debian (apt package not managed by this role) — battery charge thresholds absent on laptop hardware"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null && [[ -d /sys/class/power_supply/BAT0 ]]; then
    record "tlp-service" "WARN" "TLP intentionally absent on RHEL CSB (IT manages power policy; cycle-41 not-csb_rhel guard)"
  elif [[ -d /sys/class/power_supply/BAT0 ]]; then
    record "tlp-service" "FAIL" "tlp.service not enabled on laptop hardware (battery threshold protection absent)"
  else record "tlp-service" "WARN" "tlp.service not enabled"; fi
  if [[ -d /sys/class/power_supply/BAT0 ]]; then
    if [[ -f /etc/tlp.d/50-thinkpad.conf ]]; then record "tlp-config" "PASS"
    else record "tlp-config" "WARN" "ThinkPad TLP config not deployed (/etc/tlp.d/50-thinkpad.conf)"; fi
  fi
  # Battery charge threshold (ThinkPad sysfs — only present on supported hardware)
  if [[ -f /sys/class/power_supply/BAT0/charge_control_end_threshold ]]; then
    _bat_end=$(<"/sys/class/power_supply/BAT0/charge_control_end_threshold" 2>/dev/null || echo "?")
    if [[ "$_bat_end" != "?" && "$_bat_end" -lt 100 ]] 2>/dev/null; then record "tlp-bat-threshold" "PASS"
    else record "tlp-bat-threshold" "WARN" "end threshold=$_bat_end (expected <100 for battery longevity)"; fi
    unset _bat_end
  fi
  if [[ -f /sys/class/power_supply/BAT0/charge_control_start_threshold ]]; then
    _bat_start=$(<"/sys/class/power_supply/BAT0/charge_control_start_threshold" 2>/dev/null || echo "?")
    if [[ "$_bat_start" != "?" && "$_bat_start" -gt 0 && "$_bat_start" -lt 100 ]] 2>/dev/null; then
      record "tlp-bat-start-threshold" "PASS"
    else record "tlp-bat-start-threshold" "WARN" "start threshold=$_bat_start (expected >0 and <100)"; fi
    unset _bat_start
  fi
  if command -v tlp &>/dev/null || [[ -d /etc/tlp.d ]]; then
    if [[ "$(systemctl show -p UnitFileState --value power-profiles-daemon 2>/dev/null)" == "masked" ]]; then record "ppd-masked" "PASS"
    else record "ppd-masked" "FAIL" "power-profiles-daemon not masked — conflicts with TLP over battery thresholds and ACPI platform profiles"; fi
    if [[ "$(systemctl show -p UnitFileState --value tuned-ppd 2>/dev/null)" == "masked" ]]; then record "tuned-ppd-masked" "PASS"
    else record "tuned-ppd-masked" "FAIL" "tuned-ppd not masked — conflicts with TLP on Fedora 44"; fi
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

  # dconf system policies (CIS 1.8.3-1.8.8) — only relevant on GNOME systems
  # Sway/i3 systems use greetd not GDM; dconf policies are GNOME-specific.
  if ! systemctl cat gdm.service &>/dev/null; then
    record "dconf-policies" "PASS" "GDM not installed — dconf system policies are GNOME-specific (Sway/i3 systems not affected)"
  elif $CSB_HOST && grep -qiE '^ID=rhel' /etc/os-release 2>/dev/null; then
    record "dconf-policies" "WARN" "dconf hardening skipped on CSB — IT manages GDM banner and screensaver policy (Satellite/SCAP)"
  else
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
  fi  # end GDM check

  # Unexpected listening ports (non-loopback)
  listeners=$(ss -tulnp 2>/dev/null | grep -vE "127\.[0-9]+\.[0-9]+\.[0-9]+|::1" | grep -vE ":${_ssh_port:-$_cfg_ssh_port}([^0-9]|$)" | tail -n +2 || true)
  if [[ -z "$listeners" ]]; then record "no-open-ports" "PASS"
  else record "no-open-ports" "WARN" "$(echo "$listeners" | wc -l) non-loopback listeners"; fi

  # login.defs password aging (CIS 5.4.x)
  # Skipped on CSB — login.defs is not modified on CSB; IPA/SSSD + IT group policy governs local
  # accounts. INACTIVE=30 in particular could lock IT-managed service accounts that don't rotate
  # passwords, so Ansible deliberately skips all login.defs writes when csb_detected is true.
  _pass_max_days=$(awk '/^PASS_MAX_DAYS[[:space:]]/{print $2}' /etc/login.defs 2>/dev/null || echo "")
  if [[ -n "$_pass_max_days" && "$_pass_max_days" -ge 1 && "$_pass_max_days" -le 365 ]]; then record "pass-max-days" "PASS"
  elif $CSB_HOST; then record "pass-max-days" "WARN" "skipped on CSB — login.defs not modified; IT group policy governs password aging"
  else record "pass-max-days" "FAIL" "PASS_MAX_DAYS=${_pass_max_days:-unset} not in CIS range 1-365 in login.defs"; fi
  if grep -qE "^UMASK[[:space:]]+${_system_umask}$" /etc/login.defs 2>/dev/null; then record "umask-login-defs" "PASS"
  elif $CSB_HOST; then record "umask-login-defs" "WARN" "skipped on CSB — login.defs not modified"
  else record "umask-login-defs" "FAIL" "UMASK not set to ${_system_umask} in login.defs"; fi
  _inactive_logindefs=$(awk '/^INACTIVE[[:space:]]/{print $2}' /etc/login.defs 2>/dev/null || echo "")
  if [[ -n "$_inactive_logindefs" && "$_inactive_logindefs" -ge 1 && "$_inactive_logindefs" -le 30 ]]; then record "inactive-lock" "PASS"
  elif $CSB_HOST; then record "inactive-lock" "WARN" "skipped on CSB — INACTIVE not set (would lock IT-managed service accounts with non-rotating passwords)"
  else record "inactive-lock" "FAIL" "INACTIVE=${_inactive_logindefs:-unset} not in CIS range 1-30 in login.defs"; fi
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
      if [[ -n "$_inactive_val" && "$_inactive_val" -ge 1 && "$_inactive_val" -le 30 ]]; then record "chage-inactive-user" "PASS"
      elif $CSB_HOST; then record "chage-inactive-user" "WARN" "skipped on CSB — Ansible intentionally omits chage -I 30 (IPA krbPwdPolicy manages inactive lockout centrally)"
      else record "chage-inactive-user" "FAIL" "shadow INACTIVE='$_inactive_val' for '$_chage_user', not in CIS range 1-30 (CIS 5.5.1.5 — run: chage -I 30 $_chage_user)"; fi
    fi
    unset _chage_user _inactive_val _shadow_line
  else record "chage-inactive-user" "WARN" "skipped — reading /etc/shadow requires root (re-run with sudo for full check)"; fi
  _pass_min_days=$(awk '/^PASS_MIN_DAYS[[:space:]]/{print $2}' /etc/login.defs 2>/dev/null || echo "")
  if [[ -n "$_pass_min_days" && "$_pass_min_days" -ge 1 ]]; then record "pass-min-days" "PASS"
  elif $CSB_HOST; then record "pass-min-days" "WARN" "skipped on CSB — login.defs not modified; IT group policy governs password aging"
  else record "pass-min-days" "FAIL" "PASS_MIN_DAYS=${_pass_min_days:-unset} not >= 1 in login.defs (CIS minimum)"; fi
  _pass_warn_age=$(awk '/^PASS_WARN_AGE[[:space:]]/{print $2}' /etc/login.defs 2>/dev/null || echo "")
  if [[ -n "$_pass_warn_age" && "$_pass_warn_age" -ge 7 ]]; then record "pass-warn-age" "PASS"
  elif $CSB_HOST; then record "pass-warn-age" "WARN" "skipped on CSB — login.defs not modified; IT group policy governs password aging"
  else record "pass-warn-age" "FAIL" "PASS_WARN_AGE=${_pass_warn_age:-unset} not >= 7 in login.defs (CIS minimum)"; fi
  if grep -qE '^HOME_MODE[[:space:]]+0750$' /etc/login.defs 2>/dev/null; then record "home-mode" "PASS"
  elif $CSB_HOST; then record "home-mode" "WARN" "skipped on CSB — login.defs not modified"
  else record "home-mode" "FAIL" "HOME_MODE not set to 0750 in login.defs (CIS: explicit home dir permissions)"; fi

  # Console keymap
  if grep -q '^KEYMAP=us$' /etc/vconsole.conf 2>/dev/null; then record "vconsole-keymap" "PASS"
  else record "vconsole-keymap" "WARN" "KEYMAP=us not set in /etc/vconsole.conf"; fi

  # logind IdleAction=lock (physical security)
  if grep -q '^IdleAction=lock' /etc/systemd/logind.conf.d/99-hardening.conf 2>/dev/null; then
    record "logind-idle-lock" "PASS"
  else record "logind-idle-lock" "FAIL" "logind IdleAction not set to lock — check /etc/systemd/logind.conf.d/99-hardening.conf; fix: make system"; fi
  if grep -q '^IdleActionSec=' /etc/systemd/logind.conf.d/99-hardening.conf 2>/dev/null; then
    record "logind-idle-sec" "PASS"
  else record "logind-idle-sec" "FAIL" "logind IdleActionSec not configured (idle-lock timeout undefined) — check /etc/systemd/logind.conf.d/99-hardening.conf; fix: make system"; fi

  # Session lingering (required for rootless podman.socket to survive provisioning SSH sessions)
  if loginctl show-user "${SUDO_USER:-$USER}" --property=Linger 2>/dev/null | grep -q "^Linger=yes"; then
    record "session-linger" "PASS"
  else record "session-linger" "FAIL" "linger not enabled — podman.socket dies when provisioning SSH session ends"; fi
  # Podman user socket (required for 'kind create cluster' via DOCKER_HOST — 'make kind' in OVN-K/Submariner)
  if [[ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" ]]; then
    record "podman-socket" "PASS"
  else record "podman-socket" "WARN" "Podman user socket not present — kind create cluster will fail (re-login or restart podman.socket)"; fi
  # Verify podman user socket is durably enabled (survives reboot); complements the runtime -S check above.
  if systemctl --user is-enabled podman.socket &>/dev/null; then
    record "podman-socket-enabled" "PASS"
  else record "podman-socket-enabled" "FAIL" "podman.socket not enabled — socket will not start on reboot; fix: systemctl --user enable podman.socket"; fi

  _sysctl_check "kernel.kptr_restrict"               "1" "sysctl-kptr-restrict"
  # kexec: read expected value from deployed config (system_kexec_load_disabled defaults to 0 in default.config.yml)
  _kexec_expected=$(awk -F' *= *' '/^kernel\.kexec_load_disabled/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "kernel.kexec_load_disabled" "${_kexec_expected:-0}" "sysctl-kexec-disabled"
  _io_uring_expected=$(awk -F' *= *' '/^kernel\.io_uring_disabled/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "kernel.io_uring_disabled" "${_io_uring_expected:-1}" "sysctl-io-uring-disabled"
  _sysctl_check "kernel.dmesg_restrict"              "1" "sysctl-dmesg-restrict"
  # unprivileged_bpf: 1=disabled(write-once), 2=disabled(resettable). Both are valid.
  # Integrity lockdown mode locks the value to 2 and makes it read-only (permission denied).
  _bpf_disabled=$(sysctl -n kernel.unprivileged_bpf_disabled 2>/dev/null) || true
  if [[ -n "$_bpf_disabled" && "$_bpf_disabled" -ge 1 ]]; then record "sysctl-bpf-restrict" "PASS"
  elif [[ -z "$_bpf_disabled" && "$EUID" -ne 0 ]]; then record "sysctl-bpf-restrict" "WARN" "unreadable as non-root"
  else record "sysctl-bpf-restrict" "FAIL" "kernel.unprivileged_bpf_disabled=$_bpf_disabled expected >=1"; fi
  _perf_expected=$(awk -F' *= *' '/^kernel\.perf_event_paranoid/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "kernel.perf_event_paranoid" "${_perf_expected:-1}" "sysctl-perf-paranoid"
  # bpf_jit_harden: read expected from deployed config (system_bpf_jit_harden in config.yml may override default 1).
  # Default is 1 (constant blinding for unprivileged callers only); 2=blind all callers including root.
  _bpf_jit_harden_expected=$(awk -F' *= *' '/^net\.core\.bpf_jit_harden/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "net.core.bpf_jit_harden" "${_bpf_jit_harden_expected:-1}" "sysctl-bpf-jit-harden"
  _sysctl_check "kernel.randomize_va_space"          "2" "sysctl-aslr"
  _sysctl_check "fs.suid_dumpable"                   "0" "sysctl-suid-dumpable"
  _sysctl_check "net.ipv4.tcp_syncookies"            "1" "sysctl-syncookies"
  # tcp_timestamps: CIS 3.3.9 recommends 0; playbook default is 1 for OVN-K/Submariner RTTM/PAWS on high-BDP links.
  # Override via system_sysctl_extra: {net.ipv4.tcp_timestamps: 0} in config.yml to comply with CIS.
  _ts_expected=$(awk -F' *= *' '/^net\.ipv4\.tcp_timestamps/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "net.ipv4.tcp_timestamps" "${_ts_expected:-1}" "sysctl-tcp-timestamps"
  if [[ "${_ts_expected:-1}" != "0" ]]; then
    record "sysctl-tcp-timestamps-cis" "WARN" "net.ipv4.tcp_timestamps=${_ts_expected:-1} deviates from CIS 3.3.9 (recommended 0); override via system_sysctl_extra: {net.ipv4.tcp_timestamps: 0} in config.yml"
  fi
  _sysctl_check "net.ipv4.conf.all.accept_redirects"        "0" "sysctl-no-accept-redirects"
  _sysctl_check "net.ipv4.conf.default.accept_redirects"    "0" "sysctl-no-accept-redirects-default"
  _sysctl_check "net.ipv6.conf.all.accept_redirects"        "0" "sysctl-no-accept-redirects-v6"
  _sysctl_check "net.ipv6.conf.default.accept_redirects"    "0" "sysctl-no-accept-redirects-v6-default"
  _sysctl_check "net.ipv4.conf.all.send_redirects"          "0" "sysctl-no-send-redirects"
  _sysctl_check "net.ipv4.conf.default.send_redirects"      "0" "sysctl-no-send-redirects-default"
  _sysctl_check "net.ipv4.conf.all.accept_source_route"     "0" "sysctl-no-source-route"
  _sysctl_check "net.ipv4.conf.default.accept_source_route" "0" "sysctl-no-source-route-default"
  _sysctl_check "net.ipv6.conf.all.accept_source_route"     "0" "sysctl-no-source-route-v6"
  _sysctl_check "net.ipv6.conf.default.accept_source_route" "0" "sysctl-no-source-route-v6-default"
  _sysctl_check "net.ipv4.conf.all.secure_redirects"        "0" "sysctl-no-secure-redirects"
  _sysctl_check "net.ipv4.conf.default.secure_redirects"    "0" "sysctl-no-secure-redirects-default"
  # bridge-nf: WARN if br_netfilter module not loaded (reboot activates only when persistence file exists)
  for _item in \
    "net.bridge.bridge-nf-call-iptables|sysctl-bridge-nf-iptables|reboot or: modprobe br_netfilter && sysctl --system" \
    "net.bridge.bridge-nf-call-ip6tables|sysctl-bridge-nf-ip6tables|br_netfilter not loaded — IPv6 NetworkPolicy enforcement broken for OVN-K"; do
    _key="${_item%%|*}"; _rest="${_item#*|}"; _label="${_rest%%|*}"; _loaded_warn="${_rest#*|}"
    _val=$(sysctl -n "$_key" 2>/dev/null) || true
    if [[ "$_val" == "1" ]]; then record "$_label" "PASS"
    elif [[ -z "$_val" ]]; then
      if [[ ! -f /etc/modules-load.d/br_netfilter.conf ]]; then
        record "$_label" "WARN" \
          "br_netfilter not loaded AND persistence file missing — reboot will NOT fix this; run: make system"
      else
        record "$_label" "WARN" "$_loaded_warn"
      fi
    else record "$_label" "FAIL" "$_key=$_val expected 1"; fi
  done
  _sysctl_check "net.ipv6.conf.all.forwarding"       "1" "sysctl-ipv6-forwarding"

  # vsyscall=none kernel param (ROP gadget mitigation, requires reboot after grubby)
  if grep -q 'vsyscall=none' /proc/cmdline 2>/dev/null; then record "vsyscall-none" "PASS"
  else record "vsyscall-none" "WARN" "vsyscall=none not in cmdline (requires reboot if grubby ran)"; fi
  # IOMMU kernel param (AMD DMA protection — only applicable on AMD CPUs)
  if grep -q 'AuthenticAMD' /proc/cpuinfo 2>/dev/null; then
    if grep -q 'amd_iommu=on' /proc/cmdline 2>/dev/null; then record "amd-iommu" "PASS"
    else record "amd-iommu" "WARN" "amd_iommu=on not in cmdline (requires reboot; AMD only)"; fi
  fi
  if grep -q 'iommu=pt' /proc/cmdline 2>/dev/null; then record "iommu-pt" "PASS"
  elif grep -q 'iommu=pt' /etc/kernel/cmdline 2>/dev/null; then record "iommu-pt" "WARN" "iommu=pt in /etc/kernel/cmdline but not active (requires reboot)"; fi
  # Memory safety kernel params
  if grep -q 'init_on_free=1' /proc/cmdline 2>/dev/null; then record "init-on-free" "PASS"
  elif grep -q 'init_on_free=1' /etc/kernel/cmdline 2>/dev/null; then
    record "init-on-free" "WARN" "init_on_free=1 in /etc/kernel/cmdline but not active (requires reboot)"
  fi
  if grep -q 'page_alloc.shuffle=1' /proc/cmdline 2>/dev/null; then record "page-alloc-shuffle" "PASS"
  else record "page-alloc-shuffle" "WARN" "page_alloc.shuffle=1 not in cmdline (requires reboot)"; fi
  # AMD CPU power driver (amd-pstate-epp is default on Fedora 44 + Zen 4)
  if grep -q 'AuthenticAMD' /proc/cpuinfo 2>/dev/null; then
    if [[ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver ]]; then
      _pstate=$(<"/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver")
      if [[ "$_pstate" == "amd-pstate-epp" ]]; then record "amd-pstate-epp" "PASS"
      elif [[ "$_pstate" == "amd-pstate" ]]; then
        record "amd-pstate-epp" "WARN" "guided mode ($_pstate) not EPP — check BIOS CPPC setting"
      elif [[ "$_pstate" == "acpi-cpufreq" ]]; then
        record "amd-pstate-epp" "FAIL" "legacy acpi-cpufreq — kernel regression or BIOS CPPC disabled"
      else record "amd-pstate-epp" "WARN" "driver=$_pstate (unexpected)"; fi
    fi
  fi
  # amdgpu runtime PM (-1=auto is correct; 0=off wastes power)
  if [[ -f /sys/module/amdgpu/parameters/runpm ]]; then
    _runpm=$(<"/sys/module/amdgpu/parameters/runpm")
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
  # kernel.panic: read expected value from deployed config (system_kernel_panic defaults to 0 in default.config.yml)
  _panic_expected=$(awk -F' *= *' '/^kernel\.panic /{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "kernel.panic" "${_panic_expected:-0}" "sysctl-panic-reboot"
  # kernel.panic_on_oops: read expected value from deployed config (system_kernel_panic_on_oops may override default 1).
  # Hardcoding 1 here would false-FAIL on machines with system_kernel_panic_on_oops: 0 (OVN-K/bpfman debugging).
  _panic_on_oops_expected=$(awk -F' *= *' '/^kernel\.panic_on_oops/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "kernel.panic_on_oops" "${_panic_on_oops_expected:-0}" "sysctl-panic-on-oops"
  # accept_ra: read expected value from deployed config (system_ipv6_accept_ra in config.yml may override default 0).
  _accept_ra_expected=$(awk -F' *= *' '/^net\.ipv6\.conf\.all\.accept_ra/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "net.ipv6.conf.all.accept_ra" "${_accept_ra_expected:-0}" "sysctl-no-accept-ra"
  _accept_ra_default_expected=$(awk -F' *= *' '/^net\.ipv6\.conf\.default\.accept_ra/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  _sysctl_check "net.ipv6.conf.default.accept_ra" "${_accept_ra_default_expected:-0}" "sysctl-no-accept-ra-default"
  _sysctl_check "net.ipv4.conf.all.rp_filter"         "2" "sysctl-rp-filter"
  _sysctl_check "net.ipv4.conf.default.rp_filter"     "2" "sysctl-rp-filter-default"
  _sysctl_check "net.ipv4.tcp_rfc1337"                "1" "sysctl-tcp-rfc1337"
  _sysctl_check "net.ipv4.conf.all.log_martians"            "1" "sysctl-log-martians"
  _sysctl_check "net.ipv4.conf.default.log_martians"        "1" "sysctl-log-martians-default"
  _sysctl_check "net.ipv4.icmp_echo_ignore_broadcasts"      "1" "sysctl-icmp-no-echo-broadcast"
  _sysctl_check "net.ipv4.icmp_ignore_bogus_error_responses" "1" "sysctl-icmp-no-bogus-error"
  _ip_fwd_expected=$(awk -F' *= *' '/^net\.ipv4\.ip_forward/{print $2}' /etc/sysctl.d/90-hardening.conf 2>/dev/null)
  if [[ -n "$_ip_fwd_expected" ]]; then
    _sysctl_check "net.ipv4.ip_forward" "$_ip_fwd_expected" "sysctl-ip-forward"
  else
    record "sysctl-ip-forward" "WARN" "net.ipv4.ip_forward not in 90-hardening.conf (system_enable_ip_forward: false — Kubernetes/Tailscale routing not expected)"
  fi
  # nf_conntrack_max: module-gated sysctl — WARN if nf_conntrack not yet loaded, FAIL if loaded but wrong
  _nfct=$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null) || true
  if [[ -z "$_nfct" ]]; then record "sysctl-conntrack-max" "WARN" "nf_conntrack module not loaded (net.netfilter.nf_conntrack_max unavailable)"
  elif [[ "$_nfct" -ge "524288" ]]; then record "sysctl-conntrack-max" "PASS"
  else record "sysctl-conntrack-max" "FAIL" "net.netfilter.nf_conntrack_max=$_nfct expected >=524288"; fi
  _sysctl_check "net.core.rmem_max"                    "16777216" "sysctl-rmem-max"
  _sysctl_check "net.core.wmem_max"                    "16777216" "sysctl-wmem-max"
  _sysctl_check "vm.max_map_count"                     "1048576"  "sysctl-vm-max-map-count"
  _sysctl_check "net.ipv6.conf.all.use_tempaddr"       "2"        "sysctl-ipv6-tempaddr"
  _sysctl_check "net.ipv6.conf.default.use_tempaddr"   "2"        "sysctl-ipv6-tempaddr-default"

fi

# ---- File-gated checks (run always; silently skip where system role was not run) ----
# These files ARE deployed by the system role in Fedora/Rocky/Debian container CI;
# [[ -f ]] guards make them no-ops on macOS and container scenarios without the system role.

# kernel module blacklist — always-present entries (n_hdlc, dccp/rds/tipc CIS 3.4.x); vivid/usb_storage skipped
# (cramfs removed from template; usb_storage conditional on system_disable_usb_storage)
if [[ -f /etc/modprobe.d/hardening.conf ]]; then
  if grep -q '^blacklist n_hdlc' /etc/modprobe.d/hardening.conf && \
     grep -q '^install dccp /bin/false' /etc/modprobe.d/hardening.conf && \
     grep -q '^install rds /bin/false' /etc/modprobe.d/hardening.conf && \
     grep -q '^install tipc /bin/false' /etc/modprobe.d/hardening.conf; then
    record "modprobe-hardening" "PASS"
  else record "modprobe-hardening" "FAIL" "modprobe hardening not deployed or missing key blacklist entries"; fi
elif $IS_LINUX && ! $USER_ONLY && [[ -z "$CONTAINER" ]]; then
  record "modprobe-hardening" "FAIL" "/etc/modprobe.d/hardening.conf missing — run: make system"
fi

# NM conf.d content checks — file-gated; silently skip on macOS or where system role was not run
if [[ -f /etc/NetworkManager/conf.d/99-wifi-mac-rand.conf ]]; then
  if grep -q '^wifi.scan-rand-mac-address=yes' /etc/NetworkManager/conf.d/99-wifi-mac-rand.conf; then
    record "wifi-mac-rand" "PASS"
  elif $CSB_HOST; then record "wifi-mac-rand" "WARN" "skipped on CSB — IT may use MAC-based NAC; stable-ssid not deployed"
  else record "wifi-mac-rand" "WARN" "WiFi MAC randomization not configured"; fi
fi
if [[ -f /etc/NetworkManager/conf.d/99-dns.conf ]]; then
  # Skipped on CSB — paired with resolv.conf guard; Ansible deliberately omits both on CSB (DHCP DNS remains active)
  if grep -q '^dns=systemd-resolved' /etc/NetworkManager/conf.d/99-dns.conf; then
    record "nm-dns-resolved" "PASS"
  elif $CSB_HOST; then record "nm-dns-resolved" "WARN" "skipped on CSB — DHCP DNS active via systemd-resolved without NM override"
  else record "nm-dns-resolved" "FAIL" "NM dns=systemd-resolved not configured (/etc/NetworkManager/conf.d/99-dns.conf)"; fi
fi
if [[ -f /etc/NetworkManager/conf.d/99-wifi-powersave.conf ]]; then
  if grep -q '^wifi.powersave=2' /etc/NetworkManager/conf.d/99-wifi-powersave.conf; then
    record "nm-wifi-powersave" "PASS"
  elif $CSB_HOST; then record "nm-wifi-powersave" "WARN" "not deployed on CSB — run 'make all' to apply; Intel WiFi may suffer latency spikes under load until then"
  else record "nm-wifi-powersave" "WARN" "WiFi power saving not disabled (/etc/NetworkManager/conf.d/99-wifi-powersave.conf)"; fi
fi
if [[ -f /etc/NetworkManager/conf.d/tailscale.conf ]]; then
  if grep -q 'interface-name:tailscale' /etc/NetworkManager/conf.d/tailscale.conf; then
    record "nm-tailscale-unmanaged" "PASS"
  else record "nm-tailscale-unmanaged" "FAIL" "tailscale.conf missing or incomplete (/etc/NetworkManager/conf.d/tailscale.conf) — NM may manage kind/OVN/Tailscale interfaces; run: make system"; fi
fi
if [[ -f /etc/NetworkManager/conf.d/99-dhcp-privacy.conf ]]; then
  if grep -q '^ipv4.dhcp-send-hostname=false' /etc/NetworkManager/conf.d/99-dhcp-privacy.conf; then
    record "nm-dhcp-privacy" "PASS"
  else record "nm-dhcp-privacy" "WARN" "DHCP hostname privacy not configured (/etc/NetworkManager/conf.d/99-dhcp-privacy.conf) — run: make system"; fi
fi

# resolved.conf.d/99-dot.conf content check (file-gated; silently skips on macOS or where system role was not run)
if [[ -f /etc/systemd/resolved.conf.d/99-dot.conf ]]; then
  if grep -qE '^DNSOverTLS=' /etc/systemd/resolved.conf.d/99-dot.conf && \
     grep -qE '^Domains=~\.' /etc/systemd/resolved.conf.d/99-dot.conf; then
    record "resolved-dot-conf" "PASS"
  else
    record "resolved-dot-conf" "FAIL" "99-dot.conf missing DNSOverTLS= or Domains=~. directive — run: make system"
  fi
fi

# resolved.conf.d/98-llmnr.conf content check — file-gated; silently skips on macOS or where system role was not run
if [[ -f /etc/systemd/resolved.conf.d/98-llmnr.conf ]]; then
  if grep -q 'LLMNR=no' /etc/systemd/resolved.conf.d/98-llmnr.conf && \
     grep -q 'MulticastDNS=no' /etc/systemd/resolved.conf.d/98-llmnr.conf; then
    record 'llmnr-disabled' 'PASS'
  else
    record 'llmnr-disabled' 'FAIL' '98-llmnr.conf missing LLMNR=no or MulticastDNS=no'
  fi
elif $IS_LINUX && ! $USER_ONLY && [[ -z "$CONTAINER" ]]; then
  record 'llmnr-disabled' 'FAIL' '/etc/systemd/resolved.conf.d/98-llmnr.conf not deployed — run: make system'
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
