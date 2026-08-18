#!/usr/bin/env python3
"""
Verify critical preflight assertions are present and properly implemented.

This script performs YAML-aware validation to ensure that the identity vars
assertion task is correctly implemented with actual ansible.builtin.assert logic,
not just comment text matching (which would be trivially bypassable via grep).

Validates:
1. Task name 'Assert identity vars are not CHANGE_ME placeholder values' exists
2. Module type is exactly ansible.builtin.assert (not debug, set_fact, etc.)
3. 'that' clause contains assertion conditions (actual validation logic)
4. When condition is 'not _pf_is_molecule'
5. include_tasks for pre_flight_checks.yml is present in Play 2 pre_tasks
"""

import re
import sys
import yaml


def load_playbook(playbook_path):
    """Load and parse site.yml."""
    try:
        with open(playbook_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"::error::Failed to load {playbook_path}: {e}")
        return None


def verify_include_tasks(play2):
    """Verify include_tasks for pre_flight_checks.yml is present."""
    for task in play2.get('pre_tasks', []):
        if 'ansible.builtin.include_tasks' in task or 'include_tasks' in task:
            task_spec = task.get('ansible.builtin.include_tasks') or task.get('include_tasks')
            if isinstance(task_spec, dict):
                task_file = task_spec.get('file', '')
            else:
                task_file = str(task_spec)
            if 'pre_flight_checks.yml' in task_file:
                return True
    return False


def verify_identity_assertion(play2):
    """Verify identity vars assertion task is correctly implemented."""
    errors = []

    found_task = False
    for task in play2.get('pre_tasks', []):
        task_name = task.get('name', '')
        if 'Assert identity vars are not CHANGE_ME' not in task_name:
            continue

        found_task = True

        # Check 1: Module type must be exactly ansible.builtin.assert
        if 'ansible.builtin.assert' not in task:
            errors.append(
                f"::error::Task '{task_name}' module is not ansible.builtin.assert. "
                f"Found modules: {list(task.keys())}"
            )
            return found_task, errors

        assert_block = task['ansible.builtin.assert']

        # Check 2: Must have 'that' clause with conditions (actual logic)
        if not isinstance(assert_block, dict) or 'that' not in assert_block:
            errors.append(
                f"::error::Task '{task_name}' has no 'that' clause with assertions. "
                f"Found keys: {list(assert_block.keys()) if isinstance(assert_block, dict) else 'not a dict'}"
            )
            return found_task, errors

        that_clause = assert_block.get('that', [])
        if not that_clause:
            errors.append(
                f"::error::Task '{task_name}' 'that' clause is empty (no assertion conditions)"
            )
            return found_task, errors

        # Check 3: Assertions must contain proper value comparisons (not just existence checks)
        # Patterns ensure each variable appears with actual comparison operators (!=, ==, |, default)
        # NOT just 'is defined' or bare variable existence checks
        required_patterns = {
            'dotfiles_user_name': r"dotfiles_user_name\s*(\||!=|==|not in|is not|default\()",
            'dotfiles_github_user': r"dotfiles_github_user\s*(\||!=|==|not in|is not|default\()",
            'dotfiles_user_email_personal': r"dotfiles_user_email_personal\s*(\||!=|==|not in|is not|default\()",
        }
        found_vars = set()

        for condition in that_clause:
            condition_str = str(condition)

            # Reject string literals (quoted text with no logic operators)
            # Simple heuristic: if it's wrapped in quotes with no | or comparison operators, reject it
            if condition_str.strip().startswith(('"""', "'''", '"', "'")) and '|' not in condition_str and '!=' not in condition_str:
                errors.append(
                    f"::error::Task '{task_name}' assertion contains string literal instead of logic: {condition_str}"
                )
                return found_task, errors

            # Check for each required variable in actual value comparison context
            # Must have comparison operators like !=, ==, |, or default() filter - not just 'is defined'
            for var_name, pattern in required_patterns.items():
                if var_name not in found_vars:
                    if re.search(pattern, condition_str):
                        found_vars.add(var_name)

        missing = [v for v in required_patterns.keys() if v not in found_vars]
        if missing:
            errors.append(
                f"::error::Task '{task_name}' 'that' clause must include proper value comparisons for: {', '.join(missing)}"
            )
            return found_task, errors

        # Check 4: When condition must be present
        when_condition = task.get('when', '')
        if not when_condition:
            errors.append(
                f"::error::Task '{task_name}' has no 'when' condition"
            )
            return found_task, errors

        # Check when condition contains 'not _pf_is_molecule'
        when_str = str(when_condition).strip()
        if '_pf_is_molecule' not in when_str or 'not' not in when_str:
            errors.append(
                f"::error::Task '{task_name}' when condition is not 'not _pf_is_molecule'. "
                f"Found: '{when_str}'"
            )
            return found_task, errors

        break

    if not found_task:
        errors.append(
            "::error::Task 'Assert identity vars are not CHANGE_ME placeholder values' not found in Play 2 pre_tasks"
        )

    return found_task, errors


def main():
    """Run all verification checks."""
    playbook_path = 'site.yml'
    plays = load_playbook(playbook_path)

    if not plays or len(plays) < 2:
        print("::error::site.yml does not have at least 2 plays")
        return 1

    play2 = plays[1]

    # Verify include_tasks
    if not verify_include_tasks(play2):
        print("::error::Pre-flight checks include_tasks not found in Play 2 pre_tasks")
        return 1

    # Verify identity assertion
    found, errors = verify_identity_assertion(play2)
    for error in errors:
        print(error)

    if errors or not found:
        print("::error::Critical identity vars assertion verification failed")
        return 1

    print("SUCCESS: All critical assertions verified with proper YAML logic validation")
    return 0


if __name__ == '__main__':
    sys.exit(main())
