#!/usr/bin/env python3
"""Write local_bookmarks.yml from the bookmarks in this machine's Chrome profiles (docs/chrome-bookmarks.md).

Read-only on Chrome: reads each profile's Bookmarks and AccountBookmarks files and the profile names in Local State
(nothing else), and works while Chrome runs. Output keeps to what `make chrome-bookmarks` accepts: http and https links
only, no URL credentials, folders at most 5 deep (deeper links move up), at most 2000 entries per profile. An existing
file is kept as local_bookmarks.yml.bak-<time> (the newest 5); the new one is written to a temporary file and renamed
into place. Links whose query names a credential (token, sig, key, code, auth, ...) and an earlier import's
laptop-setup folder are left out.
Prints counts only, never names or URLs.
"""
import argparse
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import platform
from urllib.parse import parse_qsl, urlsplit
import re
import shutil
import sys
import tempfile
import unicodedata

import yaml

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / 'local_bookmarks.yml'
_spec = importlib.util.spec_from_file_location('chrome_bookmarks', ROOT / 'scripts/chrome-bookmarks.py')
render = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render)
ROOTS = [('bookmark_bar', None), ('other', 'Other bookmarks'), ('synced', 'Mobile bookmarks')]
FILES = ['Bookmarks', 'AccountBookmarks']  # local storage, and the account storage of a signed-in, non-syncing profile
MAX_NESTING = 100  # deeper input is malformed: stop reading it rather than recurse without bound
# query or fragment parameter names that usually carry a credential: such links are left out, never printed. Long
# words count anywhere in a name (accessToken, X-Amz-Signature); short ones only as a whole word of it (key, apiKey).
SECRET_ANYWHERE = ('token', 'secret', 'signature', 'password', 'passwd', 'credential', 'session', 'jwt')
SECRET_WORDS = {'key', 'apikey', 'code', 'sig', 'otp', 'auth', 'authorization', 'pwd', 'ticket'}
BACKUP = re.compile(r'\.bak-\d{8}-\d{6}-\d{6}$')
BACKUPS_KEPT = 5


def chrome_dir():
    if platform.system() == 'Darwin':
        return Path.home() / 'Library/Application Support/Google/Chrome'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'google-chrome'


def slug(name, taken):
    base = re.sub(r'[^a-z0-9]+', '-', str(name).lower()).strip('-')[:28].rstrip('-') or 'profile'
    key, n = base, 2
    while key in taken:
        key, n = f'{base}-{n}', n + 1
    return key


def clean_name(text, fallback):
    # control, format (bidi) and surrogate characters would break the YAML, the HTML or the bar of names
    text = ''.join(' ' if unicodedata.category(c) in ('Cc', 'Cf', 'Cs') else c for c in str(text or ''))
    return text.strip()[:render.MAX_NAME] or fallback


class Export:
    def __init__(self):
        self.count = 0
        self.secret_hosts = set()  # shown only with --show-left-out-hosts
        self.earlier = set()  # links from an earlier file: a link in both Bookmarks and AccountBookmarks goes out once
        self.this_file = set()
        self.skipped = {'not http(s) or with credentials': 0, 'with a secret-looking parameter': 0,
                        'over the entry limit': 0, 'nested too deep': 0, 'already in Bookmarks': 0,
                        'earlier laptop-setup import': 0, 'unreadable bookmarks file': 0}

    def items(self, children, depth):
        out = []
        if not isinstance(children, list):
            return out
        if depth > MAX_NESTING:
            self.skipped['nested too deep'] += self.links(children)
            return out
        for node in children:
            if not isinstance(node, dict):
                continue
            if node.get('type') == 'folder':
                if node.get('name') == render.FOLDER:  # what an earlier import of this export created: not again
                    self.skipped['earlier laptop-setup import'] += self.links(node.get('children'))
                    continue
                if depth >= render.MAX_DEPTH:  # too deep for the renderer: keep the links one level up
                    out.extend(self.items(node.get('children'), depth + 1))
                    continue
                if not self.take():  # the folder counts before its links
                    self.skipped['over the entry limit'] += self.links(node.get('children')) - 1
                    continue
                inner = self.items(node.get('children'), depth + 1)
                if inner:
                    out.append({'folder': clean_name(node.get('name'), 'Folder'), 'items': inner})
                else:
                    self.count -= 1
            elif node.get('type') == 'url':
                url = node.get('url')
                try:
                    render.check_url(url, '')
                except render.Invalid:
                    self.skipped['not http(s) or with credentials'] += 1
                    continue
                if secret_looking(url):
                    self.skipped['with a secret-looking parameter'] += 1
                    self.secret_hosts.add(urlsplit(url).hostname or '')
                    continue
                if url in self.earlier:
                    self.skipped['already in Bookmarks'] += 1
                    continue
                if self.take():
                    self.this_file.add(url)
                    out.append({'name': clean_name(node.get('name'), urlsplit(url).hostname or 'Link'), 'url': url})
        return out

    def links(self, children):
        """How many exportable (http/https) links lie under children (iterative: no depth limit to hit)."""
        found, stack = 0, [children]
        while stack:
            nodes = stack.pop()
            for node in nodes if isinstance(nodes, list) else []:
                if isinstance(node, dict):
                    if node.get('type') == 'url':
                        try:
                            render.check_url(node.get('url'), '')
                            found += 1
                        except render.Invalid:
                            pass
                    stack.append(node.get('children'))
        return found

    def take(self):
        if self.count >= render.MAX_ITEMS:
            self.skipped['over the entry limit'] += 1
            return False
        self.count += 1
        return True


