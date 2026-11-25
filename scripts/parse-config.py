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
    # Jinja and Ansible template globals: a key named lookup makes every later lookup() call fail
    'cycler', 'dict', 'joiner', 'lipsum', 'lookup', 'namespace', 'now', 'q', 'query', 'range', 'undef', 'zip',
})
# Facts Play 0 sets before pre-flight loads config.yml (pre-flight.yml). config.yml values are set
# with set_fact, so they would overwrite these; include_vars used to lose to them.
FACTS_SET_BEFORE_CONFIG = frozenset({'real_user_home', 'real_user_id', 'is_linux', 'is_macos'})
# The wrapper protects rollout_enable from -e only; config.yml is where it is set.
CONFIG_SETTABLE_PROTECTED = frozenset({'rollout_enable'})
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
        elif key in FACTS_SET_BEFORE_CONFIG:
            found.append('%s: set at run time before config.yml loads' % ascii(key))
        elif protected.fullmatch(key) and key not in CONFIG_SETTABLE_PROTECTED:
            found.append('%s: protected variable (_protected_keys in scripts/verify-ansible-args.sh)' % ascii(key))
        elif key in vault_mapped:
            found.append('%s: comes from the vault; set it in group_vars/all/vault.yml (make vault-edit)' % ascii(key))
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
