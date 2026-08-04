#!/bin/bash
# preflight.sh — Pre-flight checks for workstation Ansible playbook.
# Exit 0 if ready, 1 if failures. Usage: preflight.sh [--json] [--profile work|personal]
set -euo pipefail
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
    -h|--help) echo "Usage: $0 [--json] [--profile work|personal]" >&2; exit 0 ;;
    *) echo "Unknown option: $1. Usage: $0 [--json] [--profile work|personal]" >&2; exit 2 ;;
  esac
done
RED='\033[0;31m' GRN='\033[0;32m' YLW='\033[0;33m' NC='\033[0m'
RESULTS=() FAILURES=0
record() {
  local name="$1" status="$2" detail="${3:-}"
  RESULTS+=("${name}|${status}|${detail}")
  if [[ "$JSON" == false ]]; then
    case "$status" in
      pass) printf "${GRN}[PASS]${NC} %s\n" "$name" ;;
      fail) printf "${RED}[FAIL]${NC} %s — %s\n" "$name" "$detail"; FAILURES=$((FAILURES+1)) ;;
      warn|skip) printf "${YLW}[${status^^}]${NC} %s — %s\n" "$name" "$detail" ;;
    esac
  else [[ "$status" == "fail" ]] && FAILURES=$((FAILURES+1)) || true; fi
}

# --- OS / CSB / profile detection ---
OS_FAMILY="unknown" IS_CSB=false
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "$ID" in
    fedora) OS_FAMILY="fedora" ;; rhel|centos|rocky|almalinux) OS_FAMILY="rhel" ;;
  esac
elif [[ "$(uname -s)" == "Darwin" ]]; then OS_FAMILY="darwin"; fi
if [[ "$OS_FAMILY" == "unknown" ]]; then
  record "os_family" "warn" "unrecognized OS: $(uname -s) — playbook supports fedora/rhel/darwin"
else
  record "os_family" "pass" "$OS_FAMILY"
fi
if [[ "$OS_FAMILY" == "rhel" || "$OS_FAMILY" == "fedora" ]]; then
  has_certs=false has_fapolicyd=false
  for p in '2022-IT-Root-CA.pem' 'Eng-CA.crt' 'RH-IT-Root-CA.pem'; do
    [[ -f "/etc/pki/ca-trust/source/anchors/$p" ]] && has_certs=true && break
  done
  # Use list-unit-files (installed) not is-active (running) to match Ansible's csb_detect.yml
  systemctl list-unit-files fapolicyd.service &>/dev/null && has_fapolicyd=true
  if [[ "$OS_FAMILY" == "rhel" ]]; then
    [[ "$has_certs" == true && "$has_fapolicyd" == true ]] && IS_CSB=true
  else
    # Fedora CSB: FQDN ends in .csb AND internal CA present (fapolicyd not required)
    fqdn=$(hostname -f 2>/dev/null || hostname)
    [[ "$fqdn" == *.csb && "$has_certs" == true ]] && IS_CSB=true
  fi
fi
if [[ "$IS_CSB" == true ]]; then
  if [[ "$has_fapolicyd" == true ]]; then
    record "csb_detected" "warn" "CSB detected (fapolicyd installed) — expect sudo and fapolicyd constraints"
  else
    record "csb_detected" "warn" "CSB detected (no fapolicyd) — hybrid mode; may need --ask-become-pass"
  fi
else
  record "csb_detected" "pass" "not CSB"
fi
if [[ -z "$PROFILE" ]]; then
  if [[ "$IS_CSB" == true ]]; then PROFILE="work"; else PROFILE="personal"; fi
  [[ "$OS_FAMILY" == "darwin" ]] && PROFILE="personal"
  # Promote to work if config.yml explicitly sets profile: work (e.g. non-CSB work machine)
  _cfg="$(cd "$(dirname "$0")/.." && pwd)/config.yml"
  grep -qE '^profile:[[:space:]]*work([[:space:]]|$)' "$_cfg" 2>/dev/null && PROFILE="work"
  unset _cfg
fi
record "profile" "pass" "$PROFILE"

# --- Required tools ---
for tool in ansible-playbook ansible-vault git python3 curl make ssh; do
  if command -v "$tool" &>/dev/null; then
    ver=$("$tool" --version 2>/dev/null | head -1) || ver="installed"
    record "required_${tool}" "pass" "$ver"
  else
    if [[ "$tool" == "make" ]]; then
      record "required_${tool}" "fail" "not installed — install first: sudo dnf install make (Fedora/RHEL) | brew install make (macOS), then: make bootstrap"
    elif command -v make &>/dev/null; then
      record "required_${tool}" "fail" "not installed — run: make bootstrap"
    else
      record "required_${tool}" "fail" "not installed — install make first (see required_make), then run: make bootstrap"
    fi
  fi
