#!/usr/bin/env python3
"""Parse raw config YAML, including !unsafe tags, without evaluating templates.

With --policy, print the config.yml keys and characters that would change how the play runs
(key paths and code points only, never values) and exit 3 when there are any.
"""
import datetime
import json
from pathlib import Path
import re
import sys

from ansible.parsing.dataloader import DataLoader
import yaml

ROOT = Path(__file__).resolve().parent.parent

# Magic variables a config.yml key could shadow. include_vars overrides some of them (omit turns
# every `default(omit)` into a real argument, e.g. a file mode; inventory_hostname, group_names,
# inventory_dir); the rest are ignored today but are never configuration.
MAGIC_KEYS = frozenset({
    'omit', 'hostvars', 'groups', 'group_names', 'inventory_hostname', 'inventory_hostname_short',
    'inventory_dir', 'inventory_file', 'playbook_dir', 'play_hosts', 'role_name', 'role_names',
    'role_path', 'environment',
    # vars is reserved, item the default loop variable, cacheable a set_fact option
    'vars', 'item', 'cacheable',
    # Jinja and Ansible template globals: a key named lookup makes every later lookup() call fail
    'cycler', 'dict', 'joiner', 'lipsum', 'lookup', 'namespace', 'now', 'q', 'query', 'range', 'undef', 'zip',
})
# Facts Play 0 sets before pre-flight loads config.yml (pre-flight.yml). config.yml values are set
# with set_fact, so they would overwrite these; include_vars used to lose to them.
FACTS_SET_BEFORE_CONFIG = frozenset({'real_user_home', 'real_user_id', 'is_linux', 'is_macos'})
# Facts common/tasks/csb_detect.yml derives on localhost after the load, which Play 2's add_host hands
# to the dev container (proxy_url): on the container host a config.yml value would win over them.
DERIVED_FACTS = frozenset({
    'csb_detected', 'fapolicyd_enforcing', 'fapolicyd_installed', 'internal_ca_present', 'is_apt', 'is_dnf',
    'is_dnf5', 'is_fedora', 'is_rhel', 'needs_container_tier', 'network_proxied', 'proxy_url',
})
# The wrapper protects rollout_enable from -e only; config.yml is where it is set.
CONFIG_SETTABLE_PROTECTED = frozenset({'rollout_enable'})
# Executables and install locations the roles run from a fixed place (SECURITY.md, "Executable paths"):
# config.yml cannot point them elsewhere. transcrypt receives the notes vault password, codex the Codex API key.
FIXED_EXECUTABLE_KEYS = frozenset({
    'notes_transcrypt_bin_path', 'notes_transcrypt_linux_bin_path', 'containers_podman_bin',
    'containers_macos_machine_ensure_script', 'claude_bin_path', 'claude_queue_poller_path', 'codex_npm_prefix',
    'antigravity_bin_dir',
})
# Executable locations config.yml may choose, from an allowlist: the Homebrew prefix (PATH of launch agents,
# the git credential helper's gh) and the polkit agent sway/i3 start (it receives the user's password).
HOMEBREW_PREFIXES = frozenset({'/opt/homebrew', '/usr/local'})
# A system binary: /usr/bin/<name>, or any depth under /usr/libexec, /usr/lib or /usr/lib64 (Plasma 6's
# /usr/libexec/kf6/polkit-kde-authentication-agent-1). No spaces (sway's exec takes a command line), no . or ..
SYSTEM_BINARY = re.compile(r'/usr/(bin|libexec(/[A-Za-z0-9_.+-]+)*|lib(64)?(/[A-Za-z0-9_.+-]+)*)/[A-Za-z0-9_.+-]+')


def _system_binary(value):
    return bool(SYSTEM_BINARY.fullmatch(value)) and not set(value.split('/')) & {'.', '..'}


