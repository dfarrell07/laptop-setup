#!/bin/bash
# Check environment/argument guards without provisioning the host.
set -euo pipefail
cd "$(dirname "$0")"
repo_root="$PWD"
probe_dir=$(mktemp -d)
trap 'rm -rf "$probe_dir"' EXIT
cat > "$probe_dir/probe.mk" <<'MAKE'
.PHONY: _test_exported_collection_path
_test_exported_collection_path:
	@printf '%s\n' "$${ANSIBLE_COLLECTIONS_PATH}"
MAKE
actual=$(ANSIBLE_COLLECTIONS_PATH=/evil/collections make -f Makefile -f "$probe_dir/probe.mk" \
  _test_exported_collection_path | sed -n '\|/collections:|p')
expected="${repo_root}/collections:~/.ansible/collections:/usr/share/ansible/collections"
[[ "$actual" == "$expected" ]] || { echo 'FAIL: Makefile did not pin collection paths' >&2; exit 1; }

for flag in '--start-at-task=unsafe' '--skip-tags=always'; do
  if output=$(scripts/verify-ansible-args.sh ansible-playbook site.yml "$flag" 2>&1); then
    echo "FAIL: wrapper accepted $flag" >&2
    exit 1
  fi
  [[ "$output" == *rejected* ]] || { echo "FAIL: unexpected rejection for $flag" >&2; exit 1; }
done

# Bypass spellings: abbreviated long options and attached/abbreviated extra-vars forms.
bypass_cases=(
  '--start-at=foo' '--start-at-tas=foo' '--skip-tag=always' '--skip-tags=always'
  '--extra=csb_rhel=true' '--extra-v=_pf_is_molecule=true' '-ecsb_rhel=true'
  '--extra-vars=a=1 csb_rhel=true' '-e_pf_vault_asserted=true' '-e@/tmp/evil.yml'
  '--extra-vars={"common_project_root":"/tmp"}' '-esystem_sysctl_hardening={}'
  '--skip-tags= always' '--skip-tags=packages, always ,dotfiles'
  '-erollout_enable=all' '-e_rollout_disabled=[]' '--extra-vars=rollout_catalog={}'
  '-vecsb_rhel=true' '-vvecommon_project_root=/x' '-Ke_pf_vault_asserted=true' '-verollout_enable=all'
)
for flag in "${bypass_cases[@]}"; do
  if scripts/verify-ansible-args.sh true "$flag" >/dev/null 2>&1; then
    echo "FAIL: wrapper accepted bypass $flag" >&2
    exit 1
  fi
done
# Separate-value forms
if scripts/verify-ansible-args.sh true --extra 'x=1 csb_rhel=true' >/dev/null 2>&1; then
  echo 'FAIL: wrapper accepted --extra <csb_rhel>' >&2
  exit 1
fi
for skip in ' always' 'packages, always' 'always ,dotfiles' $'\xc2\xa0always' $'\xe3\x80\x80always' $'\talways'; do
  if scripts/verify-ansible-args.sh true --skip-tags "$skip" >/dev/null 2>&1; then
    echo "FAIL: wrapper accepted --skip-tags '$skip' (whitespace around always)" >&2
    exit 1
  fi
done
# Clustered short options with a separate value (-ve value) must be scanned as well.
for cluster in -ve -vve -Ke; do
  if scripts/verify-ansible-args.sh true "$cluster" 'csb_rhel=true' >/dev/null 2>&1; then
    echo "FAIL: wrapper accepted $cluster csb_rhel=true" >&2
    exit 1
  fi
done
scripts/verify-ansible-args.sh true -ve foo=bar || { echo 'FAIL: wrapper rejected a benign -ve' >&2; exit 1; }
scripts/verify-ansible-args.sh true -e foo=bar || { echo 'FAIL: wrapper rejected benign -e' >&2; exit 1; }