done

# --- Ansible collections ---
if command -v ansible-galaxy &>/dev/null; then
  missing_cols=()
  for col in community.general containers.podman ansible.posix; do
    ansible-galaxy collection list "$col" 2>/dev/null | grep -q "^$col " || missing_cols+=("$col")
  done
  if [[ ${#missing_cols[@]} -eq 0 ]]; then
    record "ansible_collections" "pass" "all required collections installed"
  else
    record "ansible_collections" "fail" "missing: ${missing_cols[*]} — run: make bootstrap"
  fi
else
  record "ansible_collections" "skip" "ansible-galaxy not found"
fi

# --- YubiKey presence ---
yk_found=false
if command -v lsusb &>/dev/null && lsusb 2>/dev/null | grep -qi "yubico\|1050:"; then
  yk_found=true
elif command -v ykman &>/dev/null && ykman info &>/dev/null; then
  yk_found=true
fi
if [[ "$yk_found" == true ]]; then
  record "yubikey_present" "pass" "detected"
  if command -v ykchalresp &>/dev/null; then
    echo "Touch your YubiKey for HMAC-SHA1 challenge-response test..." >&2
    if timeout 5 ykchalresp -2 "preflight-test" &>/dev/null; then
      record "yubikey_chalresp" "pass" "Slot 2 HMAC-SHA1 responding"
    else
      record "yubikey_chalresp" "warn" "Slot 2 challenge-response failed — HMAC-SHA1 configured?"
    fi
  else
    record "yubikey_chalresp" "skip" "ykchalresp not installed (need ykpers)"
  fi
else
  record "yubikey_present" "warn" "no YubiKey detected — vault uses stub password until configured"
fi

# --- Vault password scripts ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
vscript="${SCRIPT_DIR}/vault-pass.sh"
if [[ -x "$vscript" ]]; then
  output=$("$vscript" 2>/dev/null) || true
  len=${#output}
  if [[ $len -ge 8 ]]; then
    record "vault" "pass" "script returned valid password"
  elif [[ $len -gt 0 ]]; then
    record "vault" "warn" "script returned only ${len} chars"
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

# --- Vault encryption check ---
VAULT_FILE="$(cd "$(dirname "$0")/.." && pwd)/group_vars/all/vault.yml"
if [[ -f "$VAULT_FILE" ]]; then
  # shellcheck disable=SC2016  # Intentional: matching literal $ANSIBLE_VAULT header
  if head -1 "$VAULT_FILE" | grep -q '^\$ANSIBLE_VAULT'; then
    record "vault_encrypted" "pass" "vault.yml is encrypted"
  else
    record "vault_encrypted" "warn" "vault.yml is plaintext — encrypt before use with real secrets"
  fi
fi

# --- config.yml ---
CONFIG_FILE="$(cd "$(dirname "$0")/.." && pwd)/config.yml"
if [[ ! -f "$CONFIG_FILE" ]]; then
  record "config_yml" "fail" "config.yml missing — create it before running make all (Play 1 runs fully before Play 2 checks identity; SSH port moves to 722 and kernel hardening applies before the CHANGE_ME assert fires)"
elif ! grep -q '^desktop_environment:' "$CONFIG_FILE"; then
  record "config_yml" "fail" "config.yml exists but does not set desktop_environment — 'auto' detection fails before any WM is installed; set 'desktop_environment: sway' (or i3/gnome)"
elif grep -qE '^desktop_environment:[[:space:]]*auto([[:space:]]|$)' "$CONFIG_FILE"; then
  record "config_yml" "fail" "desktop_environment is 'auto' — auto-detection requires an active XDG session and will fail on first provision; set 'desktop_environment: sway' (or i3/gnome) in config.yml"
else
  record "config_yml" "pass" "desktop_environment is set"
fi

# --- Identity vars CHANGE_ME check ---
if [[ -f "$CONFIG_FILE" ]]; then
  for _ivar in dotfiles_user_name dotfiles_github_user dotfiles_user_email_work dotfiles_user_email_personal; do
    if ! grep -q "^${_ivar}:" "$CONFIG_FILE"; then
      record "identity_${_ivar}" "warn" "${_ivar} not set in config.yml — provisioning uses 'CHANGE_ME' placeholder, producing wrong gitconfig/zshrc"
    elif grep -qE "^${_ivar}:[[:space:]]*['\"]?CHANGE_ME" "$CONFIG_FILE"; then
      record "identity_${_ivar}" "fail" "${_ivar} is still 'CHANGE_ME' in config.yml — set a real value before running make all"
    else
      record "identity_${_ivar}" "pass" "${_ivar} is set in config.yml"
    fi
  done
fi

# --- vault_* key guard (mirrors pre_flight_checks.yml assert) ---
if [[ -f "$CONFIG_FILE" ]]; then
  if grep -qE '^vault_[a-zA-Z_]+:' "$CONFIG_FILE"; then
    record "config_vault_keys" "fail" "config.yml defines vault_* key(s) — include_vars outranks group_vars (precedence 17 > 4), silently shadowing the encrypted vault value; remove vault_* keys from config.yml"
  else
    record "config_vault_keys" "pass" "no vault_* keys in config.yml"
  fi
fi

# --- Network connectivity ---
net_urls=("github=https://github.com" "galaxy=https://galaxy.ansible.com")
[[ "$PROFILE" == "work" ]] && net_urls+=("registry=https://registry.redhat.io")
for netlabel_url in "${net_urls[@]}"; do
  nlabel="${netlabel_url%%=*}" nurl="${netlabel_url#*=}"
  if command -v curl &>/dev/null; then
    if curl -sSL --max-time 10 -o /dev/null "$nurl" 2>/dev/null; then
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
if [[ "$OS_FAMILY" != "darwin" ]]; then
  if systemctl is-active fapolicyd &>/dev/null; then
    tmpscript=$(mktemp /tmp/preflight-fap-XXXXXX.sh)
    printf '#!/bin/bash\n' > "$tmpscript" && chmod +x "$tmpscript"
    if "$tmpscript" &>/dev/null; then
      record "fapolicyd" "warn" "active but /tmp execution allowed"
    else
      FAPOLICYD_BLOCKING=true
      record "fapolicyd" "warn" "active and blocking /tmp execution — mitigated by pipelining=true in ansible.cfg"
    fi
    rm -f "$tmpscript"
  else
    record "fapolicyd" "pass" "not active"
  fi
fi

# --- Container tier derivation ---
if [[ "$IS_CSB" == true ]]; then
  if [[ "$FAPOLICYD_BLOCKING" == true ]]; then
    record "container_tier" "warn" "container — fapolicyd enforcing: run 'make container' for dev tools; 'make all' will be restricted"
  else
    record "container_tier" "warn" "hybrid — run 'make all' for host setup, then 'make container' for dev tools"
  fi
else
  record "container_tier" "pass" "host-only — run 'make all' for full provisioning"
fi

# --- Transcrypt (for notes repo) ---
if command -v transcrypt &>/dev/null; then
  record "installed_transcrypt" "pass" "$(transcrypt --version 2>&1)"
else
  record "installed_transcrypt" "warn" "not installed — needed for encrypted notes repo"
fi

# --- Sudo scope ---
if sudo -n -l &>/dev/null; then
  sudo_out=$(sudo -n -l 2>/dev/null) || true
  if printf '%s' "$sudo_out" | grep -qE '\(ALL[^)]*\)[[:space:]]+NOPASSWD:[[:space:]]+ALL'; then
    record "sudo" "pass" "full sudo available"
  else
    record "sudo" "warn" "scoped sudo — some system tasks may fail"
  fi
else
  record "sudo" "warn" "no passwordless sudo — will need --ask-become-pass"
fi

# --- Disk space (need 5GB free in $HOME) ---
avail_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}') || avail_kb=0
avail_kb="${avail_kb:-0}"
avail_gb=$((avail_kb / 1048576))
if [[ $avail_gb -ge 5 ]]; then
  record "disk_space" "pass" "${avail_gb}GB free in \$HOME"
else
  record "disk_space" "fail" "only ${avail_gb}GB free — need at least 5GB"
fi

# --- RAM ---
if [[ "$OS_FAMILY" == "darwin" ]]; then ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
else ram_gb=$(awk '/MemTotal/ {printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0); fi
if [[ $ram_gb -ge 8 ]]; then record "ram" "pass" "${ram_gb}GB"
elif [[ $ram_gb -ge 4 ]]; then record "ram" "warn" "${ram_gb}GB — 8GB+ recommended"
else record "ram" "fail" "${ram_gb}GB — insufficient"; fi

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
  [[ $FAILURES -eq 0 ]] \
    && printf "${GRN}Ready to run 'make all'${NC}\n" \
    || printf "${RED}%d check(s) failed — resolve before running 'make all'${NC}\n" "$FAILURES"
fi
exit $(( FAILURES > 0 ? 1 : 0 ))
