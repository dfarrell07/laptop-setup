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
SSH_DEFAULTS_FILE = REPO_ROOT / "roles/ssh/defaults/main.yml"
PACKAGES_DEFAULTS_FILE = REPO_ROOT / "roles/packages/defaults/main.yml"
REQUIREMENTS_FILE = REPO_ROOT / "requirements-test.txt"
MOLECULE_DIR = REPO_ROOT / "molecule"
LINTING_CI_FILE = REPO_ROOT / ".github/workflows/linting.yml"
DEFAULT_CONFIG_FILE = REPO_ROOT / "default.config.yml"
DOTFILES_DEFAULTS_FILE = REPO_ROOT / "roles/dotfiles/defaults/main.yml"

# Cross-role mirror: vars.yml is authoritative; these role defaults must match.
# Checked so standalone molecule invocations use the same defaults as full playbook runs.
CROSS_ROLE_MIRROR_KEYS = {
    "profile": [
        REPO_ROOT / "roles/claude/defaults/main.yml",
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
        REPO_ROOT / "roles/git_repos/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
        REPO_ROOT / "roles/ssh/defaults/main.yml",
        REPO_ROOT / "roles/system/defaults/main.yml",
    ],
    "packages_install_binaries": [
        REPO_ROOT / "roles/claude/defaults/main.yml",
        REPO_ROOT / "roles/distrobox/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
    ],
    "install_virtualization": [
        REPO_ROOT / "roles/packages/defaults/main.yml",
        REPO_ROOT / "roles/system/defaults/main.yml",
    ],
    "install_vpn": [
        REPO_ROOT / "roles/redhat/defaults/main.yml",
        REPO_ROOT / "roles/system/defaults/main.yml",
    ],
    "is_linux": [
        REPO_ROOT / "roles/claude/defaults/main.yml",
        REPO_ROOT / "roles/containers/defaults/main.yml",
        REPO_ROOT / "roles/desktop/defaults/main.yml",
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
    ],
    "is_macos": [
        REPO_ROOT / "roles/claude/defaults/main.yml",
        REPO_ROOT / "roles/desktop/defaults/main.yml",
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
    ],
    "is_dnf": [
        REPO_ROOT / "roles/packages/defaults/main.yml",
        REPO_ROOT / "roles/system/defaults/main.yml",
        REPO_ROOT / "roles/desktop/defaults/main.yml",
    ],
    "is_apt": [
        REPO_ROOT / "roles/desktop/defaults/main.yml",
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
    ],
    "repo_gh_cli": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
    ],
    "repo_google_chrome": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_google_cloud_sdk": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_slack": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_mullvad": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_docker_ce": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_redhat": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_rpmfusion_free": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_rpmfusion_nonfree": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "repo_tailscale": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
    ],
    "install_docker": [
        REPO_ROOT / "roles/containers/defaults/main.yml",
    ],
    "ssh_signing_key_basename": [
        REPO_ROOT / "roles/ssh/defaults/main.yml",
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
    ],
    "ssh_ciphers": [
        REPO_ROOT / "roles/system/defaults/main.yml",
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
    ],
    "ssh_macs": [
        REPO_ROOT / "roles/system/defaults/main.yml",
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
    ],
    "ssh_kex_algorithms": [
        REPO_ROOT / "roles/system/defaults/main.yml",
    ],
    "is_rhel": [
        REPO_ROOT / "roles/redhat/defaults/main.yml",
    ],
    "fapolicyd_enforcing": [
        REPO_ROOT / "roles/distrobox/defaults/main.yml",
    ],
    "needs_container_tier": [
        REPO_ROOT / "roles/dotfiles/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
    ],
    "is_fedora": [
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "csb_detected": [
        REPO_ROOT / "roles/redhat/defaults/main.yml",
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "csb_rhel": [
        REPO_ROOT / "roles/claude/defaults/main.yml",
        REPO_ROOT / "roles/distrobox/defaults/main.yml",
        REPO_ROOT / "roles/packages/defaults/main.yml",
        REPO_ROOT / "roles/redhat/defaults/main.yml",
        REPO_ROOT / "roles/repos_dnf/defaults/main.yml",
    ],
    "network_proxied": [
        REPO_ROOT / "roles/packages/defaults/main.yml",
        REPO_ROOT / "roles/system/defaults/main.yml",
    ],
    "proxy_url": [
        REPO_ROOT / "roles/packages/defaults/main.yml",
        REPO_ROOT / "roles/system/defaults/main.yml",
    ],
}

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
    "system_pwquality_maxsequence": int,
    "system_pwhistory_remember": int,
    "system_inactive_days": int,
    "system_pass_max_days": int,
    "system_pass_min_days": int,
    "system_pass_warn_age": int,
    "system_auditd_max_log_file": int,
    "system_auditd_num_logs": int,
    "system_umask": str,
    "system_bolt_enabled": str,
    "system_is_container": str,
    "system_disable_avahi": bool,
    "system_mask_nfs_server": bool,
    "system_init_on_free": bool,
    "system_faillock_even_deny_root": bool,
    "system_disable_sctp": bool,
    "system_disable_usb_storage": bool,
    "system_disable_udf": bool,
    "system_install_usbguard": bool,
    "system_kernel_panic": int,
    "system_kernel_panic_on_oops": int,
    "system_kexec_load_disabled": int,
    "system_ptrace_scope": int,
    "system_sysrq": int,
    "system_bpf_jit_harden": int,
    "system_unprivileged_bpf_disabled": int,
    "system_io_uring_disabled": int,
    "system_enable_ip_forward": bool,
    "is_dnf5": bool,
    "is_dnf": bool,
    "is_apt": bool,
    "csb_rhel": bool,
    "csb_detected": bool,
    "is_fedora": bool,
    "install_vpn": bool,
    "install_virtualization": bool,
    "network_proxied": bool,
    "proxy_url": str,
    "ssh_pubkey_accepted_algorithms": list,
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


def _fail_on_errors(errors, header, advice):
    """Print errors to stderr and exit 1; no-op when errors is empty."""
    if not errors:
        return
    print(f"ERROR: {header}", file=sys.stderr)
    for line in errors:
        print(line, file=sys.stderr)
    print(advice, file=sys.stderr)
    sys.exit(1)


def check_pipx_version_sync(packages_data):
    """Verify pipx version pins in packages defaults satisfy requirements-test.txt ranges."""
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


def check_oc_version_sync(packages_data):
    """Verify distrobox_oc_version in molecule files matches packages_oc_version.

    molecule/shared/offline-vars.yml intentionally uses "0.0.0-offline-test" to
    trigger a 404 rescue path — it is excluded from this check.
    """
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


def check_distrobox_version_sync(packages_data):
    """Verify distrobox_golangci_lint_version and distrobox_subctl_version in
    molecule files match packages_golangci_lint_version and packages_subctl_version.

    Unlike distrobox_oc_version, both vars carry real version strings in all
    molecule files (including offline-vars.yml), so no skip set is needed.
    """
    distrobox_version_vars = {
        "distrobox_golangci_lint_version": "packages_golangci_lint_version",
        "distrobox_subctl_version": "packages_subctl_version",
    }

    errors = []
    for distrobox_var, packages_var in distrobox_version_vars.items():
        expected = str(packages_data.get(packages_var, ""))
        if not expected:
            errors.append(
                f"  MISSING: {packages_var} not found in roles/packages/defaults/main.yml"
            )
            continue

        for yml_file in sorted(MOLECULE_DIR.rglob("*.yml")):
            content = yml_file.read_text()
            pattern = rf'{re.escape(distrobox_var)}:\s*["\']([^"\']+)["\']'
            for match in re.finditer(pattern, content):
                found = match.group(1).lstrip("v")
                if found != expected:
                    errors.append(
                        f"  MISMATCH: {yml_file.relative_to(REPO_ROOT)}: "
                        f"{distrobox_var}={match.group(1)!r} != {packages_var}={expected!r}"
                    )

    return errors


def check_linting_ci_sync(packages_data):
    """Verify binary versions and SHA256s in linting.yml match packages defaults.

    Extracts hardcoded version strings from tarball filenames (e.g.
    gitleaks_8.30.1_linux_x64.tar.gz) and SHA256 env var values
    (GITLEAKS_SHA256, ACTIONLINT_SHA256, ZIZMOR_SHA256) via regex, then
    asserts they match packages_gitleaks_version / packages_gitleaks_sha256,
    packages_actionlint_version / packages_actionlint_sha256, and
    packages_zizmor_version / packages_zizmor_sha256 in
    roles/packages/defaults/main.yml.
    """
    content = LINTING_CI_FILE.read_text()
    checks = [
        {
            "tool": "gitleaks",
            "version_pattern": r"gitleaks_(\d+\.\d+\.\d+)_linux_x64\.tar\.gz",
            "sha256_pattern": r"GITLEAKS_SHA256:\s*(\S+)",
            "packages_version_var": "packages_gitleaks_version",
            "packages_sha256_var": "packages_gitleaks_sha256",
        },
        {
            "tool": "actionlint",
            "version_pattern": r"actionlint_(\d+\.\d+\.\d+)_linux_amd64\.tar\.gz",
            "sha256_pattern": r"ACTIONLINT_SHA256:\s*(\S+)",
            "packages_version_var": "packages_actionlint_version",
            "packages_sha256_var": "packages_actionlint_sha256",
        },
        {
            "tool": "zizmor",
            "version_pattern": r"zizmor/releases/download/v(\d+\.\d+\.\d+)/",
            "sha256_pattern": r"ZIZMOR_SHA256:\s*(\S+)",
            "packages_version_var": "packages_zizmor_version",
            "packages_sha256_var": "packages_zizmor_sha256",
        },
    ]

    ci_rel = LINTING_CI_FILE.relative_to(REPO_ROOT)
    errors = []
    for check in checks:
        tool = check["tool"]

        ver_match = re.search(check["version_pattern"], content)
        if not ver_match:
            errors.append(
                f"  MISSING: could not find {tool} version in {ci_rel}"
            )
            continue
        ci_version = ver_match.group(1)

        sha_match = re.search(check["sha256_pattern"], content)
        if not sha_match:
            errors.append(
                f"  MISSING: could not find {tool} SHA256 in {ci_rel}"
            )
            continue
        ci_sha256 = sha_match.group(1)

        pkg_version = str(packages_data.get(check["packages_version_var"], ""))
        pkg_sha256 = str(packages_data.get(check["packages_sha256_var"], ""))

        if not pkg_version:
            errors.append(
                f"  MISSING: {check['packages_version_var']} not found in"
                " roles/packages/defaults/main.yml"
            )
        elif ci_version != pkg_version:
            errors.append(
                f"  MISMATCH: {tool} version: {ci_rel}={ci_version!r}"
                f" != {check['packages_version_var']}={pkg_version!r}"
            )

        if not pkg_sha256:
            errors.append(
                f"  MISSING: {check['packages_sha256_var']} not found in"
                " roles/packages/defaults/main.yml"
            )
        elif ci_sha256 != pkg_sha256:
            errors.append(
                f"  MISMATCH: {tool} SHA256: {ci_rel}={ci_sha256!r}"
                f" != {check['packages_sha256_var']}={pkg_sha256!r}"
            )

    return errors


def check_cross_role_mirror_keys(vars_data):
    """Verify cross-role mirror vars match vars.yml in all listed role defaults files."""
    errors = []
    for var_name, role_files in CROSS_ROLE_MIRROR_KEYS.items():
        expected = vars_data.get(var_name)
        if expected is None:
            errors.append(f"  MISSING: {var_name} not found in vars.yml")
            continue
        for role_file in role_files:
            role_data = load_yaml(role_file)
            if var_name not in role_data:
                errors.append(
                    f"  MISSING: {var_name} not found in"
                    f" {role_file.relative_to(REPO_ROOT)}"
                )
            elif role_data[var_name] != expected:
                errors.append(
                    f"  MISMATCH: {var_name}:"
                    f" vars.yml={expected!r}"
                    f" != {role_file.relative_to(REPO_ROOT)}={role_data[var_name]!r}"
                )
    return errors


def main():
    vars_data = load_yaml(VARS_FILE)
    packages_data = load_yaml(PACKAGES_DEFAULTS_FILE)

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
    _fail_on_errors(
        errors,
        "group_vars/all/vars.yml missing or wrong-type security vars:",
        "vars.yml is the single source of truth for security hardening values "
        "(roles/system/defaults/main.yml mirrors them for standalone molecule use).",
    )
    print(f"OK: {len(REQUIRED_KEYS)} security hardening keys present and correctly typed in vars.yml")

    defaults_data = load_yaml(SYSTEM_DEFAULTS_FILE)
    defaults_errors = []
    for key in REQUIRED_KEYS:
        if key not in defaults_data:
            defaults_errors.append(f"  MISSING: {key} not found in roles/system/defaults/main.yml")
        elif defaults_data[key] != vars_data[key]:
            defaults_errors.append(
                f"  MISMATCH: {key}: vars.yml={vars_data[key]!r}"
                f" != roles/system/defaults/main.yml={defaults_data[key]!r}"
            )
    _fail_on_errors(
        defaults_errors,
        "roles/system/defaults/main.yml missing or mismatched security vars"
        " (required mirror for standalone molecule runs):",
        "Update roles/system/defaults/main.yml to match vars.yml.",
    )
    print(
        f"OK: {len(REQUIRED_KEYS)} security hardening keys present and matching in"
        " roles/system/defaults/main.yml (molecule mirror)"
    )

    ssh_defaults_data = load_yaml(SSH_DEFAULTS_FILE)
    ssh_defaults_errors = []
    _SSH_MIRROR_KEYS = ["ssh_pubkey_accepted_algorithms"]
    for _SSH_MIRROR_KEY in _SSH_MIRROR_KEYS:
        if _SSH_MIRROR_KEY not in ssh_defaults_data:
            ssh_defaults_errors.append(
                f"  MISSING: {_SSH_MIRROR_KEY} not found in roles/ssh/defaults/main.yml"
            )
        elif ssh_defaults_data[_SSH_MIRROR_KEY] != vars_data[_SSH_MIRROR_KEY]:
            ssh_defaults_errors.append(
                f"  MISMATCH: {_SSH_MIRROR_KEY}: vars.yml={vars_data[_SSH_MIRROR_KEY]!r}"
                f" != roles/ssh/defaults/main.yml={ssh_defaults_data[_SSH_MIRROR_KEY]!r}"
            )
    _fail_on_errors(
        ssh_defaults_errors,
        "roles/ssh/defaults/main.yml missing or mismatched ssh mirror keys"
        " (required mirror for standalone ssh role invocation):",
        "Update roles/ssh/defaults/main.yml to match vars.yml.",
    )
    print(
        "OK: ssh_pubkey_accepted_algorithms present and matching in"
        " roles/ssh/defaults/main.yml (standalone ssh role mirror)"
    )

    default_config_data = load_yaml(DEFAULT_CONFIG_FILE)
    dotfiles_defaults_data = load_yaml(DOTFILES_DEFAULTS_FILE)
    ssh_port_errors = []
    _SSH_PORT_KEY = "ssh_port"
    if _SSH_PORT_KEY not in default_config_data:
        ssh_port_errors.append(
            f"  MISSING: {_SSH_PORT_KEY} not found in default.config.yml"
        )
    elif _SSH_PORT_KEY not in defaults_data:
        ssh_port_errors.append(
            f"  MISSING: {_SSH_PORT_KEY} not found in roles/system/defaults/main.yml"
        )
    elif default_config_data[_SSH_PORT_KEY] != defaults_data[_SSH_PORT_KEY]:
        ssh_port_errors.append(
            f"  MISMATCH: {_SSH_PORT_KEY}: default.config.yml={default_config_data[_SSH_PORT_KEY]!r}"
            f" != roles/system/defaults/main.yml={defaults_data[_SSH_PORT_KEY]!r}"
        )
    elif _SSH_PORT_KEY not in dotfiles_defaults_data:
        ssh_port_errors.append(
            f"  MISSING: {_SSH_PORT_KEY} not found in roles/dotfiles/defaults/main.yml"
        )
    elif default_config_data[_SSH_PORT_KEY] != dotfiles_defaults_data[_SSH_PORT_KEY]:
        ssh_port_errors.append(
            f"  MISMATCH: {_SSH_PORT_KEY}: default.config.yml={default_config_data[_SSH_PORT_KEY]!r}"
            f" != roles/dotfiles/defaults/main.yml={dotfiles_defaults_data[_SSH_PORT_KEY]!r}"
        )
    _fail_on_errors(
        ssh_port_errors,
        "ssh_port mismatch between default.config.yml, roles/system/defaults/main.yml,"
        " and roles/dotfiles/defaults/main.yml:",
        "Update all three files to the same ssh_port value.",
    )
    print(
        f"OK: ssh_port={default_config_data.get(_SSH_PORT_KEY)!r} matches in"
        " default.config.yml, roles/system/defaults/main.yml, and roles/dotfiles/defaults/main.yml"
    )

    _fail_on_errors(
        check_cross_role_mirror_keys(vars_data),
        "cross-role mirror vars in role defaults do not match vars.yml:",
        "Update the listed role defaults files to match group_vars/all/vars.yml.",
    )
    total_cross = sum(len(files) for files in CROSS_ROLE_MIRROR_KEYS.values())
    print(
        f"OK: {len(CROSS_ROLE_MIRROR_KEYS)} cross-role vars verified across"
        f" {total_cross} role defaults files"
    )

    _fail_on_errors(
        check_pipx_version_sync(packages_data),
        "pipx version pins in roles/packages/defaults/main.yml"
        " do not satisfy requirements-test.txt ranges:",
        "Update packages_yamllint_version / packages_ansible_lint_version"
        " in roles/packages/defaults/main.yml to match the requirements-test.txt specifiers.",
    )
    print(f"OK: {len(PIPX_VERSION_VARS)} pipx version pins satisfy requirements-test.txt ranges")

    _fail_on_errors(
        check_oc_version_sync(packages_data),
        "distrobox_oc_version in molecule files does not match"
        " packages_oc_version in roles/packages/defaults/main.yml:",
        "Update distrobox_oc_version in the listed molecule files to match"
        " packages_oc_version, or bump packages_oc_version first.",
    )
    print(
        f"OK: distrobox_oc_version in molecule files matches"
        f" packages_oc_version={packages_data.get('packages_oc_version')!r}"
    )

    _fail_on_errors(
        check_distrobox_version_sync(packages_data),
        "distrobox tool versions in molecule files do not match"
        " packages defaults in roles/packages/defaults/main.yml:",
        "Update distrobox_golangci_lint_version / distrobox_subctl_version"
        " in the listed molecule files to match the packages defaults,"
        " or bump the packages defaults first.",
    )
    print(
        f"OK: distrobox_golangci_lint_version and distrobox_subctl_version in"
        f" molecule files match packages defaults"
        f" (golangci-lint={packages_data.get('packages_golangci_lint_version')!r},"
        f" subctl={packages_data.get('packages_subctl_version')!r})"
    )

    _fail_on_errors(
        check_linting_ci_sync(packages_data),
        "binary versions/SHA256s in .github/workflows/linting.yml"
        " do not match roles/packages/defaults/main.yml:",
        "Update packages_gitleaks_version/sha256, packages_actionlint_version/sha256,"
        " or packages_zizmor_version/sha256 in roles/packages/defaults/main.yml to match"
        " linting.yml, or bump linting.yml to match.",
    )
    print(
        f"OK: gitleaks, actionlint, zizmor versions and SHA256s match between"
        f" linting.yml and packages defaults"
        f" (gitleaks={packages_data.get('packages_gitleaks_version')!r},"
        f" actionlint={packages_data.get('packages_actionlint_version')!r},"
        f" zizmor={packages_data.get('packages_zizmor_version')!r})"
    )


if __name__ == "__main__":
    main()
