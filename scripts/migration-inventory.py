#!/usr/bin/env python3
"""Inventory Git migration metadata without reading file contents or fetching."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit, urlunsplit

EXCLUDED = {
    '.git', '.cache', '.local', '.npm', '.npm-global', '.mozilla', '.ansible',
    '.ansible_async', '.oh-my-zsh', '.codex', '.codex-personal', '.claude',
    '.claude-per2', '.claude-personal', '.claude-work', '.vagrant.d',
    'node_modules', 'vendor', '__pycache__', '.venv', '.terraform',
}


def safe_remote(url, home):
    """Remove URL credentials and query/fragment tokens; abbreviate local homes."""
    if '://' in url:
        # Git remote helpers can prefix a URI with "helper::".
        prefix, uri = '', url
        helper = re.match(r'^([A-Za-z0-9_+.-]+::)([A-Za-z][A-Za-z0-9+.-]*://.*)$', url)
        if helper:
            prefix, uri = helper.groups()
        parsed = urlsplit(uri)
        hostname = parsed.hostname or ''
        if ':' in hostname:
            hostname = '[' + hostname + ']'
        if parsed.port:
            hostname += ':' + str(parsed.port)
        # SSH login names are useful metadata; passwords are never included.
        if parsed.scheme == 'ssh' and parsed.username:
            hostname = parsed.username + '@' + hostname
        url = prefix + urlunsplit((parsed.scheme, hostname, parsed.path, '', ''))
    return url.replace(str(home) + '/', '~/')


def git(path, *args):
    result = subprocess.run(
        ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false',
         '-c', 'core.hooksPath=/dev/null', '-C', str(path), *args],
        capture_output=True, timeout=45, check=True,
    )
    return result.stdout.decode('utf-8', errors='replace')


def inventory(roots):
    rows, errors, seen = [], [], set()
    home = Path.home()
    # Config homes can themselves be Git repositories. Inspect those roots only;
    # their caches and installed plugin clones stay excluded from traversal.
    config_homes = ['.claude', '.claude-work', '.claude-personal', '.claude-per2',
                    '.codex', '.codex-personal']
    roots = list(roots)
    for base in tuple(roots):
        roots.extend(base / name for name in config_homes if (base / name / '.git').exists())
    for base in roots:
        if not base.is_dir():
            errors.append({'path': str(base), 'error': 'root does not exist'})
            continue
        def walk_error(error):
            errors.append({'path': error.filename, 'error': 'directory unreadable'})
        for current, dirs, files in os.walk(base, followlinks=False, onerror=walk_error):
            dirs[:] = [name for name in dirs if name not in EXCLUDED
                       and not Path(current, name).is_symlink()
                       and not (name == 'pkg' and Path(current).name == 'go')]
            path = Path(current)
            if base.name in EXCLUDED:
                dirs.clear()
            if not (path / '.git').exists() or path in seen:
                continue
            seen.add(path)
            try:
                remotes = {}
                for name in git(path, 'remote').splitlines():
                    remotes[name] = safe_remote(git(path, 'remote', 'get-url', name).strip(), home)
                refs = []
                for line in git(path, 'for-each-ref',
                                '--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)',
                                'refs/heads').splitlines():
                    branch, upstream, tracking = line.split('\t')
                    refs.append({'branch': branch, 'upstream': upstream, 'tracking': tracking})
                try:
                    head = git(path, 'rev-parse', '--verify', '--quiet', 'HEAD').strip()
                except subprocess.CalledProcessError as error:
                    if error.returncode != 1:
                        raise
                    head = None  # initialized repositories can have uncommitted files but no commits
                rows.append({
                    'path': str(path).replace(str(home) + '/', '~/'),
                    'gitfile': (path / '.git').is_file(),
                    'head': head,
                    'remotes': remotes,
                    'branches': refs,
                    'modified': len([s for s in git(path, 'diff', '--name-only', '-z').split('\0') if s]),
                    'staged': len([s for s in git(path, 'diff', '--cached', '--name-only', '-z').split('\0') if s]),
                    'untracked': len([s for s in git(path, 'ls-files', '--others', '--exclude-standard', '-z').split('\0') if s]),
                })
            except (subprocess.SubprocessError, ValueError, OSError):
                errors.append({'path': str(path), 'error': 'Git metadata probe failed'})
    return {'created_utc': datetime.now(timezone.utc).isoformat(),
            'exclusions': sorted(EXCLUDED), 'repositories': rows, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots', type=Path, nargs='*', default=[Path.home()])
    parser.add_argument('--output', type=Path, required=True,
                        help='New local JSON file; created exclusively with mode 0600')
    args = parser.parse_args()
    # Reserve the destination before a potentially long scan; never overwrite a report.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            report = inventory([root.absolute() for root in args.roots])
            json.dump(report, stream, indent=2)
            stream.write('\n')
    except BaseException:
        args.output.unlink(missing_ok=True)
        raise
    print(f"Inventoried {len(report['repositories'])} repositories; "
          f"{len(report['errors'])} probe errors. Report: {args.output}")
    return 1 if report['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
