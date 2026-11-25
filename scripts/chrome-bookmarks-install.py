#!/usr/bin/env python3
"""Restore the backed-up work and personal bookmarks into the matching Chrome profiles; verify every link.

Usage: chrome-bookmarks-install.py [--machine NAME | --archive FILE] [--identity FILE] [--chrome-dir DIR]
                                   [--check | --verify-only | --extract DIR]

The archive (chrome-bookmarks/<NAME>.tar.age; with one archive there, that one) must be encrypted to exactly the keys in
chrome-bookmarks/recipients and match the SHA-256 in <NAME>.counts.json. It is decrypted with age, by default with the
plugged-in YubiKey (one touch; the identity stub comes from age-plugin-yubikey --identity), and checked against its
manifest: every raw file's SHA-256, every url field read, the sets holding exactly the profiles' links. --verify-only
stops there and touches no Chrome file: passing it with each key plugged in alone proves each key opens a complete
backup. --extract DIR writes the raw Bookmarks files out (0700 directory) for a restore by hand.

The set work goes to the one profile signed in to a redhat.com account, personal to the one signed in to a gmail.com
or googlemail.com account (names do not matter). Each root's items are merged into that root of the profile's
Bookmarks file: folders are matched by name and by order among same-named siblings, or created; a link is added unless
its folder already holds it as often as the set does. A second run adds nothing, and nothing is ever removed or
renamed. Account storage (Sync's copy) is not counted, since it may be reset. Chrome must be closed (it rewrites the
file from memory); this is checked again right before writing. The previous file is kept as
Bookmarks.laptop-setup-bak-<time> (0600), and the file is read back afterwards and every link checked. Prints counts only.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'chrome-bookmarks'
_spec = importlib.util.spec_from_file_location('chrome_bookmarks_backup', ROOT / 'scripts/chrome-bookmarks-backup.py')
backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup)
ACCOUNTS = {'work': ('redhat.com',), 'personal': ('gmail.com', 'googlemail.com')}
KNOWN_ROOTS = ('bookmark_bar', 'other', 'synced')


class Failure(Exception):
    pass


def now():
    """Chrome's time: microseconds since 1601-01-01, as a string."""
    return str(int((datetime.now(timezone.utc).timestamp() + 11644473600) * 1_000_000))


