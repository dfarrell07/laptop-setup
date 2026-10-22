#!/usr/bin/env python3
"""Allow connected USB devices now and keep them allowed via config.yml.

With USBGuard running, every blocked device is allowed for this session
(usbguard allow-device) and a matching rule is appended to
system_usbguard_extra_rules in config.yml (unless that exact rule, or an id-only rule, is
already saved), so the next `make system` writes it to /etc/usbguard/rules.conf. rules.conf itself is Ansible-managed, so it is
never edited here.

While USBGuard is not running, connected devices whose VID:PID no 'allow id' rule covers
are read from sysfs and only saved to config.yml, so the first provision
already allows them.

If the run is interrupted or fails after devices were allowed, their rules are still saved
(or printed for manual entry when config.yml cannot be written).
Exit status: 0 success, 1 error or the five-round limit was reached, 130 interrupted.
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
    if 'id' not in attrs or not re.fullmatch(r'[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}', attrs['id']):
        return None  # never build a rule from a missing, wildcard or malformed id
    name = attrs.get('name', '""')
    return {
        'number': int(number),
        'target': target,
        'id': attrs['id'].lower(),
        'name': safe_name(name[1:-1] if name.startswith('"') else name),
        'interfaces': attrs.get('with-interface', ''),
    }


def format_interfaces(interfaces):
    interfaces = sorted(set(interfaces))
    if len(interfaces) == 1:
        return interfaces[0]
    return '{ ' + ' '.join(interfaces) + ' }' if interfaces else ''


def _fold_id(rule):
    """Lower-case only the VID:PID of a rule (names stay case-sensitive)."""
    return re.sub(r'^(\s*\w+ id )(\S+)', lambda m: m.group(1) + m.group(2).lower(), rule)


def saved_for_live(device, rules):
    """True when a saved rule is this exact rule or an id-only rule that matches the device."""
    exact = build_rule(device)
    for rule in rules:
        if _fold_id(rule) == exact:
            return True
        match = re.fullmatch(r'\s*allow id (\S+)\s*', rule)
        if match and fnmatch.fnmatchcase(device['id'], match.group(1).lower()):
            return True
    return False


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
            except (OSError, UnicodeDecodeError):
                return ''
        interfaces = []
        unreadable_interface = False
        for iface in path.glob(path.name + ':*'):
            fields = [attr(iface, n) for n in ('bInterfaceClass', 'bInterfaceSubClass', 'bInterfaceProtocol')]
            if all(fields):
                interfaces.append(':'.join(f.lower() for f in fields))
            else:
                unreadable_interface = True
        device_id = '%s:%s' % (attr(path, 'idVendor').lower(), attr(path, 'idProduct').lower())
        if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{4}', device_id):
            print('[warn] skipping %s: unreadable or malformed USB id' % path.name, file=sys.stderr)
            continue  # never save a broken rule
        if unreadable_interface:
            print('[warn] skipping %s: unreadable interface attributes (a rule would be too broad)' % device_id,
                  file=sys.stderr)
            continue
        devices.append({
            'number': None,
            'target': 'present',
            'id': device_id,
            'name': safe_name(attr(path, 'product')),
            'interfaces': format_interfaces(interfaces),
        })
    return devices


def rule_ids(rules):
    return [m.group(1).lower() for r in rules for m in [re.match(r'\s*allow id (\S+)', r)] if m]


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

    start = next((i for i, l in enumerate(lines) if re.match(r'["\']?' + KEY + r'["\']?\s*:', l)), None)
    if start is None:
        return ''.join(lines) + KEY + ':\n' + ''.join(item(r, c, '  ') for r, c in entries)
    value = lines[start].split(':', 1)[1].split('#', 1)[0].strip()
    if value.replace(' ', '').lower() in ('[]', 'null', '~'):
        lines[start] = KEY + ':\n'
    elif value:
        raise ValueError('%s in config.yml is not a block list; add the rules by hand' % KEY)
    end, indent = start + 1, '  '
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or (stripped.startswith('#') and not lines[i][0].isspace()):
            continue  # blank and column-0 comment lines neither end nor extend the list
        if not lines[i][0].isspace() and not stripped.startswith('- '):
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
    path = path.resolve()  # write through a symlinked config.yml instead of replacing the link
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.config.yml.')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(new_text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:  # already renamed into place
            pass
        raise


def usbguard_active():
    if not shutil.which('usbguard'):
        return False
    return subprocess.run(['systemctl', 'is-active', '--quiet', 'usbguard'], check=False).returncode == 0


def blocked_devices():
    out = subprocess.run(['sudo', 'usbguard', 'list-devices', '--blocked'],
                         check=True, capture_output=True, text=True, errors='replace').stdout
    return [d for d in map(parse_device_line, out.split('\n')) if d]


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
    if not os.access(args.config.resolve().parent, os.W_OK) or not os.access(args.config, os.R_OK | os.W_OK):
        sys.exit('ERROR: cannot read and write %s; fix permissions before allowing any device' % args.config)
    text = args.config.read_text()
    config = load_yaml(text)
    if not isinstance(config, dict):
        sys.exit('ERROR: %s is not a YAML mapping' % args.config)
    if config.get(KEY) is not None and not isinstance(config.get(KEY), list):
        sys.exit('ERROR: %s must be a list in config.yml' % KEY)
    existing = list(config.get(KEY) or [])
    whitelist = config.get('system_usbguard_whitelist')
    if whitelist is None:  # an explicit empty list is honoured
        whitelist = load_yaml(DEFAULTS.read_text()).get('system_usbguard_whitelist', [])
    if not isinstance(whitelist, list) or not isinstance(config.get(KEY) or [], list) \
            or not all(isinstance(rule, str) for rule in whitelist + existing):
        sys.exit('ERROR: system_usbguard_whitelist and %s must be lists of strings in config.yml' % KEY)
    live = usbguard_active()
    if not live:
        print('USBGuard is not running: checking connected devices that no rule allows by VID:PID.')

    # Dry-run the real edit so an unsupported layout fails before any device is allowed
    probe = 'allow id 0000:0000'
    if list(load_yaml(add_rules_to_config(text, [(probe, 'probe')])).get(KEY) or []) != existing + [probe]:
        sys.exit('ERROR: cannot edit %s in %s safely; add the rules by hand' % (KEY, args.config))
    today = datetime.date.today().isoformat()
    added = []
    allowed_numbers = set()  # live devices already allowed in this run
    capped = finalizing = declined = False
    def save():
        write_config(args.config, add_rules_to_config(text, added), existing + [r for r, _ in added])

    try:
        for _ in range(5):  # allowing a hub can reveal blocked devices behind it
            if live:
                devices = [d for d in blocked_devices() if d['number'] not in allowed_numbers]
            else:
                devices = [d for d in read_sysfs() if not covered(d, whitelist + existing + [r for r, _ in added])]
            new = []
            for device in devices:
                rule = build_rule(device)
                if saved_for_live(device, existing + [r for r, _ in added]) or rule in [r for r, _, _ in new]:
                    if live:
                        new.append((None, None, device))  # already saved: allow it for this session only
                else:
                    new.append((rule, device['name'] or device['id'], device))
            if not new:
                break
            print('\nDevices to allow:')
            for rule, _, device in new:
                print('  %-10s %s' % (device['id'], rule or '(rule already saved)'))
            if not confirm('Allow these and save any new rules to config.yml?', args.yes):
                declined = True
                break
            for rule, label, device in new:
                if live:
                    subprocess.run(['sudo', 'usbguard', 'allow-device', str(device['number'])], check=True)
                    allowed_numbers.add(device['number'])
                if rule:
                    added.append((rule, '%s, added by make usb-add %s' % (label, today)))
            if not live:
                break
            time.sleep(1)
        else:
            if live and [d for d in blocked_devices() if d['number'] not in allowed_numbers]:
                capped = True
                print('WARNING: stopped after 5 rounds; more devices are still blocked, run make usb-add again.', file=sys.stderr)
        if not added:
            if allowed_numbers:
                print('Allowed %d device(s) for this session; their rules were already saved.' % len(allowed_numbers))
            else:
                print('Declined: nothing was allowed or saved.' if declined else 'Nothing to add.')
            return 1 if capped else 0
        finalizing = True
        save()
    except BaseException:
        if added and finalizing:
            print('Add these to %s by hand:' % KEY, file=sys.stderr)
            for rule, _ in added:
                print("  - '%s'" % rule.replace("'", "''"), file=sys.stderr)
        elif added:  # keep rules for devices already allowed
            try:
                save()
                print('Saved %d rule(s) allowed before the failure.' % len(added), file=sys.stderr)
            except (ValueError, OSError) as exc:
                print('ERROR: could not save rules: %s' % exc, file=sys.stderr)
                print('Add these to %s by hand:' % KEY, file=sys.stderr)
                for rule, _ in added:
                    print("  - '%s'" % rule.replace("'", "''"), file=sys.stderr)
        raise
    print('\nSaved %d rule(s) to %s (%s).' % (len(added), args.config.name, KEY))
    print('Review them, then run: make system   (writes /etc/usbguard/rules.conf)')
    return 1 if capped else 0


def run():
    try:
        return main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, OSError, yaml.YAMLError, subprocess.CalledProcessError) as exc:
        sys.exit('ERROR: %s' % exc)


if __name__ == '__main__':
    sys.exit(run())
