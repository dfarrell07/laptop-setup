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