LOCALE = re.compile(r'[A-Za-z]{2,3}(_[A-Z]{2})?(\.[A-Za-z0-9-]+)?(@[A-Za-z0-9]+)?|C|C\.UTF-8|C\.utf8|POSIX')
PLATFORM_PROFILE_CHOICES = ('low-power', 'cool', 'quiet', 'balanced', 'balanced-performance', 'performance', 'custom')
ENERGY_PERF_POLICIES = ('default', 'performance', 'balance_performance', 'balance_power', 'power')
# acli logs in to acli_site with the vault's Atlassian token: an Atlassian Cloud site, or '' to turn it off.
ATLASSIAN_SITE = re.compile(r'[a-z0-9][a-z0-9-]*\.atlassian\.net')
ALLOWED_VALUES = {
    'acli_site': lambda value: value == '' or bool(ATLASSIAN_SITE.fullmatch(value)),
    # rendered unquoted into the macOS shell start files (zshenv, bashrc): a Homebrew formula name
    'dotfiles_macos_python': lambda value: bool(re.fullmatch(r'[a-z0-9@.+-]+', value)),
    # /etc/locale.conf (evaluated by /etc/profile.d/lang.sh in root login shells) and /etc/vconsole.conf
    'system_locale': lambda value: bool(LOCALE.fullmatch(value)),
    'system_keymap': lambda value: bool(re.fullmatch(r'[a-zA-Z0-9_-]+', value)),
    # /etc/tlp.d, read by TLP as root (roles/system/tasks/power_management.yml has the same lists)
    'system_tlp_platform_profile_ac': lambda value: value in PLATFORM_PROFILE_CHOICES,
    'system_tlp_platform_profile_bat': lambda value: value in PLATFORM_PROFILE_CHOICES,
    'system_tlp_cpu_energy_policy_ac': lambda value: value in ENERGY_PERF_POLICIES,
    'system_tlp_cpu_energy_policy_bat': lambda value: value in ENERGY_PERF_POLICIES,
}
ALLOWED_EXECUTABLE_VALUES = {
    'dotfiles_homebrew_prefix': lambda value: value in HOMEBREW_PREFIXES,
    'desktop_sway_polkit_agent': _system_binary,
    'desktop_i3_polkit_agent': _system_binary,
}
# Sysctls whose value the kernel runs as root or treats as a path (usermode helpers, the core dump pipe, binfmt
# handlers) and the helpers' capability masks. A leading '-' only tells systemd-sysctl to ignore errors.
# roles/system/tasks/main.yml refuses the same pattern at the point of use; a test keeps the two equal.
SYSCTL_EXEC_KEYS = re.compile(
    r'-?(kernel\.(modprobe|poweroff_cmd|reboot_cmd|core_pattern|hotplug|usermodehelper(\..*)?)|fs\.binfmt_misc(\..*)?)')
SYSCTL_DICT_KEYS = frozenset({'system_sysctl_extra', 'system_sysctl_extra_additional'})
# A well-formed sysctl key: dot-separated lowercase segments, none empty (systemd-sysctl and procps both reduce
# kernel..modprobe or .kernel.modprobe to kernel.modprobe). The same pattern as the role's _sysctl_key_re.
SYSCTL_KEY = re.compile(r'-?[a-z0-9_-]+(\.[a-z0-9_-]+)*')


def sysctl_name(key):
    """The sysctl a key writes: no leading '-', '/' as '.', repeated dots collapsed, no leading or trailing dot."""
    return re.sub(r'\.+', '.', key.lstrip('-').replace('/', '.')).strip('.')
# C0 controls (tab and newline included: no option takes them), DEL, C1 controls and the Unicode
# line and paragraph separators. A NUL reaches files such as /etc/sysctl.d, where systemd treats it
# as a line break.
CONTROL_CHARS = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029]')
JINJA_EXPRESSION = re.compile(r'\{\{.*?\}\}', re.DOTALL)
VAULT_NAME = re.compile(r'\bvault_[A-Za-z0-9_]+')


