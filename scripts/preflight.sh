#!/bin/bash
# preflight.sh — Pre-flight checks for workstation Ansible playbook.
# Exit 0 if ready, 1 if failures. Usage: preflight.sh [--json] [--profile work|personal]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JSON=false PROFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=true; shift ;;
    --profile)
      [[ $# -ge 2 ]] || { echo "Error: --profile requires a value (work|personal)" >&2; exit 2; }
      shift
      case "$1" in
        work|personal) PROFILE="$1"; shift ;;
        *) echo "Error: --profile must be 'work' or 'personal', got '$1'" >&2; exit 2 ;;
      esac ;;
    -h|--help) echo "Usage: $0 [--json] [--profile work|personal]"; exit 0 ;;
    *) echo "Unknown option: $1. Usage: $0 [--json] [--profile work|personal]" >&2; exit 2 ;;
  esac
done
if [[ -t 1 ]] && ! $JSON; then
  RED='\033[0;31m' GRN='\033[0;32m' YLW='\033[0;33m' NC='\033[0m'
else
  RED='' GRN='' YLW='' NC=''
fi
RESULTS=() FAILURES=0
record() {
  local name="$1" status="$2" detail="${3:-}"
  RESULTS+=("${name}|${status}|${detail}")
  if [[ "$JSON" == false ]]; then
    case "$status" in
      pass) printf "${GRN}[PASS]${NC} %s\n" "$name" ;;
      fail) FAILURES=$((FAILURES+1)); printf "${RED}[FAIL]${NC} %s — %s\n" "$name" "$detail" ;;
      warn|skip) printf "${YLW}[%s]${NC} %s — %s\n" "${status^^}" "$name" "$detail" ;;
      *) printf "${RED}[BUG]${NC} unknown status '%s' for check '%s'\n" "$status" "$name" >&2; exit 99 ;;
    esac
  else if [[ "$status" == "fail" ]]; then FAILURES=$((FAILURES+1)); fi; fi
}

# --- OS / CSB / profile detection ---
OS_FAMILY="unknown" IS_CSB=false fapolicyd_installed=false
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    fedora) OS_FAMILY="fedora" ;; rhel|centos|rocky|almalinux) OS_FAMILY="rhel" ;;
  esac
elif [[ "$(uname -s)" == "Darwin" ]]; then OS_FAMILY="darwin"; fi
IS_RHEL=false; [[ "${ID:-}" == "rhel" ]] && IS_RHEL=true
if [[ "$OS_FAMILY" == "unknown" ]]; then
  record "os_family" "warn" "unrecognized OS: $(uname -s) — playbook supports fedora/rhel/darwin"
else
  record "os_family" "pass" "$OS_FAMILY"
fi
if [[ "$OS_FAMILY" == "rhel" || "$OS_FAMILY" == "fedora" ]]; then
  has_certs=false
  for p in '2022-IT-Root-CA.pem' 'Eng-CA.crt' 'RH-IT-Root-CA.pem'; do
    [[ -f "/etc/pki/ca-trust/source/anchors/$p" ]] && has_certs=true && break
  done
  # systemctl cat returns exit 1 when the unit file is absent — more reliable than list-unit-files which may exit 0 with empty output on older systemd
  systemctl cat fapolicyd.service &>/dev/null && fapolicyd_installed=true
  if [[ "$IS_RHEL" == true ]]; then
    [[ "$has_certs" == true && "$fapolicyd_installed" == true ]] && IS_CSB=true
  else
    # Fedora CSB: FQDN ends in .csb AND internal CA present (fapolicyd not required)
    fqdn=$(hostname -f 2>/dev/null || hostname)
    [[ "$fqdn" == *.csb && "$has_certs" == true ]] && IS_CSB=true
  fi
fi
if [[ "$IS_CSB" == true ]]; then
  if [[ "$fapolicyd_installed" == true ]]; then
    record "csb_detected" "warn" "CSB detected (fapolicyd installed) — see fapolicyd and container_tier checks below for enforcement status"
  else
    record "csb_detected" "warn" "CSB detected (no fapolicyd) — hybrid mode; may need --ask-become-pass"
  fi
