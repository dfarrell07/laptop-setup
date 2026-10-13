#!/usr/bin/env python3
"""Create a private, read-only workstation metadata report for migration/drift review."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

TOOLS = ['aws', 'go', 'golangci-lint', 'ansible', 'python3', 'node', 'npm', 'codex',
         'claude', 'agy', 'gemini', 'yq', 'kubectl', 'oc', 'opm', 'grype', 'helm',
         'kind', 'git', 'git-review', 'ccache', 'tkn', 'oras', 'terraform', 'packer']
GNOME = {
    'org.gnome.desktop.interface': ['clock-format', 'clock-show-date', 'clock-show-weekday', 'color-scheme'],
    'org.gnome.desktop.peripherals.touchpad': ['tap-to-click', 'natural-scroll', 'disable-while-typing', 'click-method'],
    'org.gnome.desktop.input-sources': ['xkb-options'],
    'org.gnome.desktop.wm.preferences': ['num-workspaces', 'focus-mode'],
    'org.gnome.mutter': ['dynamic-workspaces', 'edge-tiling'],
}
CONFIG_PATHS = ['.zshrc', '.zlogin', '.gitconfig', '.vimrc', '.tmux.conf', '.ssh/config',
                '.config/mimeapps.list', '.config/xdg-terminals.list', '.config/htop/htoprc',
                '.config/zsh/local.zsh', '.config/containers/registries.conf', '.config/go/env',
                '.codex/config.toml', '.codex-personal/config.toml',
                '.claude/settings.json', '.claude-per2/settings.json']
SYSTEM_DIRS = ['/etc/modprobe.d', '/etc/udev/rules.d', '/etc/sysctl.d',
               '/etc/profile.d', '/etc/tmpfiles.d', '/etc/NetworkManager/conf.d']


def private_path(path, home):
    path = str(path)
    return '~' + path[len(str(home)):] if path == str(home) or path.startswith(str(home) + '/') else path


def safe_spec(value, home):
    """Keep package provenance but remove URI auth/query/fragment tokens."""
    if not isinstance(value, str):
        return None
    if '://' in value:
        # pipx can preserve a named PEP 508 direct reference ("package @ URI").
        prefix, uri = '', value
        reference = re.match(r'^([A-Za-z0-9_.-]+(?:\[[^\]]+\])?\s*@\s*)([A-Za-z][A-Za-z0-9+.-]*://.*)$', value)
        if reference:
            prefix, uri = reference.groups()
        parsed = urlsplit(uri)
        authority = parsed.hostname or ''
        if ':' in authority:
            authority = '[' + authority + ']'
        if parsed.port:
            authority += ':' + str(parsed.port)
        value = prefix + urlunsplit((parsed.scheme, authority, parsed.path, '', ''))
    return private_path(value, home)


def probe(command, errors, label):
    if not shutil.which(command[0]):
        return None  # platform/tool unavailable is different from a failed installed tool
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45,
                                env=dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1'))
        if result.returncode:
            errors.append({'probe': label, 'reason': 'command failed', 'exit_code': result.returncode})
            return None
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        errors.append({'probe': label, 'reason': 'command unavailable or timed out'})
        return None


def command_paths(names, path_value, home):
    result = {}
    directories = list(dict.fromkeys(path_value.split(os.pathsep)))
    for name in names:
        candidates = []
        for directory in directories:
            if not directory:
                continue  # never probe implicit current-directory PATH entries
            path = Path(directory) / name
            if path.is_file() and os.access(path, os.X_OK):
                candidates.append({'path': private_path(path.absolute(), home),
                                   'target': private_path(path.resolve(), home)})
        result[name] = {'selected': candidates[0]['path'] if candidates else None,
                        'candidates': candidates, 'shadowed': len({item['target'] for item in candidates}) > 1}
    return result


def pipx_state(home, errors):
    result = []
    for relative in ['.local/share/pipx/venvs', '.local/pipx/venvs']:
        base = home / relative
        try:
            if not stat.S_ISDIR(base.stat().st_mode):
                continue
            environments = sorted(base.iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            errors.append({'probe': 'pipx', 'path': private_path(base, home), 'reason': 'directory unreadable'})
            continue
        for venv in environments:
            metadata = venv / 'pipx_metadata.json'
            try:
                if not stat.S_ISREG(metadata.stat().st_mode):
                    continue
                data = json.loads(metadata.read_text())
                if not isinstance(data, dict):
                    raise ValueError('pipx metadata must be an object')
                package = data.get('main_package', {})
                if not isinstance(package, dict):
                    raise ValueError('pipx package metadata must be an object')
                python = venv / 'bin/python'
                result.append({'name': venv.name, 'version': package.get('package_version'),
                               'source': safe_spec(package.get('package_or_url'), home),
                               'interpreter_exists': python.exists(),
                               'interpreter_target': private_path(python.resolve(), home)})
            except FileNotFoundError:
                continue  # environments without optional metadata are not probe failures
            except (OSError, ValueError):
                errors.append({'probe': 'pipx', 'path': private_path(metadata, home), 'reason': 'metadata unreadable'})
    return result


def collect(home=None, path_value=None):
    home = home or Path.home()
    errors = []
    report = {'format_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'platform': {'system': platform.system(), 'machine': platform.machine()},
              'errors': errors}
    report['command_paths'] = command_paths(TOOLS, os.environ.get('PATH', '') if path_value is None else path_value, home)
    packages = probe(['rpm', '-qa', '--qf', '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{VENDOR}\n'], errors, 'rpm')
    report['rpm_packages'] = [dict(zip(['name', 'version', 'architecture', 'vendor'], row.split('\t')))
                              for row in (packages or '').splitlines()]
    explicit = probe(['dnf', '--cacheonly', 'repoquery', '--userinstalled', '--queryformat', '%{name}\n'], errors, 'dnf-userinstalled')
    report['rpm_userinstalled'] = sorted(set((explicit or '').splitlines()))
    deb = probe(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Status}\n'], errors, 'dpkg')
    report['deb_packages'] = [dict(zip(['name', 'version', 'status'], row.split('\t'))) for row in (deb or '').splitlines()]
    brew = probe(['brew', 'list', '--formula', '--versions'], errors, 'brew')
    report['brew_formulas'] = [{'name': row.split()[0], 'versions': row.split()[1:]} for row in (brew or '').splitlines() if row.split()]
    casks = probe(['brew', 'list', '--cask', '--versions'], errors, 'brew-casks') if platform.system() == 'Darwin' else None
    report['brew_casks'] = [{'name': row.split()[0], 'versions': row.split()[1:]} for row in (casks or '').splitlines() if row.split()]
    flatpak = probe(['flatpak', 'list', '--app', '--columns=application,version,origin'], errors, 'flatpak')
    report['flatpak_apps'] = [dict(zip(['application', 'version', 'origin'], row.split('\t'))) for row in (flatpak or '').splitlines()]
    npm = probe(['npm', 'ls', '--global', '--depth=0', '--json', '--ignore-scripts'], errors, 'npm')
    try:
        dependencies = json.loads(npm or '{}').get('dependencies', {})
        report['npm_global'] = [{'name': name, 'version': item.get('version')} for name, item in sorted(dependencies.items())]
    except (ValueError, AttributeError):
        report['npm_global'] = []
        errors.append({'probe': 'npm', 'reason': 'invalid metadata JSON'})
    report['pipx'] = pipx_state(home, errors)
    python_packages = []
    for directory in home.glob('.local/lib/python*/site-packages'):
        for distribution in importlib.metadata.distributions(path=[str(directory)]):
            python_packages.append({'name': distribution.metadata.get('Name'), 'version': distribution.version,
                                    'location': private_path(directory, home)})
    report['user_python_packages'] = sorted(python_packages, key=lambda item: (item['name'] or '', item['location']))
    services = probe(['systemctl', 'list-unit-files', '--state=enabled,masked', '--no-legend', '--no-pager', '--plain'], errors, 'system-units')
    user = probe(['systemctl', '--user', 'list-unit-files', '--state=enabled,masked', '--no-legend', '--no-pager', '--plain'], errors, 'user-units')
    for label, output in [('system_units', services), ('user_units', user)]:
        report[label] = [{'name': row.split()[0], 'state': row.split()[1]} for row in (output or '').splitlines() if len(row.split()) >= 2]
    mounts = probe(['findmnt', '--json', '--output', 'TARGET,FSTYPE'], errors, 'mounts')
    report['mounts'] = json.loads(mounts) if mounts else None
    report['gnome_preferences'] = []
    if shutil.which('gsettings'):
        schemas = set((probe(['gsettings', 'list-schemas'], errors, 'gnome-schemas') or '').splitlines())
        for schema, keys in GNOME.items():
            if schema not in schemas:
                continue
            available = set((probe(['gsettings', 'list-keys', schema], errors, 'gnome-keys') or '').splitlines())
            for key in keys:
                if key not in available:
                    continue
                value = probe(['gsettings', 'get', schema, key], errors, 'gnome:' + key)
                if value is not None:
                    report['gnome_preferences'].append({'schema': schema, 'key': key, 'value': value.strip()})
    configs = []
    for relative in CONFIG_PATHS:
        path = home / relative
        if not path.exists() and not path.is_symlink():
            continue
        configs.append({'path': relative, 'symlink': path.is_symlink(),
                        'target': private_path(path.resolve(), home), 'exists': path.exists()})
    report['config_paths'] = configs
    # Only publish policy flags from Go's persistent config, never proxy credentials.
    go_env = home / '.config/go/env'
    go_values = {}
    if go_env.is_file():
        try:
            for line in go_env.read_text().splitlines():
                key, separator, value = line.partition('=')
                if separator and key in ['GOSUMDB', 'GONOSUMDB', 'GOPRIVATE', 'GOPROXY', 'GOTOOLCHAIN']:
                    go_values[key] = value
        except OSError:
            errors.append({'probe': 'go-config', 'reason': 'configuration unreadable'})
    toolchain = go_values.get('GOTOOLCHAIN', '')
    credentialed_proxy = False
    for url in go_values.get('GOPROXY', '').split(','):
        try:
            parsed = urlsplit(url)
            credentialed_proxy = credentialed_proxy or parsed.username is not None or bool(parsed.query)
        except ValueError:
            errors.append({'probe': 'go-config', 'reason': 'invalid proxy URL'})
    report['go_config_policy'] = {
        'checksum_server_disabled': go_values.get('GOSUMDB') == 'off',
        'checksum_bypass_all': '*' in go_values.get('GONOSUMDB', '').split(','),
        'private_modules_configured': bool(go_values.get('GOPRIVATE')),
        'toolchain_preference': toolchain if re.fullmatch(r'(auto|local|go[0-9.]+(\+auto)?)', toolchain) else None,
        'credentialed_proxy': credentialed_proxy,
    }
    local_files = []
    for directory in SYSTEM_DIRS:
        base = Path(directory)
        if not base.is_dir():
            continue
        try:
            paths = sorted(base.iterdir())
        except OSError:
            errors.append({'probe': 'system-config-files', 'path': str(base), 'reason': 'directory unreadable'})
            continue
        for path in paths:
            if not path.is_file():
                continue
            manager, owner = None, None
            if shutil.which('rpm'):
                manager = 'rpm'
                owner = probe(['rpm', '-qf', '--qf', '%{NAME}', str(path)], [], 'file-owner')
            elif shutil.which('dpkg-query'):
                manager = 'dpkg'
                owner = probe(['dpkg-query', '-S', str(path)], [], 'file-owner')
                if owner:
                    owner = owner.split(': ', 1)[0]
            local_files.append({'path': str(path), 'package_manager': manager,
                                'package_owner': owner or None, 'symlink': path.is_symlink()})
    report['system_config_files'] = local_files
    report['findings'] = {
        'shadowed_commands': [name for name, value in report['command_paths'].items() if value['shadowed']],
        'broken_pipx_environments': [item['name'] for item in report['pipx'] if not item['interpreter_exists']],
        'unpackaged_system_files': [item['path'] for item in local_files if item['package_manager'] is not None and item['package_owner'] is None],
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New report file, created exclusively with mode 0600')
    args = parser.parse_args()
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        report = collect()
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(f"Report: {args.output}; {len(report['findings']['shadowed_commands'])} shadowed commands; "
          f"{len(report['findings']['broken_pipx_environments'])} broken pipx environments; "
          f"{len(report['errors'])} probe errors")
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
