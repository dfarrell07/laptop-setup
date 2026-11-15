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
import re
import resource
import shlex
import signal
import stat
import subprocess
import sys

LASTPASS_COLUMNS = {'url', 'username', 'password', 'totp', 'extra', 'name', 'grouping', 'fav'}
SECURE_NOTE_URL = 'http://sn'
MEMORY_FS = {'tmpfs', 'ramfs'}
CLEANUP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT)
_to_delete = []
_export_id = None  # (st_dev, st_ino) of the export check_export approved
_import_started = False
_left = []  # files a wipe could not remove
PARTIAL = 'the vault may already hold part of the import: review it before retrying, a second import duplicates items'


def fail(message, code=1):
    print(f'pw-migrate: {message}', file=sys.stderr)
    sys.exit(code)


def open_checked(path, flags):
    """Reopen without following symlinks; the export must still be the file check_export approved."""
    descriptor = os.open(path, flags | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if path == os.environ.get('EXPORT') and _export_id and (info.st_dev, info.st_ino) != _export_id:
        os.close(descriptor)
        raise OSError(f'{path} was replaced after it was checked')
    return descriptor


def wipe(path):
    """Overwrite then unlink. On a memory filesystem the overwrite only shortens the window; it never hurts."""
    try:
        descriptor = open_checked(path, os.O_WRONLY)
    except FileNotFoundError:
        return
    with os.fdopen(descriptor, 'wb', buffering=0) as handle:
        handle.write(b'\0' * os.fstat(descriptor).st_size)
        os.fsync(descriptor)
    os.unlink(path)


def wipe_all():
    # Signals wait until the wipe is done: a handler running mid-wipe would exit before this path is removed.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, CLEANUP_SIGNALS)
    already = len(_left)
    try:
        for path in reversed(list(_to_delete)):
            try:
                wipe(path)
            except OSError:
                _left.append(path)  # keep going: one failure must not leave the others behind
            _to_delete.remove(path)
        new = _left[already:]
        if new:  # before the mask is restored: a pending signal's handler exits without returning here
            print(f'pw-migrate: could not remove {len(new)} file(s); delete them by hand: {" ".join(new)}',
                  file=sys.stderr)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def on_signal(signum, _frame):
    wipe_all()
    export = os.environ.get('EXPORT', '')
    state = 'kept' if export and os.path.lexists(export) else 'removed'
    fail(f'interrupted by signal {signum}; export {state}' + (f'; {PARTIAL}' if _import_started else ''), 130)


def linux_fs_type(descriptor):
    """Filesystem of an open file, by its mount ID: no path parsing (escaped names) and no re-resolution."""
    with open(f'/proc/self/fdinfo/{descriptor}', encoding='utf-8') as fdinfo:
        mount_id = next(line.split()[1] for line in fdinfo if line.startswith('mnt_id:'))
    with open('/proc/self/mountinfo', encoding='utf-8') as mountinfo:
        for line in mountinfo:
            if line.split()[0] == mount_id:
                return line.split(' - ', 1)[1].split()[0]
    return None


def macos_on_ramdisk(path):
    """True when the file's device is an attached `hdiutil attach ram://...` image."""
    device = subprocess.run(['df', path], capture_output=True, text=True, check=True).stdout.splitlines()[-1].split()[0]
    info = plistlib.loads(subprocess.run(['hdiutil', 'info', '-plist'], capture_output=True, check=True).stdout)
    for image in info.get('images', []):
        if str(image.get('image-path', '')).startswith('ram://'):
            for entity in image.get('system-entities', []):
                entry = entity.get('dev-entry', '')
                if entry and (device == entry or re.fullmatch(re.escape(entry) + r's\d+', device)):
                    return True
    return False


def check_export(path):
    if not path:
        fail('EXPORT is required: make pw-migrate EXPORT=/dev/shm/lastpass.csv', 2)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        fail(f'{path}: not found', 2)
    except OSError:
        fail(f'{path}: cannot open it as a regular file (symlinks are refused)', 2)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        fail(f'{path}: not a regular file', 2)
    system = platform.system()
    if system == 'Linux':
        fstype = linux_fs_type(descriptor)
        if fstype not in MEMORY_FS:
            fail(f'{path} is on {fstype or "an unknown filesystem"}, not tmpfs: move it to /dev/shm or '
                 '$XDG_RUNTIME_DIR first (it was not modified)', 2)
    elif system == 'Darwin':
        if not macos_on_ramdisk(path):
            fail(f'{path} is not on a RAM disk: see docs/password-migration.md (it was not modified)', 2)
    else:
        fail(f'unsupported system {system}', 2)
    os.fchmod(descriptor, 0o600)
    global _export_id
    info = os.fstat(descriptor)
    _export_id = (info.st_dev, info.st_ino)
    os.close(descriptor)


def read_rows(path):
    csv.field_size_limit(16 * 1024 * 1024)  # long secure notes exceed the 128 KiB default
    with os.fdopen(open_checked(path, os.O_RDONLY), newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        missing = LASTPASS_COLUMNS - set(reader.fieldnames or [])
        if missing:
            fail(f'not a LastPass CSV export: missing columns {sorted(missing)}')
        fields = reader.fieldnames
        rows = []
        for row in reader:
            if None in row.values() or None in row:  # missing or extra fields; never print the row
                fail(f'the entry ending on line {reader.line_num} of the export has the wrong number of fields; '
                     'nothing was imported')
            rows.append(row)
    if not rows:
        fail('the export has no entries; nothing to import')
    return fields, rows


def warn_disk_swap():
    """tmpfs pages and this process's memory can be swapped out; zram stays in RAM."""
    try:
        with open('/proc/swaps', encoding='utf-8') as swaps:
            disk = [line.split()[0] for line in swaps.readlines()[1:] if not line.startswith('/dev/zram')]
    except OSError:
        return
    if disk:
        print(f'  WARNING: disk swap is active ({", ".join(disk)}); the export can reach it unless it is encrypted. '
              'Consider `sudo swapoff -a` for the run.')


def note_type(row):
    """LastPass keeps cards and addresses as secure notes whose `extra` starts with NoteType:..."""
    first = (row['extra'] or '').split('\n', 1)[0].strip()
    return first[len('NoteType:'):].strip() if first.startswith('NoteType:') else ''


def counts(rows):
    groups = {row['grouping'] for row in rows if row['grouping'] and row['grouping'] != '(none)'}
    notes = [row for row in rows if row['url'] == SECURE_NOTE_URL]
    return {
        'items': len(rows),
        'logins': len(rows) - len(notes),
        'secure_notes': sum(not note_type(row) for row in notes),
        'payment_cards': sum(note_type(row) == 'Credit Card' for row in notes),
        'addresses': sum(note_type(row) == 'Address' for row in notes),
        'other_typed_notes': sum(note_type(row) not in ('', 'Credit Card', 'Address') for row in notes),
        'folders': len(groups),
        'shared_folder_items': sum(row['grouping'].startswith('Shared-') for row in rows),
        'totp': sum(bool(row['totp'].strip()) for row in rows),
        'attachments': 0,  # the CSV export carries none (docs/password-migration.md)
    }


def report(title, values):
    print(title)
    for key, value in values.items():
        print(f'  {key.replace("_", " ")}: {value}')


def strip_totp(path, fields, rows):
    stripped = path + '.no-totp'
    try:
        descriptor = os.open(stripped, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fail(f'{stripped} is left over from an earlier, killed run and holds plaintext: delete it, then retry')
    _to_delete.append(stripped)
    with os.fdopen(descriptor, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, 'totp': ''})
    return stripped


def bw(*args):
    """Run bw; return stdout. Its output may contain vault data, so only the exit status is ever shown."""
    command = shlex.split(os.environ.get('BW_CMD', 'bw')) + ['--nointeraction'] + list(args)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        fail(f'`bw {args[0]}` failed (exit {result.returncode}); its output is hidden because it may contain '
             'vault data. Check `bw status` (logged in and unlocked, BW_SESSION exported).'
             + (f' Note: {PARTIAL}.' if _import_started else ''))
    return result.stdout


def vault_counts():
    bw('sync')
    try:
        items = json.loads(bw('list', 'items', '--organizationid', 'null'))
        folders = json.loads(bw('list', 'folders'))
    except ValueError:
        fail('`bw list` returned something that is not JSON (output hidden)'
             + (f'. Note: {PARTIAL}.' if _import_started else ''))
    return {
        'items': len(items),
        'logins': sum(item.get('type') == 1 for item in items),
        'secure_notes': sum(item.get('type') == 2 for item in items),
        'cards': sum(item.get('type') == 3 for item in items),
        'identities': sum(item.get('type') == 4 for item in items),
        'folders': sum(folder.get('id') is not None for folder in folders),
        'totp': sum(bool((item.get('login') or {}).get('totp')) for item in items),
    }


def main():
    # No core dump can carry the rows to disk (systemd-coredump stores them); children (bw) inherit the limit.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    for signum in CLEANUP_SIGNALS:
        signal.signal(signum, on_signal)
    path = os.environ.get('EXPORT', '')
    check_export(path)
    dry_run = os.environ.get('DRY_RUN') == '1'
    if not (dry_run and os.environ.get('KEEP_EXPORT') == '1'):
        _to_delete.append(path)  # a declined import with KEEP_EXPORT=1 takes it back off
    try:
        fields, rows = read_rows(path)
        expected = counts(rows)
        report('LastPass export (counts only):', expected)
        if platform.system() == 'Linux':
            warn_disk_swap()
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
            try:
                answer = input(f'Import {expected["items"]} items into this Bitwarden vault? [y/N] ')
            except EOFError:
                answer = ''
            if answer.strip().lower() != 'y':
                if os.environ.get('KEEP_EXPORT') == '1' and path in _to_delete:
                    _to_delete.remove(path)
                print('Not imported.' + (' Export kept (KEEP_EXPORT=1).' if path not in _to_delete else ''))
                return
        source = strip_totp(path, fields, rows) if strip and expected['totp'] else path
        global _import_started
        _import_started = True
        bw('import', 'lastpasscsv', source)
        after = vault_counts()
        delta = {key: after[key] - before[key] for key in after}
        report('Imported (vault after minus before):', delta)
        # Bitwarden turns NoteType:Credit Card notes into cards and NoteType:Address into identities; other typed notes
        # (bank account, passport, ...) depend on server flags, so only totals, logins and TOTP must match exactly.
        want = {'items': expected['items'], 'logins': expected['logins'], 'totp': 0 if strip else expected['totp']}
        mismatch = {key: (delta[key], value) for key, value in want.items() if delta[key] != value}
        if delta['folders'] > expected['folders']:
            mismatch['folders'] = (delta['folders'], expected['folders'])
        if mismatch:
            fail('verify FAILED (got, expected): ' + json.dumps(mismatch) + '; review the vault before retrying, '
                 'a second import duplicates items')
        print('Verify OK: item, login and TOTP counts match; new folders do not exceed the export\'s '
              '(existing folders with the same name are reused). Cards, identities and notes are listed above.')
    finally:
        wipe_all()  # prints which files it could not remove
        if _left and sys.exc_info()[0] is None:
            sys.exit(1)  # only when no other exit (a signal's 130, a failure's 1) is already under way
        if path not in _to_delete and not os.path.exists(path):
            print('Export removed.')


if __name__ == '__main__':
    main()
