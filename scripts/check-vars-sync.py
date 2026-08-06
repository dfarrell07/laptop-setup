#!/usr/bin/env python3
"""Verify security hardening variables are present and have expected types in
group_vars/all/vars.yml, which is the single source of truth for these values.

group_vars/all/vars.yml is authoritative for full-playbook runs (precedence 5).
roles/system/defaults/main.yml duplicates them so the system role is standalone-invocable
in molecule (which generates a temp inventory that does not load project group_vars).

Also verifies that pipx version pins in roles/packages/defaults/main.yml satisfy
the specifier ranges in requirements-test.txt, keeping the two files in sync.
"""
import sys
import pathlib
import yaml
from packaging.version import Version
from packaging.specifiers import SpecifierSet

REPO_ROOT = pathlib.Path(__file__).parent.parent
VARS_FILE = REPO_ROOT / "group_vars/all/vars.yml"
PACKAGES_DEFAULTS_FILE = REPO_ROOT / "roles/packages/defaults/main.yml"
REQUIREMENTS_FILE = REPO_ROOT / "requirements-test.txt"

# Mapping from Ansible var name to pip package name as it appears in requirements-test.txt.
PIPX_VERSION_VARS = {
    "packages_yamllint_version": "yamllint",
    "packages_ansible_lint_version": "ansible-lint",
}

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
    "system_kernel_panic": int,
    "system_kernel_panic_on_oops": int,
    "system_kexec_load_disabled": int,
    "system_ptrace_scope": int,
}


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def parse_requirements(path):
    """Return a dict mapping pip package name to its SpecifierSet from requirements-test.txt."""
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Split on first specifier character to get the package name.
            for i, ch in enumerate(line):
                if ch in (">", "<", "=", "!"):
                    pkg_name = line[:i].strip()
                    specifier_str = line[i:].strip()
                    result[pkg_name] = SpecifierSet(specifier_str)
                    break
    return result


def check_pipx_version_sync():
    """Verify pipx version pins in packages defaults satisfy requirements-test.txt ranges."""
    packages_data = load_yaml(PACKAGES_DEFAULTS_FILE)
    req_specifiers = parse_requirements(REQUIREMENTS_FILE)

    errors = []
    for var_name, pip_pkg in PIPX_VERSION_VARS.items():
        pinned = packages_data.get(var_name)
        if pinned is None:
            errors.append(f"  MISSING: {var_name} not found in roles/packages/defaults/main.yml")
            continue
        pinned_str = str(pinned)
        specifier = req_specifiers.get(pip_pkg)
        if specifier is None:
            errors.append(
                f"  MISSING: {pip_pkg} not found in requirements-test.txt"
                f" (expected to match {var_name}={pinned_str!r})"
            )
            continue
        try:
            if Version(pinned_str) not in specifier:
                errors.append(
                    f"  MISMATCH: {var_name}={pinned_str!r} does not satisfy"
                    f" requirements-test.txt constraint {pip_pkg}{specifier}"
                )
        except Exception as exc:
            errors.append(f"  ERROR: could not parse version {pinned_str!r} for {var_name}: {exc}")

    return errors


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

    pipx_errors = check_pipx_version_sync()
    if pipx_errors:
        print(
            "ERROR: pipx version pins in roles/packages/defaults/main.yml"
            " do not satisfy requirements-test.txt ranges:",
            file=sys.stderr,
        )
        for line in pipx_errors:
            print(line, file=sys.stderr)
        print(
            "Update packages_yamllint_version / packages_ansible_lint_version"
            " in roles/packages/defaults/main.yml to match the requirements-test.txt specifiers.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: {len(PIPX_VERSION_VARS)} pipx version pins satisfy requirements-test.txt ranges")


if __name__ == "__main__":
    main()
