#!/usr/bin/env python3
"""Import a decrypted LastPass CSV export into Bitwarden with the Bitwarden CLI (`bw`), without exposing it.

Usage (via make, see docs/password-migration.md):
  make pw-migrate EXPORT=/dev/shm/lastpass.csv            # counts, confirm, import, verify, delete
  make pw-migrate EXPORT=/dev/shm/lastpass.csv DRY_RUN=1  # counts only, then delete (KEEP_EXPORT=1 keeps it)

Rules this script enforces:
- The export must be a regular file on a memory filesystem (tmpfs/ramfs on Linux, a RAM disk on macOS), so the
  plaintext never reaches persistent storage; every copy it makes (the TOTP-stripped file) is written next to it.
- It prints only counts. Nothing from the export, and nothing from `bw` that could echo it, reaches the terminal.
- The export and any copy are overwritten and removed on success, failure, Ctrl-C or SIGTERM.
- TOTP seeds are stripped before import unless STRIP_TOTP=0: the owner's plan keeps them on the YubiKey.

The `bw` command comes from BW_CMD (default `bw`); it must already be logged in and unlocked (BW_SESSION).
"""
import csv
import json
import os
import platform
import plistlib
import shlex
import signal
import stat
import subprocess
import sys

LASTPASS_COLUMNS = {'url', 'username', 'password', 'totp', 'extra', 'name', 'grouping', 'fav'}
SECURE_NOTE_URL = 'http://sn'
MEMORY_FS = {'tmpfs', 'ramfs'}
_to_delete = []


def fail(message, code=1):
    print(f'pw-migrate: {message}', file=sys.stderr)
    sys.exit(code)


def wipe(path):
    """Overwrite then unlink. On a memory filesystem the overwrite only shortens the window; it never hurts."""
    try:
        size = os.path.getsize(path)
        with open(path, 'r+b', buffering=0) as handle:
            handle.write(b'\0' * size)
            os.fsync(handle.fileno())
        os.unlink(path)
    except FileNotFoundError:
        pass


def wipe_all():
    while _to_delete:
        wipe(_to_delete.pop())


def on_signal(signum, _frame):
    wipe_all()
    fail(f'interrupted by signal {signum}; export removed', 130)


def linux_fs_type(path):
    real = os.path.realpath(path)
    best, fstype = '', None
    with open('/proc/self/mounts', encoding='utf-8') as mounts:
        for line in mounts:
            fields = line.split()
            mount_point = fields[1].replace('\\040', ' ')
            if (real == mount_point or real.startswith(mount_point.rstrip('/') + '/')) and len(mount_point) >= len(best):
                best, fstype = mount_point, fields[2]
    return fstype


def macos_on_ramdisk(path):
    """True when the file's device is an attached `hdiutil attach ram://...` image."""
    device = subprocess.run(['df', path], capture_output=True, text=True, check=True).stdout.splitlines()[-1].split()[0]
    info = plistlib.loads(subprocess.run(['hdiutil', 'info', '-plist'], capture_output=True, check=True).stdout)
    for image in info.get('images', []):
        if str(image.get('image-path', '')).startswith('ram://'):
            for entity in image.get('system-entities', []):
                if device.startswith(entity.get('dev-entry', '\0')):
                    return True
    return False


def check_export(path):
    if not path:
        fail('EXPORT is required: make pw-migrate EXPORT=/dev/shm/lastpass.csv', 2)
    info = os.lstat(path) if os.path.lexists(path) else None
    if info is None:
        fail(f'{path}: not found', 2)
    if not stat.S_ISREG(info.st_mode):
        fail(f'{path}: not a regular file (symlinks are refused)', 2)
    system = platform.system()
    if system == 'Linux':
        fstype = linux_fs_type(path)
        if fstype not in MEMORY_FS:
            fail(f'{path} is on {fstype or "an unknown filesystem"}, not tmpfs: move it to /dev/shm or '
                 '$XDG_RUNTIME_DIR first (it was not modified)', 2)
    elif system == 'Darwin':
        if not macos_on_ramdisk(path):
            fail(f'{path} is not on a RAM disk: see docs/password-migration.md (it was not modified)', 2)
    else:
        fail(f'unsupported system {system}', 2)
    os.chmod(path, 0o600)


