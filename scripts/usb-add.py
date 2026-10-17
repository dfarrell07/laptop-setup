#!/usr/bin/env python3
"""Allow connected USB devices now and keep them allowed via config.yml.

With USBGuard running, every blocked device is allowed for this session
(usbguard allow-device) and a matching rule is appended to
system_usbguard_extra_rules in config.yml, so the next `make system` writes it
to /etc/usbguard/rules.conf. rules.conf itself is Ansible-managed, so it is
never edited here.

Before USBGuard is installed, connected devices whose VID:PID no rule covers
are read from sysfs and only saved to config.yml, so the first provision
already allows them.
"""
import argparse
import datetime
import fnmatch
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / 'config.yml'
DEFAULTS = ROOT / 'roles/system/defaults/main.yml'
SYSFS = Path('/sys/bus/usb/devices')
KEY = 'system_usbguard_extra_rules'
# usbguard attribute: name followed by a quoted string, a { set } or a bare word
ATTR_RE = re.compile(r'([a-z-]+) ("(?:[^"\\]|\\.)*"|\{[^}]*\}|\S+)')
SAFE_NAME_RE = re.compile(r'^[ -~]*$')


class _Loader(yaml.SafeLoader):
    pass


def _unsafe(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_Loader.add_constructor('!unsafe', _unsafe)


def load_yaml(text):
    return yaml.load(text, Loader=_Loader) or {}  # noqa: S506 - SafeLoader subclass


def safe_name(name):
    """Return a usbguard name value safe for rules and config.yml, or ''."""
    if not name or not SAFE_NAME_RE.match(name) or any(c in name for c in '"\\{}%#'):
        return ''
    return name.strip()


def parse_device_line(line):
    """Parse one `usbguard list-devices` line into a device dict."""
    number, _, rest = line.partition(':')
    if not number.strip().isdigit():
        return None
    rest = rest.strip()
    target, _, rest = rest.partition(' ')
    attrs = dict(ATTR_RE.findall(rest))
    if 'id' not in attrs:
        return None
    name = attrs.get('name', '""')
    return {
        'number': int(number),
        'target': target,
        'id': attrs['id'],
        'name': safe_name(name[1:-1] if name.startswith('"') else name),
        'interfaces': attrs.get('with-interface', ''),
    }


def format_interfaces(interfaces):
    interfaces = sorted(set(interfaces))
    if len(interfaces) == 1:
        return interfaces[0]
    return '{ ' + ' '.join(interfaces) + ' }' if interfaces else ''


def build_rule(device):
    parts = ['allow id ' + device['id']]
    if device['name']:
        parts.append('name "%s"' % device['name'])
    if device['interfaces']:
        parts.append('with-interface ' + device['interfaces'])
    return ' '.join(parts)


def read_sysfs(root=SYSFS):
    """Connected devices from sysfs, in the same shape as parse_device_line."""
    devices = []
    for path in sorted(root.glob('*')):
        if ':' in path.name or not (path / 'idVendor').exists():
            continue
        def attr(p, name):
            try:
                return (p / name).read_text().strip()
            except OSError:
                return ''
        interfaces = []
        for iface in path.glob(path.name + ':*'):
            fields = [attr(iface, n) for n in ('bInterfaceClass', 'bInterfaceSubClass', 'bInterfaceProtocol')]
            if all(fields):
                interfaces.append(':'.join(f.lower() for f in fields))
        devices.append({
            'number': None,
            'target': 'present',
            'id': '%s:%s' % (attr(path, 'idVendor').lower(), attr(path, 'idProduct').lower()),
            'name': safe_name(attr(path, 'product')),
            'interfaces': format_interfaces(interfaces),
        })
    return devices


def rule_ids(rules):
    return [m.group(1) for r in rules for m in [re.search(r'\bid (\S+)', r)] if m]


def covered(device, rules):
    return any(fnmatch.fnmatchcase(device['id'], pattern) for pattern in rule_ids(rules))


def add_rules_to_config(text, entries):
    """Append (rule, comment) pairs to the KEY list, keeping the rest of the file as is."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'

    def item(rule, comment, indent):
        quoted = "'" + rule.replace("'", "''") + "'"
        return '%s- %s  # %s\n' % (indent, quoted, comment)

    start = next((i for i, l in enumerate(lines) if re.match(KEY + r'\s*:', l)), None)
    if start is None:
        return ''.join(lines) + KEY + ':\n' + ''.join(item(r, c, '  ') for r, c in entries)
    value = lines[start].split(':', 1)[1].split('#', 1)[0].strip()
    if value == '[]':
        lines[start] = KEY + ':\n'
    elif value:
        raise ValueError('%s in config.yml is not a block list; add the rules by hand' % KEY)
    end, indent = start + 1, '  '
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if not lines[i][0].isspace():
            break
        if stripped.startswith('- '):
            indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
        end = i + 1
    lines[end:end] = [item(r, c, indent) for r, c in entries]
    return ''.join(lines)


def write_config(path, new_text, expected):
    rules = load_yaml(new_text).get(KEY) or []
    if list(rules) != expected:
        raise ValueError('config.yml edit did not produce the expected rule list; nothing written')
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.config.yml.')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(new_text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def usbguard_active():
    if not shutil.which('usbguard'):
        return False
    return subprocess.run(['systemctl', 'is-active', '--quiet', 'usbguard'], check=False).returncode == 0


def blocked_devices():
    out = subprocess.run(['sudo', 'usbguard', 'list-devices', '--blocked'],
                         check=True, capture_output=True, text=True).stdout
    return [d for d in map(parse_device_line, out.splitlines()) if d]


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        return input(prompt + ' [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-y', '--yes', action='store_true', help='do not ask for confirmation')
    parser.add_argument('--config', type=Path, default=CONFIG)
    args = parser.parse_args(argv)

    if not args.config.exists():
        sys.exit('ERROR: %s not found; copy default.config.yml first' % args.config)
    text = args.config.read_text()
    config = load_yaml(text)
    existing = list(config.get(KEY) or [])
    whitelist = config.get('system_usbguard_whitelist') or load_yaml(DEFAULTS.read_text()).get('system_usbguard_whitelist', [])
    live = usbguard_active()
    if not live:
        print('USBGuard is not running: checking connected devices that no rule allows by VID:PID.')

    today = datetime.date.today().isoformat()
    added = []
    for _ in range(5):  # allowing a hub can reveal blocked devices behind it
        if live:
            devices = blocked_devices()
        else:
            devices = [d for d in read_sysfs() if not covered(d, whitelist + existing + [r for r, _ in added])]
        new = []
        for device in devices:
            rule = build_rule(device)
            if rule not in existing and rule not in [r for r, _ in added] and rule not in [r for r, _, _ in new]:
                new.append((rule, device['name'] or device['id'], device))
            elif live:
                new.append((None, None, device))  # already saved: allow it for this session only
        if not new:
            break
        print('\nDevices to allow:')
        for rule, _, device in new:
            print('  %-10s %s' % (device['id'], rule or '(rule already in config.yml)'))
        if not confirm('Allow these and save the rules to config.yml?', args.yes):
            break
        for rule, label, device in new:
            if live:
                subprocess.run(['sudo', 'usbguard', 'allow-device', str(device['number'])], check=True)
            if rule:
                added.append((rule, '%s, added by make usb-add %s' % (label, today)))
        if not live:
            break
        time.sleep(1)

    if not added:
        print('Nothing to add.')
        return 0
    write_config(args.config, add_rules_to_config(text, added), existing + [r for r, _ in added])
    print('\nSaved %d rule(s) to %s (%s).' % (len(added), args.config.name, KEY))
    print('Review them, then run: make system   (writes /etc/usbguard/rules.conf)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
