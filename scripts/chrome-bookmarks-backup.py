#!/usr/bin/env python3
"""Back up every Chrome profile's bookmarks, complete, encrypted to the YubiKeys' public age recipients.

Usage: chrome-bookmarks-backup.py --machine NAME [--chrome-dir DIR] [--recipients FILE] [--out FILE] [--force]
                                  [--allow-running] [--min-recipients N] [--work-host HOST ...]

Reads each profile's Bookmarks and AccountBookmarks files verbatim (read-only) and sorts every link (bookmarklets and
token-carrying links included) into the sets work and personal, each keyed by Chrome root (bookmark_bar, other,
synced, and any other root the file has). chrome-bookmarks/<NAME>.tar.age holds the raw files, sets.json and
manifest.json (per profile: file SHA-256s and sizes, link count, sorted SHA-256 of each URL; profiles with no file are
listed too), encrypted with age to every recipient in chrome-bookmarks/recipients (at least two by default): no key,
PIN or touch is needed. Checks before encrypting, any failure writing nothing: every url field in the raw JSON is a
counted link; the sets hold exactly the profiles' links, each as often; the archive's files equal the files read
again from disk afterwards, which have not changed meanwhile (Chrome is refused while running unless
--allow-running). chrome-bookmarks/<NAME>.counts.json gets totals only. Prints counts and recipient tags only.

Work: a profile signed in to a redhat.com account, or signed out and named like redhat; and, from the other profiles,
links whose host is a --work-host or under one (default redhat.com and red.ht; --work-host replaces them). Everything
else, empty folders included, is personal. A signed-in profile's items keep their place; a signed-out profile's go in
a folder named after it within the same root.
"""
import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / 'chrome-bookmarks'
FILES = ('Bookmarks', 'AccountBookmarks')
ROOT_ORDER = ('bookmark_bar', 'other', 'synced')
WORK_HOSTS = ['redhat.com', 'red.ht']
MAX_NESTING = 200
MACHINE = re.compile(r'[a-z0-9][a-z0-9-]{0,40}')
MEMBER = re.compile(r'profiles/(Default|Profile \d+)/(Bookmarks|AccountBookmarks)')
BECH32 = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'


class Refused(Exception):
    pass


def chrome_dir():
    if platform.system() == 'Darwin':
        return Path.home() / 'Library/Application Support/Google/Chrome'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'google-chrome'


def chrome_running(data_dir):
    try:
        target = os.readlink(data_dir / 'SingletonLock')
    except OSError:
        return False
    host, _, pid = target.rpartition('-')
    if host != socket.gethostname() or not pid.isdigit():
        return True  # another host's lock or an unknown form: never write under it
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False  # stale, from a crash
    except PermissionError:
        return True
    return True


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def recipient_tag(recipient):
    """The 4-byte tag age-plugin-yubikey puts in its piv-p256 stanza; None for other recipient types."""
    if not recipient.startswith('age1yubikey1'):
        return None
    acc = bits = 0
    out = bytearray()
    for v in [BECH32.index(c) for c in recipient[len('age1yubikey1'):]][:-6]:
        acc, bits = (acc << 5) | v, bits + 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xff)
    return base64.b64encode(hashlib.sha256(bytes(out)).digest()[:4]).decode().rstrip('=')


def header_stanzas(archive_bytes):
    """[(type, first arg)] of an age file's recipient stanzas."""
    head = archive_bytes[:archive_bytes.find(b'\n---')]
    return [tuple((line[3:].split() + [''])[:2]) for line in head.decode('ascii', 'replace').splitlines()
            if line.startswith('-> ')]


def check_header(archive_bytes, recipients):
    """Raise unless the archive is encrypted to exactly the listed recipients (YubiKey ones by tag)."""
    stanzas = header_stanzas(archive_bytes)
    if len(stanzas) != len(recipients):
        raise Refused(f'the archive has {len(stanzas)} recipient stanzas, recipients lists {len(recipients)}')
    want = sorted(t for t in (recipient_tag(r) for r in recipients) if t)
    have = sorted(arg for kind, arg in stanzas if kind == 'piv-p256')
    if want != have:
        raise Refused('the archive is not encrypted to the YubiKeys in recipients')