def read_rows(path):
    with open(path, newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        missing = LASTPASS_COLUMNS - set(reader.fieldnames or [])
        if missing:
            fail(f'not a LastPass CSV export: missing columns {sorted(missing)}')
        return list(reader)


def counts(rows):
    groups = {row['grouping'] for row in rows if row['grouping']}
    return {
        'items': len(rows),
        'logins': sum(row['url'] != SECURE_NOTE_URL for row in rows),
        'secure_notes': sum(row['url'] == SECURE_NOTE_URL for row in rows),
        'folders': len(groups),
        'shared_folder_items': sum(row['grouping'].startswith('Shared-') for row in rows),
        'totp': sum(bool(row['totp'].strip()) for row in rows),
        'attachments': 0,  # the CSV export carries none (docs/password-migration.md)
    }


def report(title, values):
    print(title)
    for key, value in values.items():
        print(f'  {key.replace("_", " ")}: {value}')


def strip_totp(path, rows):
    stripped = path + '.no-totp'
    descriptor = os.open(stripped, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    _to_delete.append(stripped)
    with os.fdopen(descriptor, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else sorted(LASTPASS_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, 'totp': ''})
    return stripped


def bw(*args):
    """Run bw; return stdout. Its output may contain vault data, so only the exit status is ever shown."""
    command = shlex.split(os.environ.get('BW_CMD', 'bw')) + list(args)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        fail(f'`bw {args[0]}` failed (exit {result.returncode}); its output is hidden because it may contain '
             'vault data. Check `bw status` (logged in and unlocked, BW_SESSION exported).')
    return result.stdout


def vault_counts():
    bw('sync')
    items = json.loads(bw('list', 'items', '--organizationid', 'null'))
    folders = json.loads(bw('list', 'folders'))
    return {
        'items': len(items),
        'secure_notes': sum(item.get('type') == 2 for item in items),
        'folders': sum(folder.get('id') is not None for folder in folders),
        'totp': sum(bool((item.get('login') or {}).get('totp')) for item in items),
    }


def main():
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    path = os.environ.get('EXPORT', '')
    check_export(path)
    dry_run = os.environ.get('DRY_RUN') == '1'
    if not (dry_run and os.environ.get('KEEP_EXPORT') == '1'):
        _to_delete.append(path)
    try:
        rows = read_rows(path)
        expected = counts(rows)
        report('LastPass export (counts only):', expected)
        strip = os.environ.get('STRIP_TOTP', '1') != '0'
        if expected['totp'] and strip:
            print(f'  -> {expected["totp"]} TOTP seeds will NOT be imported (STRIP_TOTP=0 keeps them); move them '
                  'to the YubiKey from LastPass by hand first.')
        if dry_run:
            print('Dry run: nothing imported.' + ('' if path in _to_delete else ' Export kept (KEEP_EXPORT=1).'))
            return
        before = vault_counts()
        report('Bitwarden personal vault before import:', before)
        if os.environ.get('ASSUME_YES') != '1':
            answer = input(f'Import {expected["items"]} items into this Bitwarden vault? [y/N] ')
            if answer.strip().lower() != 'y':
                print('Not imported.')
                return
        source = strip_totp(path, rows) if strip and expected['totp'] else path
        bw('import', 'lastpasscsv', source)
        after = vault_counts()
        delta = {key: after[key] - before[key] for key in after}
        report('Imported (vault after minus before):', delta)
        want = {'items': expected['items'], 'secure_notes': expected['secure_notes'],
                'totp': 0 if strip else expected['totp']}
        mismatch = {key: (delta[key], value) for key, value in want.items() if delta[key] != value}
        if delta['folders'] > expected['folders']:
            mismatch['folders'] = (delta['folders'], expected['folders'])
        if mismatch:
            fail('verify FAILED (got, expected): ' + json.dumps(mismatch) + '; review the vault before retrying, '
                 'a second import duplicates items')
        print('Verify OK: item, secure-note and TOTP counts match; new folders do not exceed the export\'s '
              '(existing folders with the same name are reused).')
    finally:
        wipe_all()
        if path not in _to_delete and not os.path.exists(path):
            print('Export removed.')


if __name__ == '__main__':
    main()
