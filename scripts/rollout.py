#!/usr/bin/env python3
"""Chunk registry helper: validate rollout_enable, print --skip-tags, print status.

A chunk's name is its Ansible tag. rollout_enable (config.yml) is 'all' or a list of chunk
names; every other chunk is skipped with --skip-tags. Exit status 2 means invalid input.
"""
import argparse
import difflib
import re
import subprocess
import sys
from pathlib import Path

from ansible.parsing.dataloader import DataLoader

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / 'group_vars/all/rollout.yml'
CONFIG = ROOT / 'config.yml'
RISKS = ('low', 'medium', 'high')
NAME = re.compile(r'^[a-z][a-z0-9_]*$')
RESERVED = {'all', 'always', 'become', 'common', 'never', 'system', 'tagged', 'untagged'}  # tags with a meaning of their own


class RolloutError(Exception):
    pass


def type_name(value):
    """Plain builtin type name (ansible's YAML loader returns str subclasses)."""
    return next(t.__name__ for t in type(value).__mro__ if t.__module__ == 'builtins')


def load_yaml(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = DataLoader().load(path.read_text(), show_content=False)
    except Exception as error:  # ansible raises several parser error types
        raise RolloutError(f'{path.name} is not valid YAML ({type(error).__name__}); fix it and retry') from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RolloutError(f'{path.name} must contain a mapping')
    return data


def load_catalog(registry=REGISTRY):
    """Return (catalog, default) with the catalog validated; order is the file order."""
    data = load_yaml(registry)
    catalog = data.get('rollout_catalog')
    if not isinstance(catalog, dict) or not catalog:
        raise RolloutError('rollout_catalog must be a non-empty mapping')
    seen = []
    for name, entry in catalog.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED:
            raise RolloutError(f'chunk name {name!r} must match {NAME.pattern} and not be a reserved tag (it is an Ansible tag)')
        if not isinstance(entry, dict):
            raise RolloutError(f'chunk {name}: entry must be a mapping')
        if not isinstance(entry.get('desc'), str) or not entry['desc']:
            raise RolloutError(f'chunk {name}: desc is required')
        if entry.get('risk') not in RISKS:
            raise RolloutError(f'chunk {name}: risk must be one of {", ".join(RISKS)}')
        for field in ('requires', 'verify'):
            value = entry.get(field, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise RolloutError(f'chunk {name}: {field} must be a list of strings')
        for need in entry.get('requires', []):
            if need not in seen:
                raise RolloutError(f'chunk {name}: requires {need}, which must be listed earlier')
        seen.append(name)
    return catalog, data.get('rollout_enable', 'all')


def suggest(name, known):
    close = difflib.get_close_matches(name, known, n=1)
    return f" (did you mean '{close[0]}'?)" if close else ''


def enabled_chunks(catalog, setting):
    """Resolve rollout_enable to the enabled chunk names in registry order; raise on bad input."""
    names = list(catalog)
    if setting == 'all':
        return names
    if not isinstance(setting, list) or not all(isinstance(v, str) for v in setting):
        raise RolloutError(f"config.yml: rollout_enable is {setting!r} ({type_name(setting)}); use 'all' or a list of chunk names "
                           f"such as [packages, ssh_client] (make status shows them all)")
    unknown = [v for v in setting if v not in catalog]
    if unknown:
        hints = '; '.join(f"unknown chunk '{v}'{suggest(v, names)}" for v in unknown)
        raise RolloutError(f"rollout_enable: {hints}. Valid: {' '.join(names)}")
    if len(set(setting)) != len(setting):
        dup = sorted({v for v in setting if setting.count(v) > 1})
        raise RolloutError(f"rollout_enable lists a chunk twice: {' '.join(dup)}")
    wanted = set(setting)
    missing = [f'{n} needs {r}' for n in names if n in wanted
               for r in catalog[n].get('requires', []) if r not in wanted]
    if missing:
        raise RolloutError('rollout_enable is missing prerequisites (add them): ' + '; '.join(missing))
    return [n for n in names if n in wanted]


def resolve(registry=REGISTRY, config=CONFIG):
    catalog, default = load_catalog(registry)
    setting = load_yaml(config).get('rollout_enable', default)
    return catalog, enabled_chunks(catalog, setting)


def skip_tags(catalog, enabled):
    return [n for n in catalog if n not in enabled]


def closure(catalog, name):
    """Chunks that name transitively requires (they may host its tasks through inherited tags)."""
    seen, todo = set(), list(catalog[name].get('requires', []))
    while todo:
        need = todo.pop()
        if need not in seen:
            seen.add(need)
            todo.extend(catalog[need].get('requires', []))
    return seen


def require_enabled(catalog, enabled, name):
    if name not in catalog:
        raise RolloutError(f"unknown chunk '{name}'{suggest(name, list(catalog))}. Valid: {' '.join(catalog)}")
    if name not in enabled:
        raise RolloutError(f"chunk '{name}' is not enabled; add it to rollout_enable in config.yml (make status shows the list)")


def chunk_args(catalog, enabled, name):
    """Extra ansible-playbook arguments that apply exactly one enabled chunk."""
    require_enabled(catalog, enabled, name)
    system = name.startswith('sys_')
    # System sub-chunks share the `system` role tag (its untagged prerequisites must run), so they are
    # selected by complement; every other chunk is selected by its own tag. Required chunks stay unskipped:
    # intra-role features inherit their parent's tag (login_shell inside desktop) and a system chunk must still
    # run the always-tagged guards of a required non-system chunk (the ssh role's lockout check). Other
    # system chunks stay skipped, because the `system` tag would otherwise select them too.
    required = closure(catalog, name)
    keep = {name} | (required if not system else {n for n in required if not n.startswith('sys_')})
    skip = [n for n in catalog if n not in keep]
    sudo = catalog[name].get('sudo', True)
    if not sudo:
        skip.append('become')
    args = ['--tags', f"common,{'system' if system else name}"]
    if skip:
        args += ['--skip-tags', ','.join(skip)]
    if sudo:
        args.append('--ask-become-pass')
    return args


def confirm_high_risk(catalog, name):
    if catalog[name]['risk'] != 'high':
        return
    if not sys.stdin.isatty():
        raise RolloutError(f"'{name}' is a high-risk chunk (it can lock you out or break boot): run it from a terminal")
    # stderr, never stdout: make captures stdout as ansible-playbook arguments
    sys.stderr.write(f"'{name}' is high risk: {catalog[name]['desc']}.\n"
                     'See docs/rollout-recovery.md and keep a second session open.\n'
                     f'Type the chunk name ({name}) to continue: ')
    sys.stderr.flush()
    if sys.stdin.readline().strip() != name:
        raise RolloutError('not confirmed')


def verify_command(catalog, enabled, name=None):
    chunks = [name] if name else enabled
    checks = []
    for chunk in chunks:
        for check in catalog[chunk].get('verify', []):
            if check not in checks:
                checks.append(check)
    if not checks:
        return None
    kinds = {c.startswith('sys_') for c in chunks}
    scope = ['--system-only'] if kinds == {True} else ['--user-only'] if kinds == {False} else []
    return [str(ROOT / 'scripts/smoke-test.sh'), *scope, '--only', ','.join(checks)]


def next_hint(catalog, enabled):
    """Suggest the first enabled chunk, preferring one that needs no sudo prompt."""
    first = next((n for n in enabled if not catalog[n].get('sudo', True)), enabled[0] if enabled else None)
    return f'Next: make plan CHUNK={first}' if first else 'Next: add chunks to rollout_enable in config.yml'


def status_table(catalog, enabled):
    width = max(len(n) for n in catalog)
    lines = [f'{"#":>2}  {"chunk":<{width}}  {"risk":<6}  {"state":<5}  {"sudo":<4}  description']
    for i, (name, entry) in enumerate(catalog.items(), 1):
        state = 'ON' if name in enabled else 'off'
        sudo = 'yes' if entry.get('sudo', True) else 'no'
        lines.append(f'{i:>2}  {name:<{width}}  {entry["risk"]:<6}  {state:<5}  {sudo:<4}  {entry["desc"]}')
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=['skip-tags', 'status', 'chunk-args', 'banner', 'verify'])
    parser.add_argument('chunk', nargs='?', default='')
    parser.add_argument('--preview', action='store_true', help='chunk-args for a read-only preview: skip the high-risk confirmation')
    parser.add_argument('--config', default=str(CONFIG))
    args = parser.parse_args(argv)
    try:
        catalog, enabled = resolve(config=args.config)
    except RolloutError as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 2
    off = skip_tags(catalog, enabled)
    if args.command == 'skip-tags':
        print(','.join(off))
    elif args.command == 'banner':
        if off:
            print(f'ROLLOUT: {len(enabled)} of {len(catalog)} chunks enabled; make applies only those. '
                  f'Skipped: {", ".join(off)}. (make status lists them)', file=sys.stderr)
    elif args.command == 'status':
        print('\n'.join(status_table(catalog, enabled)))
        if off:
            print(f'\nmake all applies only the ON chunks ({len(enabled)} of {len(catalog)}).')
            print(next_hint(catalog, enabled))
        else:
            print('\nAll chunks are enabled; set rollout_enable in config.yml to a list to apply them one at a time.')
            print(next_hint(catalog, list(catalog)))
    elif args.command in ('chunk-args', 'verify'):
        name = args.chunk
        try:
            if args.command == 'chunk-args':
                if not name:
                    raise RolloutError('chunk-args needs a chunk name')
                result = chunk_args(catalog, enabled, name)
                if not args.preview:
                    confirm_high_risk(catalog, name)
                print(' '.join(result))
                return 0
            if name:
                require_enabled(catalog, enabled, name)
        except RolloutError as error:
            print(f'ERROR: {error}', file=sys.stderr)
            return 2
        command = verify_command(catalog, enabled, name or None)
        if command is None:
            print(f"no smoke check is mapped to {name or 'the enabled chunks'} yet, so its state is unknown; "
                  f"run make plan CHUNK={name or '<chunk>'} and look for changed=0")
            return 1
        code = subprocess.run(command).returncode
        if code == 0:
            print('\nNext: make status')
        else:
            where = name or 'the failing chunk'
            print(f'\nNext: see docs/rollout-recovery.md for {where}')
            if '--user-only' not in command:
                print('Root-only checks show WARN or SKIP as a normal user; run them as root: sudo ' + ' '.join(command))
        return code
    return 0


if __name__ == '__main__':
    sys.exit(main())