def decrypt(archive, identity, key=None):
    """Decrypt with an identity file, or with the one plugged-in YubiKey key (by serial: other keys stay idle)."""
    with tempfile.TemporaryDirectory() as scratch:
        ids = identity
        if ids is None:
            ids = Path(scratch) / 'identity'
            stub = subprocess.run(['age-plugin-yubikey', '--identity', '--serial', key['serial'], '--slot', key['slot']],
                                  capture_output=True, text=True, timeout=60)
            if stub.returncode or not stub.stdout.strip():
                raise Failure(f'no age identity on YubiKey {key["serial"]} slot {key["slot"]}')
            fd = os.open(ids, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(stub.stdout)
            pin = '' if key.get('pin') == 'Never' else 'enter its PIN if asked, then '
            print(f'chrome-bookmarks-install: YubiKey {key["serial"]}: {pin}touch it when it blinks', file=sys.stderr)
        result = subprocess.run(['age', '--decrypt', '-i', str(ids), str(archive)], capture_output=True, timeout=300)
    if result.returncode:
        raise Failure(f'cannot decrypt {archive.name}: {result.stderr.decode(errors="replace").strip()[:200]}')
    return result.stdout


def recipient_keys(recipients):
    """The plugged-in YubiKeys whose recipient is listed, in serial order."""
    return sorted((k for k in backup.plugged_keys() if k['recipient'] in recipients), key=lambda k: k['serial'])


def open_archive(archive, identity, recipients_file, key=None):
    data = archive.read_bytes()
    try:
        backup.check_header(data, backup.recipients_of(recipients_file))
    except backup.Refused as error:
        raise Failure(f'{archive.name}: {error}') from None
    counts = archive.with_name(archive.name.replace('.tar.age', '.counts.json'))
    if not counts.is_file():
        raise Failure(f'{counts.name} is missing: it holds the SHA-256 the archive must match')
    if json.loads(counts.read_text(encoding='utf-8')).get('archive_sha256') != backup.sha(data):
        raise Failure(f'{archive.name} does not match the SHA-256 in {counts.name}')
    members = backup.read_tar(decrypt(archive, identity, key))
    try:
        manifest = backup.verify(members)
    except (backup.Refused, ValueError, KeyError) as error:
        raise Failure(f'{archive.name} fails its manifest ({error})') from None
    return manifest, json.loads(members['sets.json']), members


def profiles_by_key(data_dir):
    try:
        cache = json.loads((data_dir / 'Local State').read_text(encoding='utf-8'))['profile']['info_cache']
    except (OSError, ValueError, KeyError, TypeError):
        cache = {}
    found = {key: [] for key in ACCOUNTS}
    for directory, info in (cache.items() if isinstance(cache, dict) else []):
        email = str(info.get('user_name') or '') if isinstance(info, dict) else ''
        domain = email.rsplit('@', 1)[-1].lower() if '@' in email else ''
        for key, domains in ACCOUNTS.items():
            if domain in domains and (data_dir / directory).is_dir():
                found[key].append(directory)
    return found


def empty_bookmarks(stamp):
    def root(i, name):
        return {'id': str(i), 'name': name, 'type': 'folder', 'children': [], 'date_added': stamp, 'date_modified': '0'}
    return {'version': 1, 'roots': {'bookmark_bar': root(1, 'Bookmarks bar'), 'other': root(2, 'Other bookmarks'),
                                    'synced': root(3, 'Mobile bookmarks')}}


def max_id(node):
    best = int(node['id']) if isinstance(node, dict) and str(node.get('id', '')).isdigit() else 0
    for child in node.get('children', []) if isinstance(node, dict) and isinstance(node.get('children'), list) else []:
        best = max(best, max_id(child))
    return best


def merge(items, parent, ids, counts, stamp):
    """Merge items into parent: the k-th folder named X matches the k-th child folder named X; a URL is added until
    the folder holds it as often as items do."""
    children = parent.setdefault('children', [])
    have = Counter(c['url'] for c in children if isinstance(c, dict) and isinstance(c.get('url'), str))
    wanted, seen = Counter(), Counter()
    for item in items:
        if 'folder' in item:
            seen[item['folder']] += 1
            same = [c for c in children if isinstance(c, dict) and 'url' not in c and c.get('name') == item['folder']]
            if len(same) >= seen[item['folder']]:
                node = same[seen[item['folder']] - 1]
            else:
                node = {'id': str(next(ids)), 'name': item['folder'], 'type': 'folder', 'children': [],
                        'date_added': stamp, 'date_modified': '0'}
                children.append(node)
            merge(item['items'], node, ids, counts, stamp)
            continue
        wanted[item['url']] += 1
        if wanted[item['url']] <= have[item['url']]:
            counts['present'] += 1
            continue
        children.append({'id': str(next(ids)), 'name': item['name'], 'type': 'url', 'url': item['url'],
                         'date_added': stamp})
        counts['added'] += 1


def place(data, by_root, ids, counts, stamp):
    roots = data['roots']
    for key, items in by_root.items():
        if key in KNOWN_ROOTS and isinstance(roots.get(key), dict):
            merge(items, roots[key], ids, counts, stamp)
        else:  # a root this Chrome does not have: keep its items in a folder under Other bookmarks
            merge([{'folder': key, 'items': items}], roots['other'], ids, counts, stamp)


def missing(data, by_root):
    copy = json.loads(json.dumps(data))
    counts = {'added': 0, 'present': 0}
    place(copy, by_root, iter(range(10 ** 12, 10 ** 13)), counts, '0')
    return counts['added']


def install(data_dir, profile_dir, by_root, check):
    path = profile_dir / 'Bookmarks'
    stamp = now()
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else empty_bookmarks(stamp)
    except (OSError, UnicodeDecodeError, ValueError):
        raise Failure(f'{profile_dir.name}/Bookmarks is unreadable') from None
    if not isinstance(data, dict) or not isinstance(data.get('roots'), dict) or \
            not all(isinstance(data['roots'].get(k), dict) for k in KNOWN_ROOTS):
        raise Failure(f'{profile_dir.name}/Bookmarks lacks the standard roots')
    counts = {'added': 0, 'present': 0}
    ids = iter(range(max_id({'children': list(data['roots'].values())}) + 1, 10 ** 12))
    place(data, by_root, ids, counts, stamp)
    if check or not counts['added']:
        return counts
    if backup.chrome_running(data_dir):  # again: the YubiKey touch may have taken a while
        raise Failure(f'Chrome started meanwhile: quit it and run again (nothing written to {profile_dir.name}; a profile '
                      'printed above as added was written)')
    data.pop('checksum', None)  # Chrome recomputes it, and gives the new nodes GUIDs, when it loads the file
    if path.exists():
        kept = profile_dir / f'Bookmarks.laptop-setup-bak-{datetime.now():%Y%m%d-%H%M%S}'
        shutil.copyfile(path, kept)
        os.chmod(kept, 0o600)
    fd, temporary = tempfile.mkstemp(dir=profile_dir, prefix='.Bookmarks.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=3, ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    left = missing(json.loads(path.read_text(encoding='utf-8')), by_root)
    if left:
        raise Failure(f'{profile_dir.name}: {left} links missing after writing')
    return counts


def pick_archive(args):
    if args.archive:
        return args.archive
    if args.machine:
        if not backup.MACHINE.fullmatch(args.machine):
            raise Failure('--machine: lowercase letters, digits and hyphens')
        return STORE / f'{args.machine}.tar.age'
    found = sorted(STORE.glob('*.tar.age'))
    if len(found) != 1:
        raise Failure(f'{len(found)} archives in {STORE}: name one with --machine (make: MACHINE=)')
    return found[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--machine', default=os.environ.get('CHROME_BOOKMARKS_MACHINE') or None)
    parser.add_argument('--archive', type=Path, default=None)
    parser.add_argument('--recipients', type=Path, default=STORE / 'recipients')
    parser.add_argument('--identity', type=Path, default=None)
    parser.add_argument('--chrome-dir', type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--extract', type=Path, metavar='DIR')
    args = parser.parse_args()
    data_dir = args.chrome_dir or backup.chrome_dir()
    try:
        archive = pick_archive(args)
        if not archive.is_file():
            raise Failure(f'no archive {archive}')
        touches_chrome = not (args.verify_only or args.extract)
        if touches_chrome:
            try:
                (data_dir / 'Local State').stat()
            except PermissionError as error:
                raise Failure(backup.mac_access_hint(error)) from None
            except OSError:
                pass
            if not (data_dir / 'Local State').is_file():
                raise Failure(f'no Chrome data at {data_dir}: start Chrome once and sign in')
            if backup.chrome_running(data_dir):
                print('chrome-bookmarks-install: running: Chrome holds its profiles; quit Chrome (every window) and run again')
                return 2
        recipients = backup.recipients_of(args.recipients)
        if args.identity:
            keys = [None]
        else:
            keys = recipient_keys(recipients)
            if not keys:
                raise Failure('no plugged-in YubiKey is listed in recipients: plug in an enrolled key')
        if not args.verify_only:
            keys = keys[:1]  # one key opens it; the others stay untouched
        for key in keys:
            manifest, sets, members = open_archive(archive, args.identity, args.recipients, key)
            total = sum(e['links'] for e in manifest['profiles'].values())
            who = f'YubiKey {key["serial"]}' if key else 'the given identity'
            print(f'{archive.name}: {who} opens it; verified: {len(manifest["profiles"])} profiles, {total} links (work '
                  f'{manifest["sets"]["work"]}, personal {manifest["sets"]["personal"]}); every file and link matches the '
                  'manifest')
        if args.verify_only and not args.identity:
            plugged = {k['recipient'] for k in keys}
            unchecked = [backup.recipient_tag(r) or r[:16] for r in recipients if r not in plugged]
            if unchecked:
                print(f'not checked (not plugged in): {", ".join(unchecked)}: the backup is safe to rely on once every '
                      'listed key has opened it')
            else:
                print(f'all {len(recipients)} listed keys open the archive: safe to rely on')
        if args.extract:
            args.extract.mkdir(mode=0o700, parents=True, exist_ok=False)
            base = args.extract.resolve()
            for name, data in members.items():
                if name.startswith('profiles/'):
                    target = (base / name).resolve()
                    if base not in target.parents:  # verify() already refuses such names; never write outside anyway
                        raise Failure(f'archive member {name[:80]!r} points outside {args.extract}')
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
            print(f'raw files in {args.extract}/profiles/: copy one over a closed profile\'s Bookmarks to restore it as is. '
                  f'They are plaintext: delete {args.extract} when done')
            return 0
        if args.verify_only:
            return 0
        found, failed = profiles_by_key(data_dir), False
        for key in ('work', 'personal'):
            by_root = sets.get(key) or {}
            if not by_root:
                continue
            dirs = found[key]
            if len(dirs) != 1:
                print(f'{key}: {"no" if not dirs else len(dirs)} Chrome profile(s) signed in to '
                      f'{" or ".join(ACCOUNTS[key])}: sign in to exactly one, then run again')
                failed = True
                continue
            counts = install(data_dir, data_dir / dirs[0], by_root, args.check)
            verb = 'would add' if args.check else 'added'
            print(f'{key}: profile {dirs[0]}: {verb} {counts["added"]}, {counts["present"]} already in place'
                  + ('' if args.check else '; every link verified in place'))
        return 1 if failed else 0
    except Failure as error:
        print(f'chrome-bookmarks-install: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