def json_default(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError("config.yml contains a value that cannot be represented as JSON")


def load(content):
    # DataLoader.load parses YAML; unlike include_vars it does not grant trust
    # to template expressions. JSON serialization never invokes the templar.
    data = DataLoader().load(content, show_content=False)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError("config.yml must contain a mapping")
    return data


def parse_config(content):
    return json.dumps(load(content), default=json_default)


class PolicySourceError(Exception):
    """A source of the policy lists cannot be read; the message names a file, never its content."""


class _VarsLoader(yaml.SafeLoader):
    """SafeLoader that accepts the tags Ansible allows in vars files: !unsafe as plain data, !vault as opaque."""


def _construct_unsafe(loader, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_VarsLoader.add_constructor('!unsafe', _construct_unsafe)
_VarsLoader.add_constructor('!vault', lambda loader, node: '')


def protected_keys_pattern(root=ROOT):
    """The -e protected-key regex of scripts/verify-ansible-args.sh, the single list of protected keys."""
    text = (root / 'scripts/verify-ansible-args.sh').read_text()
    match = re.search(r"^_protected_keys='([^']+)'$", text, re.MULTILINE)
    if not match:
        raise PolicySourceError('_protected_keys not found in verify-ansible-args.sh')
    return re.compile(match.group(1))


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def vault_mappings(root=ROOT):
    """{variable: vault_* names its Jinja value reads}: a config.yml key of that name shadows the vault."""
    files = [p for p in sorted((root / 'group_vars/all').glob('*.yml')) if p.name != 'vault.yml']
    files += sorted(root.glob('roles/*/defaults/main.yml')) + sorted(root.glob('roles/*/vars/main.yml'))
    mapped = {}
    for path in files:
        try:
            data = yaml.load(path.read_text(), Loader=_VarsLoader)  # noqa: S506 - SafeLoader subclass
        except Exception as error:  # the message would quote the offending line
            raise PolicySourceError('%s in %s' % (type(error).__name__, path.name)) from None
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            names = {name for text in _strings(value) for expression in JINJA_EXPRESSION.findall(text)
                     for name in VAULT_NAME.findall(expression)}
            if names and not str(key).startswith('vault_'):
                mapped[str(key)] = names
    if not mapped:
        raise PolicySourceError('no vault_* mappings found under group_vars/all or roles/*/defaults')
    return mapped


def _control_chars(value, path):
    if isinstance(value, dict):
        for key, item in value.items():
            here = path + [str(key)]
            yield from _control_chars(str(key), here)
            yield from _control_chars(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _control_chars(item, path + [str(index)])
    elif isinstance(value, str):
        for char in sorted(set(CONTROL_CHARS.findall(value))):
            yield '%s: control character U+%04X' % (ascii('.'.join(path)), ord(char))


def policy_violations(data, root=ROOT):
    protected = protected_keys_pattern(root)
    vault_mapped = vault_mappings(root)
    found = []
    for key in map(str, data):
        if key.lower().startswith('ansible_'):
            found.append('%s: ansible_* connection and behaviour variables cannot be set in config.yml' % ascii(key))
        elif key.startswith('_'):
            found.append('%s: internal (underscore) variables cannot be set in config.yml' % ascii(key))
        elif key in MAGIC_KEYS:
            found.append('%s: Ansible magic variable' % ascii(key))
        elif key in FACTS_SET_BEFORE_CONFIG or key in DERIVED_FACTS:
            found.append('%s: derived at run time, not configuration' % ascii(key))
        elif protected.fullmatch(key) and key not in CONFIG_SETTABLE_PROTECTED:
            found.append('%s: protected variable (_protected_keys in scripts/verify-ansible-args.sh)' % ascii(key))
        elif key in vault_mapped:
            found.append('%s: comes from the vault; set it in group_vars/all/vault.yml (make vault-edit)' % ascii(key))
        elif key in FIXED_EXECUTABLE_KEYS:
            found.append('%s: an executable the roles run from a fixed location' % ascii(key))
        elif key in ALLOWED_EXECUTABLE_VALUES and not (
                isinstance(data[key], str) and ALLOWED_EXECUTABLE_VALUES[key](data[key])):
            found.append('%s: not an allowed executable location (SECURITY.md, "Executable paths")' % ascii(key))
        elif key in ALLOWED_VALUES and not (isinstance(data[key], str) and ALLOWED_VALUES[key](data[key])):
            found.append('%s: not an allowed value (see its default.config.yml comment and SECURITY.md)' % ascii(key))
        elif key in SYSCTL_DICT_KEYS and isinstance(data[key], dict):
            for name in map(str, data[key]):
                if not SYSCTL_KEY.fullmatch(name):
                    found.append('%s.%s: not a well-formed sysctl key' % (ascii(key), ascii(name)))
                elif SYSCTL_EXEC_KEYS.fullmatch(sysctl_name(name)):
                    found.append('%s.%s: a sysctl the kernel runs or reads as a path' % (ascii(key), ascii(name)))
    found += _control_chars(data, [])
    return found


def main(argv):
    try:
        # strict UTF-8 whatever the locale (a C locale reads stdin with surrogateescape): the error type only
        data = load(sys.stdin.buffer.read().decode('utf-8'))
        output = json.dumps(data, default=json_default)
    except Exception as error:
        print(f"Cannot parse config.yml: {type(error).__name__}", file=sys.stderr)
        return 1
    if argv[1:] == ['--policy']:
        try:
            found = policy_violations(data)
        except Exception as error:  # fail closed: an unreadable source of truth rejects the config
            detail = f": {error}" if isinstance(error, PolicySourceError) else ""
            print(f"Cannot check config.yml keys: {type(error).__name__}{detail}", file=sys.stderr)
            return 3
        print('\n'.join(found))
        return 3 if found else 0
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