else
  record "csb_detected" "pass" "not CSB"
fi
CONFIG_FILE="$SCRIPT_DIR/../config.yml"
if [[ -z "$PROFILE" ]]; then
  PROFILE="work"  # default matches default.config.yml; overridden by macOS detection or config.yml profile: line below
  [[ "$OS_FAMILY" == "darwin" ]] && PROFILE="personal"
  # Apply config.yml profile override in both directions
  if grep -qE '^profile:[[:space:]]*["'"'"']?work["'"'"']?([[:space:]]|$)' "$CONFIG_FILE" 2>/dev/null; then
    PROFILE=work
  elif grep -qE '^profile:[[:space:]]*["'"'"']?personal["'"'"']?([[:space:]]|$)' "$CONFIG_FILE" 2>/dev/null; then
    PROFILE=personal
  elif grep -qE '^profile:[[:space:]]' "$CONFIG_FILE" 2>/dev/null; then
    record "config_profile" "fail" "unrecognized profile value in config.yml — set 'work' or 'personal'"
  fi
fi

# --- Required tools ---
# When adding a tool to the loop, add a matching elif branch with install guidance.
for tool in ansible-playbook git python3 curl make ssh; do
  if command -v "$tool" &>/dev/null; then
    if [[ "$tool" == "ssh" ]]; then
      ver=$(ssh -V 2>&1 | head -1) || ver="installed"
    else
      ver=$("$tool" --version 2>/dev/null | head -1) || ver="installed"
    fi
    record "required_${tool}" "pass" "$ver"
  else
    if [[ "$tool" == "make" ]]; then
      record "required_${tool}" "fail" "not installed — install first: sudo dnf install make (Fedora/RHEL) | brew install make (macOS), then: make bootstrap"
    elif [[ "$tool" == "ssh" ]]; then
      record "required_${tool}" "fail" "not installed — run: sudo dnf install openssh-clients (Fedora/RHEL) | brew install openssh (macOS)"
    elif [[ "$tool" == "curl" ]]; then
      record "required_${tool}" "fail" "not installed — run: sudo dnf install curl (Fedora/RHEL) | brew install curl (macOS)"
    elif [[ "$tool" == "python3" ]]; then
      record "required_${tool}" "fail" "not installed — run: sudo dnf install python3 (Fedora/RHEL) | brew install python3 (macOS)"
    elif [[ "$tool" == "git" ]]; then
      record "required_${tool}" "fail" "not installed — run: sudo dnf install git (Fedora/RHEL) | brew install git (macOS)"
    elif [[ "$tool" == "ansible-playbook" ]]; then
      if command -v make &>/dev/null; then
        record "required_${tool}" "fail" "not installed — run: make bootstrap"
      else
        record "required_${tool}" "fail" "not installed — install make first (dnf/brew), then: make bootstrap"
      fi
    else
      record "required_${tool}" "fail" "not installed — add install guidance in preflight.sh elif chain"
    fi
  fi
done
if command -v shellcheck &>/dev/null; then
  record "linttools_shellcheck" "pass" "$(shellcheck --version | awk '/^version:/{print "shellcheck " $2}')"
else
  if [[ "$OS_FAMILY" == "darwin" ]]; then
    record "linttools_shellcheck" "warn" "not installed — needed for make lint/CI (run: brew install shellcheck or: make bootstrap)"
  else
    record "linttools_shellcheck" "warn" "not installed — needed for make lint/CI (run: sudo dnf install ShellCheck or: make bootstrap)"
  fi
fi

# --- Ansible collections ---
missing_cols=()
for col in community.general containers.podman ansible.posix; do
  [[ -d "${SCRIPT_DIR}/../collections/ansible_collections/${col//.//}" ]] || missing_cols+=("$col")