# Chunk selection: for site.yml runs the wrapper appends --skip-tags from scripts/rollout.py (nothing
# when every chunk is enabled); other playbooks and commands are left alone.
fake_ansible="$probe_dir/ansible-playbook"
printf '#!/bin/sh\nprintf "%%s\\n" "$@"\n' > "$fake_ansible"
chmod +x "$fake_ansible"
rollout_py=python3
[[ -x .venv/bin/python3 ]] && rollout_py=.venv/bin/python3
expected_skip=$("$rollout_py" scripts/rollout.py skip-tags)
site_out=$(scripts/verify-ansible-args.sh "$fake_ansible" site.yml --syntax-check)
if [[ -n "$expected_skip" ]]; then
  [[ "$site_out" == *$'--skip-tags\n'"$expected_skip" ]] \
    || { echo 'FAIL: wrapper did not append --skip-tags for site.yml' >&2; exit 1; }
else
  [[ "$site_out" != *--skip-tags* ]] || { echo 'FAIL: wrapper added --skip-tags with every chunk enabled' >&2; exit 1; }
fi
other_out=$(scripts/verify-ansible-args.sh "$fake_ansible" other.yml)
[[ "$other_out" != *--skip-tags* ]] || { echo 'FAIL: wrapper added --skip-tags to a non-site.yml run' >&2; exit 1; }

# All inherited ANSIBLE_* (not in the pinned set) and shell-trace vars must be cleared.
trace_probe='! env | grep -E "^(ANSIBLE_(VAULT_IDENTITY_LIST|DEBUG|TEST_PLUGINS|PRIVATE_KEY_FILE)|BASH_XTRACEFD|PS4|SHELLOPTS|BASHOPTS)="'
ANSIBLE_VAULT_IDENTITY_LIST=x@/tmp/evil ANSIBLE_DEBUG=1 ANSIBLE_TEST_PLUGINS=/tmp \
  ANSIBLE_PRIVATE_KEY_FILE=/tmp/k BASH_XTRACEFD=2 PS4='+evil ' \
  scripts/verify-ansible-args.sh bash -c "$trace_probe" \
  || { echo 'FAIL: wrapper left ANSIBLE_*/trace vars set' >&2; exit 1; }

# Editor, pager, npm registry and PAM variables could exfiltrate decrypted vault output or redirect downloads.
EDITOR=/tmp/evil VISUAL=/tmp/evil GIT_EDITOR=/tmp/evil PAGER=/tmp/evil GIT_PAGER=/tmp/evil MANPAGER=/tmp/evil \
  SYSTEMD_PAGER=/tmp/evil NPM_CONFIG_REGISTRY=https://evil.invalid/ PAM_USER=evil \
  scripts/verify-ansible-args.sh bash -c '[ -z "${EDITOR-}${VISUAL-}${GIT_EDITOR-}${PAGER-}${GIT_PAGER-}${MANPAGER-}${SYSTEMD_PAGER-}${NPM_CONFIG_REGISTRY-}${PAM_USER-}" ]' \
  || { echo 'FAIL: wrapper left editor/pager/registry/PAM vars set' >&2; exit 1; }

probe_file="$probe_dir/probe.py"
cat > "$probe_file" <<'PY'
import os
import shutil
from pathlib import Path
root = Path.cwd()
if (root / '.venv/bin/python3').exists():
    assert Path(shutil.which('ansible-playbook')) == root / '.venv/bin/ansible-playbook'
assert '/evil' not in os.environ['PATH']
assert os.environ['ANSIBLE_BECOME_PLUGINS'] == '/dev/null'
assert os.environ['ANSIBLE_LOOKUP_PLUGINS'] == '/dev/null'
assert os.environ['ANSIBLE_COLLECTIONS_PATH'].split(':')[0] == str(root / 'collections')
assert 'PYTHONPATH' not in os.environ
PY
probe_python=python3
[[ ! -x .venv/bin/python3 ]] || probe_python=.venv/bin/python3
ANSIBLE_BECOME_PLUGINS=/evil/plugins ANSIBLE_LOOKUP_PLUGINS=/evil/plugins \
  ANSIBLE_COLLECTIONS_PATH=/evil/collections PYTHONPATH=/evil/python \
  scripts/verify-ansible-args.sh "$probe_python" "$probe_file"
echo 'Environment and argument guards passed.'
