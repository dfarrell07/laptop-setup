#!/usr/bin/env bash
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
  [[ "$status" =~ ^(pass|fail|warn|skip)$ ]] || { echo "BUG: unknown status '$status' for '$name'" >&2; exit 99; }
  RESULTS+=("${name}|${status}|${detail}")
  [[ "$status" != fail ]] || FAILURES=$((FAILURES+1))
  if [[ "$JSON" == false ]]; then
    case "$status" in
      pass) printf "${GRN}[PASS]${NC} %s\n" "$name" ;;
      fail) printf "${RED}[FAIL]${NC} %s — %s\n" "$name" "$detail" ;;
      warn) printf "${YLW}[WARN]${NC} %s — %s\n" "$name" "$detail" ;;
      skip) printf "${YLW}[SKIP]${NC} %s — %s\n" "$name" "$detail" ;;
    esac
  fi
  return 0  # recording a successful check must not trigger set -e
}

# --- OS / CSB / profile detection ---
OS_FAMILY="unknown"
IS_CSB=false
fapolicyd_installed=false
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    fedora) OS_FAMILY="fedora" ;; rhel|centos|rocky|almalinux) OS_FAMILY="rhel" ;;
    debian) OS_FAMILY="debian" ;;
  esac
elif [[ "$(uname -s)" == "Darwin" ]]; then OS_FAMILY="darwin"; fi
IS_RHEL=false; [[ "${ID:-}" == "rhel" ]] && IS_RHEL=true
if [[ "$OS_FAMILY" == "unknown" ]]; then
  record "os_family" "warn" "unrecognized OS: $(uname -s) — playbook supports fedora/rhel/debian/darwin"
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
    # Fedora CSB (mirrors csb_detect.yml): internal CA present AND (FQDN ends in .csb OR fapolicyd installed)
    fqdn=$(hostname -f 2>/dev/null || hostname 2>/dev/null || true)
    [[ "$has_certs" == true && ( "$fqdn" == *.csb || "$fapolicyd_installed" == true ) ]] && IS_CSB=true
  fi
fi
if [[ "$IS_CSB" == true ]]; then
  if [[ "$fapolicyd_installed" == true ]]; then
    record "csb_detected" "warn" "CSB detected (fapolicyd installed) — see fapolicyd and container_tier checks below for enforcement status"
  else
    record "csb_detected" "warn" "CSB detected (no fapolicyd) — hybrid mode"
  fi
else
  record "csb_detected" "pass" "not CSB"
fi
CONFIG_FILE="$SCRIPT_DIR/../config.yml"
if [[ -z "$PROFILE" ]]; then
  PROFILE="work"  # default matches group_vars/all/vars.yml; overridden by the config.yml profile: line below
  # Apply config.yml profile override in both directions
  if [[ -f "$CONFIG_FILE" ]]; then
    if grep -qE '^profile:[[:space:]]*["'"'"']?work["'"'"']?([[:space:]]|$)' "$CONFIG_FILE" 2>/dev/null; then
      PROFILE=work
      record "config_profile" "pass" "profile=work"
    elif grep -qE '^profile:[[:space:]]*["'"'"']?personal["'"'"']?([[:space:]]|$)' "$CONFIG_FILE" 2>/dev/null; then
      PROFILE=personal
      record "config_profile" "pass" "profile=personal"
    elif grep -qE '^profile:[[:space:]]*' "$CONFIG_FILE" 2>/dev/null; then
      record "config_profile" "fail" "unrecognized profile value in config.yml — set 'work' or 'personal'"
    else
      record "config_profile" "pass" "profile=${PROFILE} (default — no override in config.yml)"
    fi
  else
    record "config_profile" "skip" "config.yml absent — profile defaults to ${PROFILE}"
  fi
else
  record "config_profile" "pass" "profile=${PROFILE} (set via --profile CLI flag)"
fi

