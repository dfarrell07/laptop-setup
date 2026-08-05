#!/usr/bin/env python3
"""Verify security hardening variables are present and have expected types in
group_vars/all/vars.yml, which is the single source of truth for these values.

These vars were previously duplicated in roles/system/defaults/main.yml but that
copy was removed (cycle 1 refactor); vars.yml is now authoritative for both
full-playbook and standalone molecule verify invocations (group_vars, precedence 5).
"""
import sys
import pathlib
import yaml

REPO_ROOT = pathlib.Path(__file__).parent.parent
VARS_FILE = REPO_ROOT / "group_vars/all/vars.yml"

# These must be present in vars.yml with the expected Python type.
REQUIRED_KEYS = {
    "system_tmout": int,
    "system_faillock_deny": int,
    "system_faillock_unlock_time": int,
    "system_pwquality_minlen": int,
    "system_pwquality_difok": int,
    "system_pwquality_ucredit": int,
    "system_pwquality_lcredit": int,
    "system_pwquality_ocredit": int,
    "system_pwquality_dcredit": int,
    "system_pwquality_maxrepeat": int,
    "system_pwhistory_remember": int,
    "system_inactive_days": int,
    "system_pass_max_days": int,
    "system_pass_min_days": int,
    "system_pass_warn_age": int,
    "system_auditd_max_log_file": int,
    "system_auditd_num_logs": int,
    "system_umask": str,
    "system_disable_avahi": bool,
    "system_mask_nfs_server": bool,
    "system_init_on_free": bool,
    "system_faillock_even_deny_root": bool,
}


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    vars_data = load_yaml(VARS_FILE)

    errors = []
    for key, expected_type in REQUIRED_KEYS.items():
        val = vars_data.get(key)
        if val is None:
            errors.append(f"  MISSING: {key} not found in vars.yml")
        elif not isinstance(val, expected_type):
            errors.append(
                f"  WRONG TYPE: {key}={val!r} expected {expected_type.__name__},"
                f" got {type(val).__name__}"
            )

    if errors:
        print("ERROR: group_vars/all/vars.yml missing or wrong-type security vars:", file=sys.stderr)
        for line in errors:
            print(line, file=sys.stderr)
        print(
            "vars.yml is the single source of truth for these values "
            "(roles/system/defaults/main.yml no longer carries them).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: {len(REQUIRED_KEYS)} security hardening keys present and correctly typed in vars.yml")


if __name__ == "__main__":
    main()
