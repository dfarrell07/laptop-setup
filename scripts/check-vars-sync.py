#!/usr/bin/env python3
"""Check that security hardening variables are in sync between
group_vars/all/vars.yml and roles/system/defaults/main.yml.

Both files must define these variables because molecule verify plays run as
standalone plays where role defaults (precedence 2) are unavailable; group_vars
(precedence 5) is authoritative. If they diverge, the role default becomes dead
code that misleads readers.
"""
import sys
import pathlib
import yaml

REPO_ROOT = pathlib.Path(__file__).parent.parent
VARS_FILE = REPO_ROOT / "group_vars/all/vars.yml"
DEFAULTS_FILE = REPO_ROOT / "roles/system/defaults/main.yml"

SYNCED_KEYS = [
    "system_tmout",
    "system_faillock_deny",
    "system_faillock_unlock_time",
    "system_pwquality_minlen",
    "system_pwquality_difok",
    "system_pwquality_ucredit",
    "system_pwquality_lcredit",
    "system_pwquality_ocredit",
    "system_pwquality_dcredit",
    "system_pwquality_maxrepeat",
    "system_pwhistory_remember",
    "system_inactive_days",
    "system_pass_max_days",
    "system_pass_min_days",
    "system_pass_warn_age",
    "system_auditd_max_log_file",
    "system_auditd_num_logs",
    "system_umask",
    "system_disable_avahi",
    "system_mask_nfs_server",
    "system_init_on_free",
    "system_faillock_even_deny_root",
]


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    vars_data = load_yaml(VARS_FILE)
    defaults_data = load_yaml(DEFAULTS_FILE)

    drift = []
    for key in SYNCED_KEYS:
        vars_val = vars_data.get(key)
        defaults_val = defaults_data.get(key)
        if vars_val != defaults_val:
            drift.append(
                f"  {key}: group_vars/all/vars.yml={vars_val!r}"
                f"  roles/system/defaults/main.yml={defaults_val!r}"
            )

    if drift:
        print("ERROR: vars.yml / roles/system/defaults/main.yml drift detected:")
        for line in drift:
            print(line)
        print(
            "Update BOTH files to the same value. "
            "group_vars (precedence 5) is authoritative; role defaults (precedence 2) "
            "exist for standalone molecule verify plays."
        )
        sys.exit(1)

    print(f"OK: {len(SYNCED_KEYS)} synced keys match between vars.yml and defaults/main.yml")


if __name__ == "__main__":
    main()
