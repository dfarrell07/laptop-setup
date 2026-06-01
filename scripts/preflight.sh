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
    fedora) OS_FAMILY="fedora" ;; rhel|centos|rocky|alma) OS_FAMILY="rhel" ;;
  esac
elif [[ "$(uname -s)" == "Darwin" ]]; then OS_FAMILY="darwin"; fi
record "os_family" "pass" "$OS_FAMILY"
if [[ "$OS_FAMILY" == "rhel" ]]; then
  for d in /etc/pki/ca-trust/source/anchors /etc/pki/tls/certs; do
    for p in '*RH*' '*redhat*' '*Eng-CA*' '*IT-Root*'; do
      [[ -n "$(find "$d" -maxdepth 1 -name "$p" -print -quit 2>/dev/null)" ]] && IS_CSB=true && break 2
    done
  done
fi
[[ "$IS_CSB" == true ]] \
  && record "csb_detected" "warn" "RHEL CSB — expect fapolicyd/sudo constraints" \
  || record "csb_detected" "pass" "not CSB"
if [[ -z "$PROFILE" ]]; then
  [[ "$IS_CSB" == true || "$OS_FAMILY" != "darwin" ]] && PROFILE="work" || PROFILE="personal"
fi
record "profile" "pass" "$PROFILE"

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
    if ykchalresp -2 "preflight-test" &>/dev/null; then
      record "yubikey_chalresp" "pass" "Slot 2 HMAC-SHA1 responding"
    else
      record "yubikey_chalresp" "fail" "Slot 2 challenge-response failed — HMAC-SHA1 configured?"
    fi
  else
    record "yubikey_chalresp" "skip" "ykchalresp not installed (need ykpers)"
  fi
else
  record "yubikey_present" "fail" "no YubiKey detected — vault decryption will fail"
fi

# --- Vault password scripts ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
vscript="${SCRIPT_DIR}/vault-pass.sh"
if [[ -x "$vscript" ]]; then
  output=$("$vscript" 2>/dev/null) || true
  len=${#output}
  if [[ $len -ge 8 ]]; then
    record "vault" "pass" "script returned ${len}-char password"
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
  if head -1 "$VAULT_FILE" | grep -q '^\$ANSIBLE_VAULT'; then
    record "vault_encrypted" "pass" "vault.yml is encrypted"
  else
    record "vault_encrypted" "warn" "vault.yml is plaintext — encrypt before use with real secrets"
  fi
fi

# --- Network connectivity ---
for netlabel_url in github=https://github.com galaxy=https://galaxy.ansible.com registry=https://registry.access.redhat.com; do
  nlabel="${netlabel_url%%=*}" nurl="${netlabel_url#*=}"
  if curl -sfSL --max-time 10 -o /dev/null "$nurl" 2>/dev/null; then
    record "net_${nlabel}" "pass" "$nurl reachable"
  else record "net_${nlabel}" "fail" "$nurl unreachable"; fi
done

# --- fapolicyd detection (Linux only) ---
if [[ "$OS_FAMILY" != "darwin" ]]; then
  if systemctl is-active fapolicyd &>/dev/null; then
    tmpscript=$(mktemp /tmp/preflight-fap-XXXXXX.sh)
    printf '#!/bin/bash\n' > "$tmpscript" && chmod +x "$tmpscript"
    if "$tmpscript" &>/dev/null; then
      record "fapolicyd" "warn" "active but /tmp execution allowed"
    else
      record "fapolicyd" "fail" "active and blocking — use pipelining=True in ansible.cfg"
    fi
    rm -f "$tmpscript"
  else
    record "fapolicyd" "pass" "not active"
  fi
fi

# --- Sudo scope ---
if sudo -n -l &>/dev/null 2>&1; then
  sudo_out=$(sudo -n -l 2>/dev/null) || true
  if printf '%s' "$sudo_out" | grep -qE '\(ALL\) ALL|\(ALL : ALL\) ALL'; then
    record "sudo" "pass" "full sudo available"
  else
    record "sudo" "warn" "scoped sudo — some system tasks may fail"
  fi
else
  record "sudo" "warn" "no passwordless sudo — will need --ask-become-pass"
fi

# --- Disk space (need 5GB free in $HOME) ---
avail_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print $4}') || avail_kb=0
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
    detail="${detail//\\/\\\\}"; detail="${detail//\"/\\\"}"
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