done
if [[ ${#missing_cols[@]} -eq 0 ]]; then
  record "ansible_collections" "pass" "all required collections installed"
else
  record "ansible_collections" "fail" "missing: ${missing_cols[*]} — run: make bootstrap"
fi

# --- YubiKey presence ---
yk_found=false
yk_detect_possible=true
if command -v lsusb &>/dev/null && lsusb 2>/dev/null | grep -qiE "yubico|1050:"; then
  yk_found=true
elif command -v ykman &>/dev/null && ykman info &>/dev/null; then
  yk_found=true
else
  if ! command -v lsusb &>/dev/null && ! command -v ykman &>/dev/null; then
    yk_detect_possible=false
  fi
fi
if [[ "$yk_found" == true ]]; then
  record "yubikey_present" "pass" "detected"
  if command -v ykchalresp &>/dev/null; then
    if [[ "$JSON" == false ]]; then
      if ! command -v timeout &>/dev/null; then
        record "yubikey_chalresp" "skip" "timeout not available (install gnu-coreutils on macOS)"
      else
        echo "Touch your YubiKey for HMAC-SHA1 challenge-response test..." >&2
        if timeout 15 ykchalresp -2 "preflight-test" &>/dev/null; then
          record "yubikey_chalresp" "pass" "Slot 2 HMAC-SHA1 responding"
        else
          record "yubikey_chalresp" "warn" "Slot 2 no response within 15s — touch YubiKey when prompted, or verify HMAC-SHA1 slot 2 is configured"
        fi
      fi
    else
      record "yubikey_chalresp" "skip" "skipped in --json mode (interactive)"
    fi
  else
    record "yubikey_chalresp" "skip" "ykchalresp not installed (need ykpers)"
  fi
else
  if [[ "$yk_detect_possible" == false ]]; then
    record "yubikey_present" "skip" "lsusb/ykman not found — cannot detect YubiKey presence"
  else
    record "yubikey_present" "warn" "no YubiKey detected — vault uses stub password until configured"
  fi
fi

# --- Vault password scripts ---
vscript="${SCRIPT_DIR}/vault-pass.sh"
if [[ -x "$vscript" ]] && [[ "$JSON" == true ]]; then
  record "vault" "skip" "skipped in --json mode (interactive)"
elif [[ -x "$vscript" ]]; then
  output=$("$vscript" 2>/dev/null) || true
  len=${#output}
  if [[ $len -ge 8 ]]; then
    if [[ "$output" == *ci-dummy-vault-password* ]]; then
      record "vault" "warn" "vault-pass.sh is still the CI dummy stub — replace with YubiKey HMAC-SHA1 implementation before encrypting vault.yml (see SECURITY.md 'Setting Up vault-pass.sh')"
    else
      record "vault" "pass" "script returned valid password"
    fi
  elif [[ $len -gt 0 ]]; then
    record "vault" "fail" "script returned only ${len} chars — vault password too short"
  else
    record "vault" "fail" "script returned empty output"
  fi
elif [[ -f "$vscript" ]]; then
  record "vault" "fail" "script exists but is not executable"
elif [[ -f "${HOME}/.vault_pass" ]]; then
  record "vault" "warn" "no script, using ~/.vault_pass file (password on disk)"
else
  record "vault" "fail" "no script and no ~/.vault_pass"
fi

# vault_encrypted is a file-only check — no YubiKey interaction needed — runs in --json mode unlike the vault script test above
# --- Vault encryption check ---
VAULT_FILE="$SCRIPT_DIR/../group_vars/all/vault.yml"
if [[ -f "$VAULT_FILE" ]]; then
  # shellcheck disable=SC2016  # Intentional: matching literal $ANSIBLE_VAULT header
  if head -1 "$VAULT_FILE" | grep -q '^\$ANSIBLE_VAULT'; then
    record "vault_encrypted" "pass" "vault.yml is encrypted"
  else
    if grep -q 'vault_placeholder' "$VAULT_FILE" 2>/dev/null; then
      record "vault_encrypted" "warn" "vault.yml is plaintext stub — populate with real secrets then encrypt (see CLAUDE.md step 3)"
    else
      record "vault_encrypted" "warn" "vault.yml is plaintext without vault_placeholder sentinel — encrypt or add vault_placeholder stub"
    fi
  fi
else
  record "vault_encrypted" "fail" "group_vars/all/vault.yml missing — create from vault.yml.example or decrypt from backup"
fi

# --- config.yml ---
if [[ ! -f "$CONFIG_FILE" ]]; then
  record "config_yml" "fail" "config.yml missing — create it before running make all (Play 1 runs fully before Play 2 checks identity; SSH port moves to 722 and kernel hardening applies before the CHANGE_ME assert fires)"
elif ! grep -q '^desktop_environment:' "$CONFIG_FILE"; then
  record "config_yml" "fail" "config.yml exists but does not set desktop_environment — 'auto' detection fails before any WM is installed; set 'desktop_environment: sway' (or i3/gnome)"
# Intentional: 'auto' subset of the generic check below; gives a more actionable error for the common mistake
elif grep -qE '^desktop_environment:[[:space:]]*["'"'"']?auto["'"'"']?([[:space:]]|$)' "$CONFIG_FILE"; then
  record "config_yml" "fail" "desktop_environment is 'auto' — auto-detection requires an active XDG session and will fail on first provision; set 'desktop_environment: sway' (or i3/gnome) in config.yml"
elif ! grep -qE '^desktop_environment:[[:space:]]*["'"'"']?(sway|i3|gnome)["'"'"']?([[:space:]]|$)' "$CONFIG_FILE"; then
  record "config_yml" "fail" "desktop_environment must be sway, i3, or gnome — got: $(grep '^desktop_environment:' "$CONFIG_FILE")"
else
  record "config_yml" "pass" "desktop_environment is set"
fi

# --- Identity vars CHANGE_ME check ---
if [[ -f "$CONFIG_FILE" ]]; then
  for _ivar in dotfiles_user_name dotfiles_github_user dotfiles_user_email_work dotfiles_user_email_personal system_timezone; do
    if [[ "$_ivar" == "dotfiles_user_email_work" && "$PROFILE" == "personal" ]]; then continue; fi
    if ! grep -q "^${_ivar}:" "$CONFIG_FILE"; then
      if [[ "$_ivar" == "system_timezone" ]]; then
        record "identity_${_ivar}" "fail" "${_ivar} not set in config.yml — timedatectl set-timezone CHANGE_ME will fail; run timedatectl list-timezones to find yours"
      else
        record "identity_${_ivar}" "fail" "${_ivar} not set in config.yml — provisioning uses 'CHANGE_ME' placeholder, producing wrong gitconfig/zshrc"
      fi
    elif grep -qE "^${_ivar}:[[:space:]]*['\"]?CHANGE_ME" "$CONFIG_FILE"; then
      record "identity_${_ivar}" "fail" "${_ivar} is still 'CHANGE_ME' in config.yml — set a real value before running make all"
    else
      record "identity_${_ivar}" "pass" "${_ivar} is set in config.yml"
    fi
  done
fi

# --- vault_* key guard (mirrors pre_flight_checks.yml assert) ---
if [[ -f "$CONFIG_FILE" ]]; then
  if grep -qE '^vault_[^:]*:' "$CONFIG_FILE"; then
    record "config_vault_keys" "fail" "config.yml defines vault_* key(s) — include_vars outranks group_vars (precedence 17 > 4), silently shadowing the encrypted vault value; remove vault_* keys from config.yml"
  else
    record "config_vault_keys" "pass" "no vault_* keys in config.yml"
  fi
fi

# --- Network connectivity ---
net_urls=("github=https://github.com" "galaxy=https://galaxy.ansible.com")
[[ "$PROFILE" == "work" ]] && net_urls+=("registry=https://registry.redhat.io")
_curl_ok=false; command -v curl &>/dev/null && _curl_ok=true
for netlabel_url in "${net_urls[@]}"; do
  nlabel="${netlabel_url%%=*}" nurl="${netlabel_url#*=}"
  if [[ $_curl_ok == true ]]; then
    if curl -sSLf --max-time 10 -o /dev/null "$nurl" 2>/dev/null; then
      record "net_${nlabel}" "pass" "$nurl reachable"
    else record "net_${nlabel}" "fail" "$nurl unreachable"; fi
  else
    record "net_${nlabel}" "skip" "curl not installed"
  fi
done

# --- registry.redhat.io authentication (required for RH subctl download) ---
if [[ "$PROFILE" == "work" ]]; then
  if command -v podman &>/dev/null; then
    if podman login --get-login registry.redhat.io &>/dev/null; then
      record "registry_redhat_auth" "pass" "authenticated with registry.redhat.io"
    else
      record "registry_redhat_auth" "warn" "not authenticated with registry.redhat.io — run 'podman login registry.redhat.io' before using packages_subctl_rh_versions"
    fi
  else
    record "registry_redhat_auth" "skip" "podman not installed"
  fi
fi

# --- fapolicyd detection (Linux only) ---
FAPOLICYD_BLOCKING=false
if [[ "$OS_FAMILY" == "rhel" || "$OS_FAMILY" == "fedora" ]]; then
  if systemctl is-active fapolicyd &>/dev/null; then
    if grep -qiE '^permissive[[:space:]]*=[[:space:]]*1([[:space:]]*(#.*))?$' /etc/fapolicyd/fapolicyd.conf 2>/dev/null; then
      record "fapolicyd" "warn" "active but permissive mode (permissive=1 in config) — /tmp execution allowed"
    else
      FAPOLICYD_BLOCKING=true
      pl=$(awk -F= '/^\[/{section=$0} /^pipelining/ && section ~ /\[defaults\]/{gsub(/ /,"",$2); gsub(/#.*$/,"",$2); print $2}' "$SCRIPT_DIR/../ansible.cfg" 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)
      pl=${pl:-false (default — not set in ansible.cfg)}
      if [[ "$pl" == "true" || "$pl" == "yes" || "$pl" == "on" || "$pl" == "1" ]]; then
        record "fapolicyd" "warn" "active and enforcing — mitigated by pipelining=true in ansible.cfg"
      else
        record "fapolicyd" "warn" "active and enforcing — pipelining=$pl in ansible.cfg — fapolicyd will block /tmp execution"
      fi
    fi
  else
    record "fapolicyd" "pass" "not active"
  fi
fi

# --- Container tier derivation ---
if [[ "$IS_CSB" == true && "$FAPOLICYD_BLOCKING" == true ]]; then
  record "container_tier" "warn" "container — fapolicyd enforcing: run 'make container' for dev tools; 'make all' will be restricted"
elif [[ "$IS_CSB" == true || "$IS_RHEL" == true ]]; then
  record "container_tier" "warn" "hybrid — run 'make all' for host setup, then 'make container' for dev tools"
else
  record "container_tier" "pass" "host-only — run 'make all' for full provisioning"
fi

# --- Transcrypt (for notes repo) ---
notes_enabled=false
grep -qE '^notes_enabled:[[:space:]]*true([[:space:]]|$)' "$CONFIG_FILE" 2>/dev/null && notes_enabled=true
if command -v transcrypt &>/dev/null; then
  record "installed_transcrypt" "pass" "$(transcrypt --version 2>&1)"
else
  if [[ "$notes_enabled" == true ]]; then
    record "installed_transcrypt" "warn" "not installed — needed for encrypted notes repo"
  else
    record "installed_transcrypt" "skip" "not installed — only needed when notes_enabled: true in config.yml"
  fi
fi

# --- Sudo scope ---
if sudo_out=$(sudo -n -l 2>/dev/null); then
  if grep -qE '\(ALL[^)]*\)[[:space:]]+NOPASSWD:[[:space:]]*ALL' <<< "$sudo_out"; then
    record "sudo" "pass" "full sudo available"
  else
    record "sudo" "warn" "scoped sudo — some system tasks may fail"
  fi
else
  record "sudo" "warn" "no passwordless sudo — will need --ask-become-pass"
fi

# --- Disk space (need 5GB free in $HOME) ---
avail_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}'); if [[ -z "$avail_kb" ]]; then
  record "disk_space" "warn" "df failed on $HOME — cannot measure free space"
else
  avail_gb=$((avail_kb / 1048576))
  avail_mb=$((avail_kb / 1024))
  if [[ $avail_gb -ge 5 ]]; then record "disk_space" "pass" "${avail_gb}GiB free in \$HOME"
  else record "disk_space" "fail" "only ${avail_mb}MiB free — need at least 5GiB"; fi
fi

# --- RAM ---
if [[ "$OS_FAMILY" == "darwin" ]]; then ram_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1073741824 ))
elif [[ "$OS_FAMILY" == "unknown" ]]; then record 'ram' 'skip' 'unknown OS — cannot read RAM'
else ram_gb=$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null); fi
if [[ "$OS_FAMILY" != "unknown" ]]; then
  ram_gb=${ram_gb:-0}  # guard: awk exits 0 with empty output when MemTotal absent
  if [[ $ram_gb -ge 8 ]]; then record "ram" "pass" "${ram_gb}GiB"
  elif [[ $ram_gb -ge 4 ]]; then record "ram" "warn" "${ram_gb}GiB — 8GiB+ recommended"
  else record "ram" "fail" "${ram_gb}GiB — insufficient"; fi
