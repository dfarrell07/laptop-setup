#!/usr/bin/env python3
"""Verify security hardening variables are present and have expected types in
group_vars/all/vars.yml, which is the single source of truth for these values.

group_vars/all/vars.yml is authoritative for full-playbook runs (precedence 5).
roles/system/defaults/main.yml duplicates them so the system role is standalone-invocable
in molecule (which generates a temp inventory that does not load project group_vars).

Also verifies that pipx version pins in roles/packages/defaults/main.yml satisfy
the specifier ranges in requirements-test.txt, keeping the two files in sync.
"""
import re
import sys
import pathlib
import yaml
from packaging.version import Version
from packaging.specifiers import SpecifierSet

REPO_ROOT = pathlib.Path(__file__).parent.parent
VARS_FILE = REPO_ROOT / "group_vars/all/vars.yml"
SYSTEM_DEFAULTS_FILE = REPO_ROOT / "roles/system/defaults/main.yml"
PACKAGES_DEFAULTS_FILE = REPO_ROOT / "roles/packages/defaults/main.yml"
REQUIREMENTS_FILE = REPO_ROOT / "requirements-test.txt"
MOLECULE_DIR = REPO_ROOT / "molecule"

# molecule files intentionally set distrobox_oc_version to a non-release value
# (e.g. "0.0.0-offline-test") to exercise rescue/degradation paths — skip them.
_OC_VERSION_SKIP = {
    MOLECULE_DIR / "shared" / "offline-vars.yml",
}

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


def check_oc_version_sync():
    """Verify distrobox_oc_version in molecule files matches packages_oc_version.

    molecule/shared/offline-vars.yml intentionally uses "0.0.0-offline-test" to
    trigger a 404 rescue path — it is excluded from this check.
    """
    packages_data = load_yaml(PACKAGES_DEFAULTS_FILE)
    expected = str(packages_data.get("packages_oc_version", ""))
    if not expected:
        return ["  MISSING: packages_oc_version not found in roles/packages/defaults/main.yml"]

    errors = []

    for yml_file in sorted(MOLECULE_DIR.rglob("*.yml")):
        if yml_file in _OC_VERSION_SKIP:
            continue
        content = yml_file.read_text()
        for match in re.finditer(r'distrobox_oc_version:\s*["\']([^"\']+)["\']', content):
            found = match.group(1)
            if found != expected:
                errors.append(
                    f"  MISMATCH: {yml_file.relative_to(REPO_ROOT)}: "
                    f"distrobox_oc_version={found!r} != packages_oc_version={expected!r}"
                )

    # Also check the stub shell script in container/prepare.yml: the echo output
    # must match so comments stay accurate when packages_oc_version is bumped.
    prepare_file = MOLECULE_DIR / "container" / "prepare.yml"
    if prepare_file.exists() and prepare_file not in _OC_VERSION_SKIP:
        content = prepare_file.read_text()
        for match in re.finditer(r'Client Version:\s*(\d+\.\d+\.\d+)', content):
            found = match.group(1)
            if found != expected:
                errors.append(
                    f"  MISMATCH: {prepare_file.relative_to(REPO_ROOT)}: "
                    f"stub 'Client Version: {found}' != packages_oc_version={expected!r}"
                )

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
            "vars.yml is the single source of truth for security hardening values "
            "(roles/system/defaults/main.yml mirrors them for standalone molecule use).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: {len(REQUIRED_KEYS)} security hardening keys present and correctly typed in vars.yml")

    defaults_data = load_yaml(SYSTEM_DEFAULTS_FILE)
    defaults_errors = []
    for key in REQUIRED_KEYS:
        if key not in defaults_data:
            defaults_errors.append(f"  MISSING: {key} not found in roles/system/defaults/main.yml")
    if defaults_errors:
        print(
            "ERROR: roles/system/defaults/main.yml missing security vars"
            " (required mirror for standalone molecule runs):",
            file=sys.stderr,
        )
        for line in defaults_errors:
            print(line, file=sys.stderr)
        print(
            "Add missing keys to roles/system/defaults/main.yml to match vars.yml.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"OK: {len(REQUIRED_KEYS)} security hardening keys present in"
        " roles/system/defaults/main.yml (molecule mirror)"
    )

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

    oc_errors = check_oc_version_sync()
    if oc_errors:
        print(
            "ERROR: distrobox_oc_version in molecule files does not match"
            " packages_oc_version in roles/packages/defaults/main.yml:",
            file=sys.stderr,
        )
        for line in oc_errors:
            print(line, file=sys.stderr)
        print(
            "Update distrobox_oc_version in the listed molecule files to match"
            " packages_oc_version, or bump packages_oc_version first.",
            file=sys.stderr,
        )
        sys.exit(1)

    packages_data = load_yaml(PACKAGES_DEFAULTS_FILE)
    print(
        f"OK: distrobox_oc_version in molecule files matches"
        f" packages_oc_version={packages_data.get('packages_oc_version')!r}"
    )


if __name__ == "__main__":
    main()