# --- Required tools ---
_curl_ok=false
for tool in ansible-playbook git python3 curl make ssh; do
  if command -v "$tool" &>/dev/null; then
    if [[ "$tool" == "ssh" ]]; then
      ver=$(ssh -V 2>&1 | head -1) || ver="installed"
    else
      ver=$("$tool" --version 2>/dev/null | head -1) || ver="installed"
    fi
    record "required_${tool}" "pass" "$ver"
    if [[ "$tool" == "ssh" && "$OS_FAMILY" == "darwin" ]]; then
      _ssh_path=$(command -v ssh)
      _brew_prefix=$(brew --prefix 2>/dev/null || true)
      if [[ -n "$_brew_prefix" && "$_ssh_path" != "$_brew_prefix"* ]]; then
        record "required_ssh_brew_path" "warn" "ssh resolves to $_ssh_path (not brew openssh at $_brew_prefix/bin/ssh) — LibreSSL build lacks FIDO2/sk-ssh-ed25519 support; ensure \$(brew --prefix)/bin precedes /usr/bin in PATH (add eval \"\$(brew shellenv)\" to ~/.zprofile)"
      fi
    fi
    [[ "$tool" == "curl" ]] && _curl_ok=true
  else
    case "$tool" in ssh) _pd=openssh-clients _pa=openssh-client _pb=openssh ;; *) _pd=$tool _pa=$tool _pb=$tool ;; esac
    if [[ "$tool" == ansible-playbook ]]; then
      if command -v make &>/dev/null; then
        record "required_${tool}" "fail" "not installed — run: make bootstrap"
      else
        record "required_${tool}" "fail" "not installed — install make first (dnf/brew), then: make bootstrap"
      fi
    elif [[ "$tool" == make ]]; then
      record "required_${tool}" "fail" "not installed — install first: sudo dnf install make (Fedora/RHEL) | sudo apt install make (Debian) | brew install make (macOS), then: make bootstrap"
    else
      record "required_${tool}" "fail" "not installed — run: sudo dnf install ${_pd} (Fedora/RHEL) | sudo apt install ${_pa} (Debian) | brew install ${_pb} (macOS)"
    fi
  fi
done

# --- Ansible collections ---
missing_cols=()
for col in community.general containers.podman ansible.posix; do
  [[ -d "${SCRIPT_DIR}/../collections/ansible_collections/${col//.//}" ]] || missing_cols+=("$col")