fi

# --- SSH session safety check ---
# make all restarts sshd mid-play; an SSH session gets SIGHUP and dies, leaving
# provisioning incomplete. Run from a local console or inside tmux/screen.
# Inside tmux/screen the multiplexer session survives sshd restart — safe to continue.
if [[ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ]]; then
  if [[ -n "${TMUX:-}${STY:-}" ]]; then
    record "ssh_session" "warn" "running over SSH inside tmux/screen — sshd restart drops the SSH connection but the multiplexer session survives; reconnect and reattach after provisioning"
  else
    record "ssh_session" "fail" "running over SSH — sshd will restart mid-play and drop this connection. Use a local console or run inside tmux: tmux new-session 'make all'"
  fi
else
  record "ssh_session" "pass" "local console (not over SSH)"
fi

# --- Existing installations ---
for tool in claude podman distrobox toolbox; do
  if command -v "$tool" &>/dev/null; then
    ver=$("$tool" --version 2>/dev/null | head -1) || ver="installed"
    record "installed_${tool}" "pass" "$ver"
  else
    record "installed_${tool}" "skip" "not found"
  fi
done

# --- Output ---
if [[ "$JSON" == true ]]; then
  printf '{"os_family":"%s","is_csb":%s,"profile":"%s","checks":[' "$OS_FAMILY" "$IS_CSB" "$PROFILE"
  first=true
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r name status detail <<< "$r"
    detail="${detail//\\/\\\\}"; detail="${detail//\"/\\\"}"; detail="${detail//$'\n'/\\n}"; detail="${detail//$'\t'/\\t}"
    [[ "$first" == true ]] && first=false || printf ','
    printf '{"name":"%s","status":"%s","detail":"%s"}' "$name" "$status" "$detail"
  done
  printf '],"ready":%s}\n' "$( [[ $FAILURES -eq 0 ]] && echo true || echo false )"
else
  echo ""
  if [[ $FAILURES -eq 0 ]]; then
    printf "${GRN}Ready to run 'make all'${NC}\n"
  else
    printf "${RED}%d check(s) failed — resolve before running 'make all'${NC}\n" "$FAILURES"
  fi
fi
exit $(( FAILURES > 0 ? 1 : 0 ))