def secret_looking(url):
    parts = urlsplit(url)
    names = [name for name, _ in parse_qsl(parts.query, keep_blank_values=True)]
    names += [name for name, _ in parse_qsl(parts.fragment, keep_blank_values=True)]
    for name in names:
        if any(word in name.lower() for word in SECRET_ANYWHERE):
            return True
        words = re.split(r'[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])', name)
        if SECRET_WORDS & {word.lower() for word in words}:
            return True
    return False


def profile_names(base):
    try:
        cache = json.loads((base / 'Local State').read_text(encoding='utf-8'))['profile']['info_cache']
        return {folder: str(info.get('name') or folder) for folder, info in cache.items() if isinstance(info, dict)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def natural(name):
    return [int(part) if part.isdigit() else part for part in re.split(r'(\d+)', name)]


def export_profile(directory):
    """(items, Export) from a profile's bookmark files; items is empty when none hold an http(s) link."""
    export, items, wrappers = Export(), [], {}
    for name in FILES:
        path = directory / name
        if not path.is_file():
            continue
        try:
            roots = json.loads(path.read_text(encoding='utf-8')).get('roots')
        except (OSError, ValueError, AttributeError, RecursionError):
            roots = None
        if not isinstance(roots, dict):
            export.skipped['unreadable bookmarks file'] += 1  # counted per file, not per link
            continue
        for key, label in ROOTS:
            root = roots.get(key)
            if not isinstance(root, dict):
                continue
            if label is None:
                items.extend(export.items(root.get('children'), 1))
                continue
            if label not in wrappers:  # one "Other bookmarks" folder for both files
                if not export.take():
                    export.skipped['over the entry limit'] += export.links(root.get('children')) - 1
                    continue
                wrappers[label] = {'folder': label, 'items': []}
                items.append(wrappers[label])
            wrappers[label]['items'].extend(export.items(root.get('children'), 2))
        export.earlier |= export.this_file
        export.this_file = set()
    for label, wrapper in wrappers.items():
        if not wrapper['items']:
            items.remove(wrapper)
            export.count -= 1
    return items, export


def write(path, text):
    """Keep the previous file as <name>.bak-<time>, then put text in place atomically (0600)."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError(f'{path} is not a regular file')
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text)
        if path.exists():
            backup = path.with_name(f'{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S-%f}')
            shutil.copy2(path, backup)
            os.chmod(backup, 0o600)
            ours = sorted(p for p in path.parent.glob(f'{path.name}.bak-*') if BACKUP.search(p.name))
            for old in ours[:-BACKUPS_KEPT]:
                try:
                    old.unlink()
                except OSError as error:
                    print(f'chrome-bookmarks-export: could not remove old backup {old.name} ({error.strerror})',
                          file=sys.stderr)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--chrome-dir', type=Path, default=chrome_dir())
    parser.add_argument('--output', type=Path, default=TARGET)
    parser.add_argument('--show-left-out-hosts', action='store_true',
                        help='also name the hosts of links left out for a secret-looking parameter, to re-add by hand')
    args = parser.parse_args()
    base = args.chrome_dir
    if not base.is_dir():
        print(f'chrome-bookmarks-export: no Chrome data at {base}', file=sys.stderr)
        return 1
    names = profile_names(base)
    folders = sorted((p.name for p in base.iterdir() if any((p / name).is_file() for name in FILES)), key=natural)
    profiles, report = {}, []
    for folder in folders:
        items, export = export_profile(base / folder)
        skipped = ', '.join(f'{n} {why}' for why, n in export.skipped.items() if n)
        left_out = f' ({skipped} left out)' if skipped else ''
        if not items:
            report.append(f'{folder}: no http(s) bookmarks, skipped' + left_out)
            continue
        name = names.get(folder, folder)
        key = slug(folder if '@' in name else name, profiles)  # an email-shaped profile name never becomes a key
        profiles[key] = items
        report.append(f'{folder} -> {key}: {export.count} entries' + left_out)
        if args.show_left_out_hosts and export.secret_hosts:
            report.append(f'  secret-looking links left out, hosts: {", ".join(sorted(export.secret_hosts))}')
    if not profiles:
        print('chrome-bookmarks-export: no profile has http(s) bookmarks; nothing written', file=sys.stderr)
        return 1
    text = ('# Exported from this machine\'s Chrome profiles by make chrome-bookmarks-export; edit freely, then run\n'
            '# make chrome-bookmarks (docs/chrome-bookmarks.md). Gitignored: it can name internal hosts.\n'
            + yaml.safe_dump({'chrome_bookmarks': profiles}, sort_keys=False, allow_unicode=True, width=1000))
    # what is written must load and validate exactly as make chrome-bookmarks reads it
    try:
        render.load_text(text, args.output)
    except render.Invalid:  # its message can quote a bookmark: not printed
        print('chrome-bookmarks-export: the export does not validate; nothing written', file=sys.stderr)
        return 1
    try:
        write(args.output, text)
    except OSError as error:
        print(f'chrome-bookmarks-export: cannot write {args.output} ({error.strerror or error}); nothing written',
              file=sys.stderr)
        return 1
    print('\n'.join(report))
    print(f'wrote {args.output} ({len(profiles)} profiles); next: review it, then make chrome-bookmarks')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