done
if [[ ${#missing_cols[@]} -eq 0 ]]; then
  record "ansible_collections" "pass" "all required collections installed"
else
  record "ansible_collections" "fail" "missing: ${missing_cols[*]} — run: make collections (fresh machine: make bootstrap)"
fi

# --- YubiKey presence ---
yk_found=false
yk_detect_possible=true
if command -v lsusb &>/dev/null && lsusb 2>/dev/null | grep -iE "yubico|1050:" >/dev/null; then
  yk_found=true; yk_detect_method="lsusb"
elif command -v ykman &>/dev/null && ykman info &>/dev/null; then
  yk_found=true; yk_detect_method="ykman"
else
  if ! command -v lsusb &>/dev/null && ! command -v ykman &>/dev/null; then
    yk_detect_possible=false
  fi
fi
if [[ "$yk_found" == true ]]; then
  yk_count=$(lsusb 2>/dev/null | grep -ciE "yubico|1050:" || true)
  if [[ ${yk_count:-0} -gt 1 ]]; then
    record "yubikey_present" "warn" "${yk_count} YubiKeys inserted: unplug all but one (the touch test and vault-pass.sh need exactly one)"
  else
    record "yubikey_present" "pass" "detected via ${yk_detect_method}"
  fi
  if command -v ykman &>/dev/null; then
    if [[ "$JSON" == false ]]; then
      if ! command -v timeout &>/dev/null; then
        record "yubikey_chalresp" "skip" "timeout not available (brew install coreutils on macOS)"
      else
        _yk_timeout=15
        echo "Touch your YubiKey for HMAC-SHA1 challenge-response test..." >&2
        if timeout "$_yk_timeout" ykman otp calculate 2 "$(printf '%s' 'preflight-test' | od -An -tx1 | tr -d ' \n')" &>/dev/null; then
          record "yubikey_chalresp" "pass" "Slot 2 HMAC-SHA1 responding"
        else
          record "yubikey_chalresp" "warn" "no HMAC-SHA1 response from slot 2 (touch timed out after ${_yk_timeout}s, slot 2 empty, or more than one YubiKey inserted); see the vault line"
        fi
      fi
    else
      record "yubikey_chalresp" "skip" "skipped in --json mode (interactive)"
    fi
  else
    record "yubikey_chalresp" "skip" "ykman not installed"
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
if [[ -x "$vscript" ]]; then
  if [[ "$JSON" == false ]]; then
    # The password must not reach an xtrace log (bash -x): trace off while it is held
    _xtrace=false
    [[ $- != *x* ]] || _xtrace=true
    set +x
    _vrc=0
    echo "Testing vault-pass.sh (touch your YubiKey again if it blinks)..." >&2
    output=$("$vscript" 2>/dev/null) || _vrc=$?
    len=${#output}
    if [[ $_vrc -ne 0 ]]; then
      record "vault" "fail" "vault-pass.sh exited with status ${_vrc} (YubiKey touch timed out, key not inserted, or more than one key inserted)"
    elif [[ $len -ge 8 ]]; then
      if [[ "$output" == *ci-dummy-vault-password* || "$output" == CHANGE_ME ]]; then
        record "vault" "warn" "vault-pass.sh is still a placeholder stub — run: make setup-yubikeys (or see SECURITY.md 'Setting Up vault-pass.sh')"
      else
        record "vault" "pass" "script returned valid password"
      fi
    elif [[ $len -gt 0 ]]; then
      record "vault" "fail" "script returned only ${len} chars — vault password too short"
    else
      record "vault" "fail" "script returned empty output"
    fi
    output=""
    if [[ "$_xtrace" == true ]]; then set -x; fi
  else
    record "vault" "skip" "skipped in --json mode (interactive)"
  fi
elif [[ -f "$vscript" ]]; then
  record "vault" "fail" "script exists but is not executable"
else
  record "vault" "fail" "no scripts/vault-pass.sh — run: make bootstrap, then make setup-yubikeys"
fi

# vault_encrypted is a file-only check — no YubiKey interaction needed — runs in --json mode unlike the vault script test above
# --- Vault encryption check ---
VAULT_FILE="$SCRIPT_DIR/../group_vars/all/vault.yml"
if [[ -f "$VAULT_FILE" ]]; then
  # shellcheck disable=SC2016  # Intentional: matching literal $ANSIBLE_VAULT header
  if head -1 "$VAULT_FILE" | grep -q '^\$ANSIBLE_VAULT'; then
    record "vault_encrypted" "pass" "vault.yml is encrypted"
  else
    if python3 "$SCRIPT_DIR/verify-vault-stub.py" < "$VAULT_FILE"; then
      record "vault_encrypted" "warn" "vault.yml is plaintext stub — vault_ssh_auth_key will be empty; 'make container' skips dev container provisioning. Run make setup-yubikeys if not done yet, then make ssh-keys and make vault-init (CLAUDE.md step 3)"
    else
      record "vault_encrypted" "warn" "vault.yml is plaintext and not the empty stub (values set or template changed) — encrypt it: make vault-init (after make setup-yubikeys and make ssh-keys)"
    fi
  fi
else
  record "vault_encrypted" "fail" "group_vars/all/vault.yml missing — restore it with: git checkout group_vars/all/vault.yml"
fi

# --- config.yml ---
if [[ ! -f "$CONFIG_FILE" ]]; then
  record "config_yml" "fail" "config.yml missing — cp default.config.yml config.yml and replace every CHANGE_ME (Play 0 refuses to run until identity values are set)"
elif ! grep -q '^desktop_environment:' "$CONFIG_FILE"; then
  record "config_yml" "fail" "config.yml exists but does not set desktop_environment; set 'desktop_environment: sway' (or i3/gnome)"
elif ! grep -qE '^desktop_environment:[[:space:]]*["'"'"']?('"$([[ "$OS_FAMILY" == darwin ]] && echo aerospace || echo 'sway|i3|gnome')"')["'"'"']?([[:space:]]|$)' "$CONFIG_FILE"; then
  record "config_yml" "fail" "desktop_environment must be $([[ "$OS_FAMILY" == darwin ]] && echo aerospace || echo 'sway, i3, or gnome') — got: $(grep '^desktop_environment:' "$CONFIG_FILE")"
else
  record "config_yml" "pass" "desktop_environment is set"
  # HiDPI warning: Sway on a HiDPI display without scale set produces microscopic fonts.
  # No assertion can know the display resolution, but if sway is chosen and scale is absent/1.0
  # we remind the user to check. This is a WARN not a FAIL — non-HiDPI users are unaffected.
  if grep -qE '^desktop_environment:[[:space:]]*["'"'"']?sway["'"'"']?' "$CONFIG_FILE"; then
    if ! grep -qE '^desktop_sway_hidpi_scale:[[:space:]]*["'"'"']?[^[:space:]"'"'"'#1]' "$CONFIG_FILE" && \
       ! grep -qE '^desktop_sway_hidpi_scale:[[:space:]]*["'"'"']?1\.[1-9]' "$CONFIG_FILE"; then
      record "config_sway_hidpi" "warn" "desktop_sway_hidpi_scale not set above 1.0 — if this is a HiDPI display (e.g. ThinkPad P16v 2560x1600) add 'desktop_sway_hidpi_scale: \"1.5\"' to config.yml to avoid microscopic fonts"
    else
      record "config_sway_hidpi" "pass" "desktop_sway_hidpi_scale set to $(grep '^desktop_sway_hidpi_scale:' "$CONFIG_FILE" | cut -d: -f2- | sed 's/^[[:space:]]*//')"
    fi
  fi
fi

# --- Identity vars CHANGE_ME check ---
# True when a scalar is empty in YAML terms: nothing, "", '', ~ or null (optional !unsafe tag and trailing comment).
_identity_empty() {
  local v="$1" dq='^"([^"]*)"' sq="^'([^']*)'"
  v=$(sed -E 's/^[[:space:]]*(!unsafe([[:space:]]+|$))?//' <<<"$v")
  if [[ "$v" =~ $dq || "$v" =~ $sq ]]; then
    v="${BASH_REMATCH[1]}"
  else
    v=$(sed -E 's/(^|[[:space:]])#.*$//; s/[[:space:]]+$//' <<<"$v")
    [[ "$v" =~ ^(~|null|Null|NULL)$ ]] && v=""
  fi
  [[ -z "$v" ]]
}
if [[ -f "$CONFIG_FILE" ]]; then
  for _ivar in dotfiles_user_name dotfiles_github_user dotfiles_user_email_work dotfiles_user_email_personal system_timezone; do
    if [[ "$_ivar" == "dotfiles_user_email_work" && "$PROFILE" == "personal" ]]; then continue; fi
    if ! grep -q "^${_ivar}:" "$CONFIG_FILE"; then
      if [[ "$_ivar" == "system_timezone" ]]; then
        record "identity_${_ivar}" "fail" "${_ivar} not set in config.yml — timedatectl set-timezone CHANGE_ME will fail; run timedatectl list-timezones to find yours"
      else
        record "identity_${_ivar}" "fail" "${_ivar} not set in config.yml — provisioning uses 'CHANGE_ME' placeholder, producing wrong gitconfig/zshrc"
      fi
    elif grep -qE "^${_ivar}:[[:space:]]*(!unsafe[[:space:]]+)?['\"]?CHANGE_ME" "$CONFIG_FILE"; then
      record "identity_${_ivar}" "fail" "${_ivar} is still 'CHANGE_ME' in config.yml — set a real value before running make all"
    elif _identity_empty "$(grep -m1 "^${_ivar}:" "$CONFIG_FILE" | cut -d: -f2-)"; then
      record "identity_${_ivar}" "fail" "${_ivar} is empty in config.yml — set a real value (Play 0 rejects empty identity values)"
    else
      record "identity_${_ivar}" "pass" "${_ivar}=$(grep "^${_ivar}:" "$CONFIG_FILE" | cut -d: -f2- | sed 's/^[[:space:]]*//')"
    fi
  done
fi

# --- vault_* key guard (mirrors pre_flight_checks.yml assert) ---
if [[ -f "$CONFIG_FILE" ]]; then
  if grep -qE '^["'"'"']?vault_[^:]*:' "$CONFIG_FILE"; then
    record "config_vault_keys" "fail" "config.yml defines vault_* key(s) — include_vars outranks group_vars (precedence 18 > 4-5), silently shadowing the encrypted vault value; remove vault_* keys from config.yml"
  else
    record "config_vault_keys" "pass" "no vault_* keys in config.yml"
  fi
fi

# --- keyscan fingerprint guard (mirrors roles/ssh/tasks/keyscan_host.yml; warns before the sudo prompt) ---
if [[ -f "$CONFIG_FILE" ]]; then
  _bad_fp=$(python3 -I -c '
import re, sys, yaml
class L(yaml.SafeLoader): pass
L.add_multi_constructor("!", lambda loader, tag, node: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else loader.construct_mapping(node, deep=True) if isinstance(node, yaml.MappingNode) else loader.construct_sequence(node, deep=True))
hosts = (yaml.load(open(sys.argv[1]), Loader=L) or {}).get("ssh_work_keyscan_hosts") or []
for h in hosts:
    if isinstance(h, dict) and "fingerprint" in h and not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", str(h["fingerprint"])):
        print(h.get("host", "?"))
' "$CONFIG_FILE" 2>/dev/null | paste -sd' ' -) || _fp_unchecked=true
  if [[ "${_fp_unchecked:-false}" == true ]]; then
    record "config_keyscan_fingerprints" "warn" "could not parse config.yml (python3 with PyYAML required) — fingerprints unchecked"
  elif [[ -n "$_bad_fp" ]]; then
    record "config_keyscan_fingerprints" "warn" "fingerprint not pinned for: $_bad_fp — these hosts and their SSH repos are skipped until the full SHA256 (ssh-keygen -lf) is set"
  else
    record "config_keyscan_fingerprints" "pass" "keyscan fingerprints well-formed or unset"
  fi
  unset _bad_fp _fp_unchecked
fi

# --- Network connectivity ---
net_urls=("github=https://github.com" "galaxy=https://galaxy.ansible.com")
# Registry API root: 401 means reachable (auth required); the bare host returns 404
[[ "$PROFILE" == "work" ]] && net_urls+=("registry=https://registry.redhat.io/v2/")
for netlabel_url in "${net_urls[@]}"; do
  nlabel="${netlabel_url%%=*}" nurl="${netlabel_url#*=}"
  if [[ "$_curl_ok" == true ]]; then
    _http_code=$(curl -sSL --max-time 10 -o /dev/null -w '%{http_code}' "$nurl" 2>/dev/null) || _http_code=000
    if [[ "$_http_code" =~ ^[23] || ( "$nlabel" == registry && "$_http_code" == 401 ) ]]; then
      record "net_${nlabel}" "pass" "$nurl reachable (HTTP $_http_code)"
    # Only GitHub is required (binary downloads); collections are vendored and the registry is
    # used for image pulls (which need a login), so those are warnings
    elif [[ "$nlabel" == github ]]; then record "net_${nlabel}" "fail" "$nurl unreachable (HTTP ${_http_code:-000})"
    else record "net_${nlabel}" "warn" "$nurl unreachable (HTTP ${_http_code:-000})"; fi
  else
    record "net_${nlabel}" "skip" "curl not installed"
  fi
done

# --- registry.redhat.io authentication ---
if [[ "$PROFILE" == "work" ]]; then
  if command -v podman &>/dev/null; then
    if podman login --get-login registry.redhat.io &>/dev/null; then
      record "registry_redhat_auth" "pass" "authenticated with registry.redhat.io"
    else
      record "registry_redhat_auth" "warn" "not authenticated with registry.redhat.io — run 'podman login registry.redhat.io' before pulling from registry.redhat.io"
    fi
  else
    record "registry_redhat_auth" "warn" "podman not installed — registry.redhat.io auth not verifiable; install podman or run 'make packages' first"
  fi
fi

# --- fapolicyd detection (Linux only) ---
FAPOLICYD_BLOCKING=false
if [[ "$OS_FAMILY" == "rhel" || "$OS_FAMILY" == "fedora" ]]; then
  if systemctl is-active fapolicyd &>/dev/null; then
    if grep -qiE '^[[:space:]]*permissive[[:space:]]*=[[:space:]]*1([[:space:]]|#|$)' /etc/fapolicyd/fapolicyd.conf 2>/dev/null; then
      record "fapolicyd" "warn" "active but permissive mode (permissive=1 in config) — /tmp execution allowed"
    else
      FAPOLICYD_BLOCKING=true
      pl=$(awk -F= '/^\[/{section=$0} /^pipelining/ && section ~ /\[defaults\]/{print $2}' "$SCRIPT_DIR/../ansible.cfg" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*#.*$//' | tr '[:upper:]' '[:lower:]' || true)
      if [[ "$pl" =~ ^(true|yes|on|1)$ ]]; then
        record "fapolicyd" "warn" "active and enforcing — mitigated by pipelining=true in ansible.cfg"
      elif [[ -z "$pl" ]]; then
        record "fapolicyd" "warn" "active and enforcing — pipelining not set in ansible.cfg — fapolicyd will block /tmp execution"
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
  record "container_tier" "warn" "container — fapolicyd enforcing: run 'make container' for dev tools; host provisioning will be restricted"
elif [[ "$IS_CSB" == true || "$IS_RHEL" == true ]]; then
  record "container_tier" "warn" "hybrid — provision the host by chunk (make status), then 'make container' for dev tools"
else
  record "container_tier" "pass" "host-only — run 'make all' for full provisioning"
fi


# --- Sudo scope ---
if sudo_out=$(sudo -n -l 2>/dev/null); then
  if grep -qE '\(ALL[^)]*\)[[:space:]]+NOPASSWD:[[:space:]]*ALL([^A-Za-z0-9_]|$)' <<< "$sudo_out"; then
    record "sudo" "pass" "full sudo available"
  else
    record "sudo" "warn" "scoped sudo — some system tasks may fail"
  fi
else
  record "sudo" "warn" "no passwordless sudo — sudo chunks and targets prompt for your password (BECOME password)"
fi

# --- Desktop TrackPoint id (template injection guard) ---
# sway.config.j2 keeps [a-zA-Z0-9 ./:_-]; the i3 xorg template only strips newlines, so i3 ids must match its stricter set.
if [[ -f "$CONFIG_FILE" ]]; then
  _tp_found=false
  for _tpkey in desktop_sway_trackpoint_id desktop_i3_trackpoint_id; do
    grep -qE "^${_tpkey}:" "$CONFIG_FILE" 2>/dev/null || continue
    _tp_found=true
    trackpoint_value=$(grep -m1 "^${_tpkey}:" "$CONFIG_FILE" | cut -d: -f2- | sed "s/^[[:space:]]*//;s/[[:space:]][[:space:]]*#.*$//;s/[[:space:]]*$//" | tr -d "'" | tr -d '"')
    if [[ "$_tpkey" == desktop_sway_trackpoint_id ]]; then _tp_bad='[^a-zA-Z0-9_./: -]'; _tp_set='alphanumeric, spaces, colons, hyphens, underscores, dots, slashes only'
    else _tp_bad='[^a-zA-Z0-9_./ -]'; _tp_set='alphanumeric, spaces, hyphens, underscores, dots, slashes only'; fi
    if [[ -z "$trackpoint_value" ]]; then
      record "config_trackpoint_id" "pass" "${_tpkey} is empty (safe)"
    elif [[ "$trackpoint_value" =~ $_tp_bad ]]; then
      record "config_trackpoint_id" "fail" "${_tpkey} contains unsafe characters — ${_tp_set}"
    else
      record "config_trackpoint_id" "pass" "${_tpkey} is safe"
    fi
  done
  [[ "$_tp_found" == true ]] || record "config_trackpoint_id" "skip" "neither desktop_sway_trackpoint_id nor desktop_i3_trackpoint_id is set in config.yml"
  unset _tp_found _tpkey _tp_bad _tp_set
else
  record "config_trackpoint_id" "skip" "config.yml absent"
fi

# --- Disk space (need 5GB free in $HOME) ---
avail_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}') || avail_kb=""; if [[ -z "$avail_kb" ]]; then
  record "disk_space" "warn" "df failed on $HOME — cannot measure free space"
else
  avail_gb=$((avail_kb / 1048576))
  avail_mb=$((avail_kb / 1024))
  if [[ $avail_gb -ge 5 ]]; then record "disk_space" "pass" "${avail_gb}GiB free in \$HOME"
  else record "disk_space" "fail" "only ${avail_mb}MiB free — need at least 5GiB"; fi
fi

# --- RAM ---
if [[ "$OS_FAMILY" == "unknown" ]]; then record 'ram' 'skip' 'unknown OS — cannot read RAM'
else
  if [[ "$OS_FAMILY" == "darwin" ]]; then
    _raw_ram_bytes=$(sysctl -n hw.memsize 2>/dev/null) || _raw_ram_bytes=""
    [[ -n "$_raw_ram_bytes" ]] && ram_gb=$(( _raw_ram_bytes / 1073741824 ))
  else _raw_ram_gib=$(awk '/MemTotal/ {print int(($2 + 1048575) / 1048576)}' /proc/meminfo 2>/dev/null) || _raw_ram_gib=""; ram_gb=$_raw_ram_gib; fi
  ram_gb=${ram_gb:-0}  # guard: awk exits 0 with empty output when MemTotal absent
  if [[ -z "${_raw_ram_bytes:-}${_raw_ram_gib:-}" ]]; then record "ram" "warn" "could not read RAM — measurement tool returned empty output"
  elif [[ $ram_gb -ge 8 ]]; then record "ram" "pass" "${ram_gb}GiB"
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

# --- kernel ptrace_scope live-value check (Linux only) ---
# Warn when /proc/sys/kernel/yama/ptrace_scope is 0 so the user knows secrets
# are unprotected before sysctl.yml runs and applies system_ptrace_scope.
if [[ -f /proc/sys/kernel/yama/ptrace_scope ]]; then
  _live_ptrace=$(< /proc/sys/kernel/yama/ptrace_scope) || _live_ptrace=""
  if [[ "$_live_ptrace" == "0" ]]; then
    _cfg_ptrace_zero=false
    if [[ -f "$CONFIG_FILE" ]]; then
      grep -qE '^system_ptrace_scope:[[:space:]]*["'"'"']?0["'"'"']?([[:space:]]|$)' "$CONFIG_FILE" 2>/dev/null \
        && _cfg_ptrace_zero=true
    fi
    if [[ "$_cfg_ptrace_zero" == true ]]; then
      record "ptrace_scope" "warn" \
        "live kernel ptrace_scope=0 and system_ptrace_scope: 0 in config.yml — YAMA ptrace restrictions intentionally disabled; any same-UID process can ptrace ansible-playbook and read vault secrets from memory. Set system_ptrace_scope: 1 after dev work."
    else
      record "ptrace_scope" "warn" \
        "live kernel ptrace_scope=0 — YAMA ptrace unrestricted; any same-UID process can ptrace ansible-playbook and extract vault secrets from memory during this run. Only the sys_sysctl chunk (or make all) sets system_ptrace_scope=1; until it runs, secrets are unprotected during any run."
    fi
  else
    record "ptrace_scope" "pass" "live kernel ptrace_scope=${_live_ptrace}"
  fi
else
  record "ptrace_scope" "skip" "YAMA not available (macOS or kernel without CONFIG_SECURITY_YAMA)"
fi

# --- Secure Boot (Linux EFI only) ---
# Firmware setting, not managed by Ansible: warn early so it is enabled before sys_lockdown (smoke-test FAILs secure-boot until then).
if [[ "$OS_FAMILY" != "darwin" && -d /sys/firmware/efi ]]; then
  if ! command -v mokutil &>/dev/null; then
    record "secure_boot" "skip" "mokutil not installed (dnf/apt install mokutil) — cannot read Secure Boot state"
  elif _sb_state=$(mokutil --sb-state 2>&1) && [[ "$_sb_state" == *"SecureBoot enabled"* ]]; then
    record "secure_boot" "pass" "SecureBoot enabled"
  elif [[ "$_sb_state" == *"SecureBoot disabled"* ]]; then
    _sb_note=""
    # Fedora's shim is signed by Microsoft's third-party UEFI CA: with it missing from db, Secure Boot makes the machine unbootable.
    # Capture first: grep -q exits on the first match, so a pipe would SIGPIPE mokutil and fail under pipefail.
    _sb_db=$(mokutil --db 2>/dev/null || true)
    grep -qE 'CN=Microsoft (Corporation )?UEFI CA' <<<"$_sb_db" \
      || _sb_note=" — the firmware db has no Microsoft third-party UEFI CA: enable 'Allow Microsoft 3rd Party UEFI CA' FIRST or Fedora will not boot"
    record "secure_boot" "warn" "Secure Boot is disabled in firmware — enable it before sys_lockdown (lockdown=integrity is bypassable without it)${_sb_note}; steps: SECURITY.md § Secure Boot"
  else
    record "secure_boot" "skip" "could not read Secure Boot state: ${_sb_state//$'\n'/ }"
  fi
else
  record "secure_boot" "skip" "not an EFI boot (or macOS)"
fi

# --- Output ---
if [[ "$JSON" == true ]]; then
  printf '{"os_family":"%s","is_csb":%s,"profile":"%s","checks":[' "$OS_FAMILY" "$IS_CSB" "$PROFILE"
  first=true
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r name status detail <<< "$r"
    detail="${detail//\\/\\\\}"; detail="${detail//\"/\\\"}"; detail="${detail//$'\n'/\\n}"; detail="${detail//$'\t'/\\t}"; detail="${detail//$'\r'/\\r}"
    [[ "$first" == true ]] && first=false || printf ','
    printf '{"name":"%s","status":"%s","detail":"%s"}' "$name" "$status" "$detail"
  done
  printf '],"ready":%s}\n' "$( [[ $FAILURES -eq 0 ]] && echo true || echo false )"
else
  echo ""
  if [[ $FAILURES -eq 0 ]]; then
    printf "${GRN}Ready${NC}: next 'make status', then 'make plan CHUNK=<name>' and 'make chunk CHUNK=<name>' one chunk at a time (CLAUDE.md); 'make all' applies every enabled chunk\n"
  else
    printf "${RED}%d check(s) failed — resolve before provisioning${NC}\n" "$FAILURES"
  fi
fi
exit $(( FAILURES > 0 ? 1 : 0 ))