def plugged_keys():
    """[{serial, slot, pin, recipient}] of every connected YubiKey's age identity (no PIN or touch needed)."""
    try:
        out = subprocess.run(['age-plugin-yubikey', '--list-all'], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    keys, current = [], {}
    for line in out.splitlines():
        m = re.match(r'#\s*Serial:\s*(\d+),\s*Slot:\s*(\d+)', line)
        if m:
            current = {'serial': m.group(1), 'slot': m.group(2), 'pin': ''}
            continue
        m = re.match(r'#\s*PIN policy:\s*(\w+)', line)
        if m and current:
            current['pin'] = m.group(1)
        elif re.fullmatch(r'age1[0-9a-z]{20,200}', line.strip()) and current:
            keys.append(dict(current, recipient=line.strip()))
            current = {}
    return keys


def mac_access_hint(error):
    """An EPERM reading Chrome's data on macOS is the OS privacy guard, not a missing file."""
    if platform.system() == 'Darwin' and isinstance(error, PermissionError):
        return ('macOS blocks access to Chrome\'s data: in System Settings > Privacy & Security > Full Disk Access (or '
                'App Management/App Data) allow the terminal app that runs this (Terminal, Ghostty), then run again')
    return f'cannot read Chrome\'s data ({error})'


def keys_command(recipients_file, add):
    """List plugged-in keys by serial against recipients; add the ones whose serials are given (never all at once)."""
    listed = recipients_of(recipients_file) if recipients_file.exists() else []
    keys = plugged_keys()
    if not keys:
        raise Refused('no YubiKey with an age identity is plugged in (age-plugin-yubikey --generate makes one)')
    missing = []
    for k in keys:
        state = 'listed' if k['recipient'] in listed else 'NOT listed'
        print(f'YubiKey {k["serial"]} slot {k["slot"]}: {state} in {recipients_file.name} (tag {recipient_tag(k["recipient"]) or "n/a"},'
              f' PIN policy {k["pin"] or "unknown"})')
        if k['recipient'] not in listed:
            missing.append(k)
    plugged = {k['recipient'] for k in keys}
    for r in listed:
        if r not in plugged:
            print(f'listed but not plugged in: tag {recipient_tag(r) or r[:16]}')
    if add:
        unknown = sorted(set(add) - {k['serial'] for k in keys})
        if unknown:
            raise Refused(f'no plugged-in YubiKey with serial {", ".join(unknown)}')
        chosen = [k for k in missing if k['serial'] in add]
        with recipients_file.open('a', encoding='utf-8') as stream:
            for k in chosen:
                stream.write(k['recipient'] + '\n')
                print(f'added YubiKey {k["serial"]} (tag {recipient_tag(k["recipient"]) or "n/a"})')
        if chosen:
            print(f'{recipients_file} changed: back up again (FORCE=1) so the archive includes the new key(s)')
    return 0


def recipients_of(path):
    lines = [l.strip() for l in path.read_text(encoding='utf-8').splitlines()]
    found = [l for l in lines if l and not l.startswith('#')]
    for r in found:
        # bech32 data characters only (no b, i, o or 1 after the separator): recipient_tag decodes YubiKey ones
        if not re.fullmatch(r'age1(yubikey1)?[' + BECH32 + r']{20,200}', r):
            raise Refused(f'{path}: not an age recipient line: {r[:16]}...')
    if len(set(found)) != len(found):
        raise Refused(f'{path}: a recipient is listed twice')
    return found


def url_fields(value, depth=0):
    """Every url field anywhere in a parsed Bookmarks file: the independent count the sets are checked against."""
    if depth > MAX_NESTING:
        raise Refused('bookmarks nested deeper than this backup reads')
    if isinstance(value, dict):
        n = 1 if isinstance(value.get('url'), str) else 0
        return n + sum(url_fields(v, depth + 1) for k, v in value.items() if k != 'meta_info')
    if isinstance(value, list):
        return sum(url_fields(v, depth + 1) for v in value)
    return 0


def tree(node, depth=0):
    """Chrome node -> set item ({name, url} or {folder, items}); typeless nodes judged by their fields."""
    if depth > MAX_NESTING:
        raise Refused('bookmarks nested deeper than this backup reads')
    if not isinstance(node, dict):
        return None
    if isinstance(node.get('url'), str):
        return {'name': str(node.get('name') or ''), 'url': node['url']}
    if isinstance(node.get('children'), list) or node.get('type') == 'folder':
        kids = [tree(c, depth + 1) for c in node.get('children', []) if isinstance(node.get('children'), list)]
        return {'folder': str(node.get('name') or ''), 'items': [k for k in kids if k is not None]}
    return None


def roots_of(data):
    """{root key: [items]} for every root of a Bookmarks file."""
    roots = data.get('roots') if isinstance(data, dict) else None
    if not isinstance(roots, dict):
        raise Refused('a Bookmarks file has no roots')
    out = {}
    for key, node in roots.items():
        item = tree(node)
        if item and 'folder' in item and item['items']:
            out[key] = item['items']
        elif item and 'url' in item:
            out[key] = [item]
    return out


def links(items, path=()):
    for item in items:
        if 'folder' in item:
            yield from links(item['items'], path + (item['folder'],))
        else:
            yield path, item['url']


def set_links(by_root):
    return [u for items in by_root.values() for _, u in links(items)]


def split(items, hosts):
    """(work, personal) of items by host; folders kept where they hold something, empty ones go to personal."""
    work, personal = [], []
    for item in items:
        if 'folder' in item:
            w, p = split(item['items'], hosts)
            if w:
                work.append({'folder': item['folder'], 'items': w})
            if p or not w:
                personal.append({'folder': item['folder'], 'items': p})
        else:
            try:
                host = (urlsplit(item['url']).hostname or '').rstrip('.').lower()
                is_work = any(host == h or host.endswith('.' + h) for h in hosts)
            except ValueError:  # an unparsable URL is still backed up, in the work set: never a work link in personal
                is_work = True
            (work if is_work else personal).append(item)
    return work, personal


def account(info):
    email = str(info.get('user_name') or '')
    domain = email.rsplit('@', 1)[-1].lower() if '@' in email else ''
    if domain == 'redhat.com':
        return 'work'
    if domain in ('gmail.com', 'googlemail.com'):
        return 'personal'
    return 'other' if domain else 'none'


def profile_dirs(data_dir):
    found = [p for p in data_dir.iterdir() if p.is_dir() and re.fullmatch(r'Default|Profile \d+', p.name)]
    return sorted(found, key=lambda p: -1 if p.name == 'Default' else int(p.name.split()[-1]))


def stat_of(path):
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def collect(data_dir, hosts):
    try:
        cache = json.loads((data_dir / 'Local State').read_text(encoding='utf-8'))['profile']['info_cache']
    except (OSError, ValueError, KeyError, TypeError):
        cache = {}
    profiles, raw, stats = {}, {}, {}
    sets = {'work': {}, 'personal': {}}
    for directory in profile_dirs(data_dir):
        info = cache.get(directory.name, {}) if isinstance(cache, dict) else {}
        kind, name = account(info), str(info.get('name') or directory.name)
        entry = {'account': kind, 'files': {}, 'links': 0, 'unique': 0, 'url_hashes': []}
        profiles[directory.name] = entry  # listed even with no file: "had none", not "missed"
        by_root = {}
        for fname in FILES:
            path = directory / fname
            if not path.is_file():
                continue
            member = f'profiles/{directory.name}/{fname}'
            stats[member] = (path, stat_of(path))
            data = path.read_bytes()
            raw[member] = data
            entry['files'][fname] = {'sha256': sha(data), 'bytes': len(data)}
            try:
                parsed = json.loads(data.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as error:
                raise Refused(f'{directory.name}/{fname} is unreadable ({type(error).__name__})') from None
            roots = roots_of(parsed)
            found = sum(1 for items in roots.values() for _ in links(items))
            if found != url_fields(parsed):
                raise Refused(f'{directory.name}/{fname}: {url_fields(parsed)} url fields but {found} links read')
            for key, items in roots.items():
                by_root.setdefault(key, []).extend(items)
        urls = set_links(by_root)
        entry.update(links=len(urls), unique=len(set(urls)), url_hashes=sorted(sha(u) for u in urls))
        work_profile = kind == 'work' or (kind == 'none' and 'redhat' in name.lower())
        for key, items in by_root.items():
            w, p = (items, []) if work_profile else split(items, hosts)
            for target, part in (('work', w), ('personal', p)):
                if not part:
                    continue
                if kind in ('work', 'personal'):  # a signed-in profile's items keep their place
                    sets[target].setdefault(key, []).extend(part)
                else:
                    sets[target].setdefault(key, []).append({'folder': f'{name} ({directory.name})', 'items': part})
    return profiles, raw, stats, sets


def build_tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(members):
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(members[name]), 0, 0o600
            tar.addfile(info, io.BytesIO(members[name]))
    return buf.getvalue()


def read_tar(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r') as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def verify(members):
    """Re-derive everything from the archive's own members; raise on any difference from its manifest."""
    manifest = json.loads(members['manifest.json'])
    sets = json.loads(members['sets.json'])
    listed = {'manifest.json', 'sets.json'} | {f'profiles/{d}/{f}' for d, e in manifest['profiles'].items() for f in e['files']}
    for name in members:  # nothing the manifest does not account for: a crafted archive could carry anything
        if name not in listed or (name.startswith('profiles/') and not MEMBER.fullmatch(name)):
            raise Refused(f'unexpected archive member {name[:80]!r}')
    every = Counter()
    for directory, entry in manifest['profiles'].items():
        urls = []
        for fname, meta in entry['files'].items():
            data = members.get(f'profiles/{directory}/{fname}')
            if data is None or sha(data) != meta['sha256'] or len(data) != meta['bytes']:
                raise Refused(f'{directory}/{fname}: missing or checksum differs')
            parsed = json.loads(data.decode('utf-8'))
            roots = roots_of(parsed)
            found = [u for items in roots.values() for _, u in links(items)]
            if len(found) != url_fields(parsed):
                raise Refused(f'{directory}/{fname}: not every url field was read')
            urls += found
        if len(urls) != entry['links'] or sorted(sha(u) for u in urls) != entry['url_hashes']:
            raise Refused(f'{directory}: links differ from the manifest')
        every.update(urls)
    in_sets = Counter(u for key in ('work', 'personal') for u in set_links(sets.get(key, {})))
    if in_sets != every:
        raise Refused(f'sets hold {sum(in_sets.values())} links, profiles {sum(every.values())}: not the same links')
    if manifest['sets'] != {k: len(set_links(sets.get(k, {}))) for k in ('work', 'personal')}:
        raise Refused('set counts differ from the manifest')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--machine', default=os.environ.get('CHROME_BOOKMARKS_MACHINE') or None)
    parser.add_argument('--chrome-dir', type=Path, default=None)
    parser.add_argument('--recipients', type=Path, default=STORE / 'recipients')
    parser.add_argument('--out', type=Path, default=None)
    parser.add_argument('--force', action='store_true', help='replace an existing archive')
    parser.add_argument('--allow-running', action='store_true', help='back up while Chrome runs (refused if a file changes)')
    parser.add_argument('--min-recipients', type=int, default=2,
                        help='refuse fewer recipients (default 2: one lost YubiKey must not lose the backup)')
    parser.add_argument('--work-host', action='append', dest='hosts', metavar='HOST')
    parser.add_argument('--keys', action='store_true', help='list the plugged-in YubiKeys by serial against recipients')
    parser.add_argument('--add', action='append', default=[], metavar='SERIAL',
                        help='with --keys: append this plugged-in key (by serial; repeat for more)')
    args = parser.parse_args()
    if args.keys:
        try:
            env = os.environ.get('CHROME_BOOKMARKS_ADD_SERIAL', '').split()
            if any(not s.isdigit() for s in env + args.add):
                raise Refused('a serial is digits only (make: SERIAL=<n>, several separated by spaces)')
            return keys_command(args.recipients, args.add + env)
        except Refused as error:
            raise SystemExit(f'chrome-bookmarks-backup: {error}') from None
    try:
        if not args.machine or not MACHINE.fullmatch(args.machine):
            raise Refused('name the backup with --machine (make: MACHINE=), e.g. old-laptop: lowercase letters, digits '
                          'and hyphens, not the hostname (the file name is public)')
        if args.machine == re.sub(r'[^a-z0-9-]+', '-', socket.gethostname().split('.')[0].lower()):
            raise Refused('--machine is this host\'s name, which would go public: pick a neutral one such as old-laptop')
        data_dir = args.chrome_dir or chrome_dir()
        out = args.out or STORE / f'{args.machine}.tar.age'
        if not out.name.endswith('.tar.age'):
            raise Refused('--out must end in .tar.age (its counts file goes beside it)')
        recipients = recipients_of(args.recipients)
        if len(recipients) < args.min_recipients:
            raise Refused(f'{args.recipients} lists {len(recipients)} recipient(s), fewer than {args.min_recipients}: '
                          'add another YubiKey (age-plugin-yubikey --list) so one lost key cannot lose the backup')
        if out.exists() and not args.force:
            raise Refused(f'{out} exists: --force (make: FORCE=1) replaces it')
        if not (data_dir / 'Local State').is_file():
            raise Refused(f'no Chrome data at {data_dir}')
        if chrome_running(data_dir) and not args.allow_running:
            raise Refused('Chrome is running: quit it (every window), or --allow-running (make: ALLOW_RUNNING=1)')
        try:
            profiles, raw, stats, sets = collect(data_dir, [h.lower().strip('.') for h in (args.hosts or WORK_HOSTS)])
        except PermissionError as error:
            raise Refused(mac_access_hint(error)) from None
        if not raw:
            raise Refused('no profile has a Bookmarks file')
        counts = {k: len(set_links(sets[k])) for k in ('work', 'personal')}
        manifest = {'machine': args.machine, 'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'profiles': profiles, 'sets': counts}
        members = dict(raw)
        members['manifest.json'] = json.dumps(manifest, indent=1, sort_keys=True).encode()
        members['sets.json'] = json.dumps(sets, ensure_ascii=False).encode()
        tar = build_tar(members)
        packed = read_tar(tar)
        verify(packed)
        for member, (path, before) in stats.items():  # the source, read again: unchanged and equal to the archive
            if stat_of(path) != before or path.read_bytes() != packed[member]:
                raise Refused(f'{member} changed during the backup (Chrome wrote it?): run again with Chrome closed')
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=out.parent, prefix=f'.{out.name}.')
        os.close(fd)
        try:
            cmd = ['age', '--encrypt', '-o', temporary] + [a for r in recipients for a in ('-r', r)]
            result = subprocess.run(cmd, input=tar, capture_output=True)
            if result.returncode:
                raise Refused(f'age failed: {result.stderr.decode(errors="replace").strip()[:200]}')
            check_header(Path(temporary).read_bytes(), recipients)
            os.chmod(temporary, 0o644)  # ciphertext, committed to the repository
            os.replace(temporary, out)
        finally:
            Path(temporary).unlink(missing_ok=True)
        public = {'machine': args.machine, 'created': manifest['created'], 'archive_sha256': sha(out.read_bytes()),
                  'recipients': len(recipients), 'profiles': len(profiles),
                  'links': sum(e['links'] for e in profiles.values()), 'sets': counts}
        out.with_name(out.name.replace('.tar.age', '.counts.json')).write_text(
            json.dumps(public, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    except Refused as error:
        raise SystemExit(f'chrome-bookmarks-backup: {error}; nothing written') from None
    for directory, entry in profiles.items():
        files = ', '.join(entry['files']) or 'no bookmark file'
        print(f'{directory}: {entry["account"]}, {entry["links"]} links ({entry["unique"]} unique); {files}')
    tags = ', '.join(recipient_tag(r) or r[:12] for r in recipients)
    print(f'sets: work {counts["work"]}, personal {counts["personal"]} = all {public["links"]} links, checked against '
          f'the source on disk; encrypted to {len(recipients)} recipients ({tags}): {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
