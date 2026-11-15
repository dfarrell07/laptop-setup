#!/usr/bin/env python3
"""pw-migrate: counts only, nothing secret on the terminal, export always removed, verify catches mismatches.

Uses a fake LastPass vault on /dev/shm and a fake `bw` (BW_CMD); never a real vault or account.
"""
import csv
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts/pw-migrate.py'
SHM = Path('/dev/shm')
SECRETS = ['hunter2-secret', 'JBSWY3DPEHPK3PXP', 'note-body-secret', 'alice@example.test', '4111111111111111',
           '1 Secret Street']
FAKE_BW = r'''
import csv, json, os, sys
state_path = os.environ['FAKE_BW_STATE']
state = json.load(open(state_path)) if os.path.exists(state_path) else {'items': [], 'folders': []}
mode = os.environ.get('FAKE_BW_MODE', '')
args = sys.argv[1:]
assert args[0] == '--nointeraction'  # never prompts on the captured stdout
args = args[1:]
if args[0] == 'sync':
    print('Syncing complete.')
elif args[:2] == ['list', 'items']:
    print(json.dumps(state['items']))
elif args[:2] == ['list', 'folders']:
    print(json.dumps([{'id': None, 'name': 'No Folder'}] + state['folders']))
elif args[:2] == ['import', 'lastpasscsv']:
    if mode == 'fail-import':
        print('Import error on row: hunter2-secret', file=sys.stderr)
        sys.exit(1)
    rows = list(csv.DictReader(open(args[2], encoding='utf-8-sig')))
    if mode == 'drop-one':
        rows = rows[1:]
    names = {f['name'] for f in state['folders']}
    for row in rows:
        if row['grouping'] and row['grouping'] != '(none)' and row['grouping'] not in names:
            names.add(row['grouping'])
            state['folders'].append({'id': 'f' + str(len(names)), 'name': row['grouping']})
        note = row['url'] == 'http://sn'
        kind = row['extra'].split('\n', 1)[0] if note else ''
        item_type = {'NoteType:Credit Card': 3, 'NoteType:Address': 4}.get(kind, 2) if note else 1
        state['items'].append({'type': item_type, 'name': row['name'],
                               'login': None if note else {'password': row['password'], 'totp': row['totp'] or None}})
    json.dump(state, open(state_path, 'w'))
    print('Imported ' + str(len(rows)) + ' items: hunter2-secret')
json.dump(state, open(state_path, 'w'))
'''
ROWS = [
    {'url': 'https://a.example.test', 'username': 'alice@example.test', 'password': 'hunter2-secret',
     'totp': 'JBSWY3DPEHPK3PXP', 'extra': '', 'name': 'A', 'grouping': 'Work', 'fav': '0'},
    {'url': 'https://b.example.test', 'username': 'bob', 'password': 'pw2', 'totp': '', 'extra': '', 'name': 'B',
     'grouping': 'Shared-Team', 'fav': '1'},
    {'url': 'http://sn', 'username': '', 'password': '', 'totp': '', 'extra': 'note-body-secret', 'name': 'N',
     'grouping': 'Work', 'fav': '0'},
    {'url': 'https://c.example.test', 'username': 'c', 'password': 'pw3', 'totp': '', 'extra': '', 'name': 'C',
     'grouping': '', 'fav': '0'},
    {'url': 'http://sn', 'username': '', 'password': '', 'totp': '', 'name': 'K', 'grouping': '(none)', 'fav': '0',
     'extra': 'NoteType:Credit Card\nNumber:4111111111111111'},
    {'url': 'http://sn', 'username': '', 'password': '', 'totp': '', 'name': 'H', 'grouping': 'Home', 'fav': '0',
     'extra': 'NoteType:Address\nAddress 1:1 Secret Street\nCity:X'},
]


@unittest.skipUnless(SHM.is_dir() and sys.platform.startswith('linux'), 'needs Linux /dev/shm (tmpfs)')
class PwMigrateTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix='pw-migrate-test-')
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        (self.work / 'fakebw.py').write_text(FAKE_BW)
        handle = tempfile.NamedTemporaryFile('w', dir=SHM, prefix='pw-migrate-test-', suffix='.csv', delete=False,
                                             newline='', encoding='utf-8-sig')  # LastPass exports may carry a BOM
        writer = csv.DictWriter(handle, fieldnames=list(ROWS[0]))
        writer.writeheader()
        writer.writerows(ROWS)
        handle.close()
        self.export = Path(handle.name)
        self.addCleanup(lambda: self.export.exists() and self.export.unlink())

    def run_migrate(self, **env):
        environment = {**os.environ, 'EXPORT': str(self.export), 'ASSUME_YES': '1',
                       'BW_CMD': f'{sys.executable} {self.work / "fakebw.py"}',
                       'FAKE_BW_STATE': str(self.work / 'state.json'), **env}
        result = subprocess.run([sys.executable, str(SCRIPT)], env=environment, capture_output=True, text=True,
                                timeout=60)
        for secret in SECRETS:
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def test_dry_run_reports_counts_only_and_removes_the_export(self):
        result = self.run_migrate(DRY_RUN='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ['items: 6', 'logins: 3', 'secure notes: 1', 'payment cards: 1', 'addresses: 1', 'folders: 3',
                     'shared folder items: 1', 'totp: 1', 'attachments: 0']:
            self.assertIn(line, result.stdout)
        self.assertFalse(self.export.exists())
        self.assertFalse((self.work / 'state.json').exists())  # bw never called (it writes state on every call)

    def test_dry_run_can_keep_the_export(self):
        self.assertEqual(self.run_migrate(DRY_RUN='1', KEEP_EXPORT='1').returncode, 0)
        self.assertTrue(self.export.exists())
        self.assertEqual(self.export.stat().st_mode & 0o777, 0o600)

    def test_refuses_an_export_on_disk_and_leaves_it_alone(self):
        disk_dir = tempfile.TemporaryDirectory(prefix='pw-migrate-test-', dir=Path.home())  # /tmp may be tmpfs
        self.addCleanup(disk_dir.cleanup)
        on_disk = Path(disk_dir.name) / 'export.csv'
        on_disk.write_text(self.export.read_text())
        result = self.run_migrate(EXPORT=str(on_disk))
        self.assertEqual(result.returncode, 2)
        self.assertIn('not tmpfs', result.stderr)
        self.assertTrue(on_disk.exists())

    def test_refuses_a_symlink(self):
        link = SHM / (self.export.name + '.link')
        link.symlink_to(self.export)
        self.addCleanup(link.unlink)
        self.assertEqual(self.run_migrate(EXPORT=str(link)).returncode, 2)
        self.assertTrue(self.export.exists())

    def test_import_strips_totp_verifies_and_removes_every_copy(self):
        result = self.run_migrate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Verify OK', result.stdout)
        state = json.loads((self.work / 'state.json').read_text())
        self.assertEqual(len(state['items']), 6)
        self.assertEqual(sorted(i['type'] for i in state['items']), [1, 1, 1, 2, 3, 4])
        self.assertFalse([i for i in state['items'] if (i.get('login') or {}).get('totp')])
        self.assertFalse(self.export.exists())
        self.assertFalse(Path(str(self.export) + '.no-totp').exists())

    def test_import_can_keep_totp(self):
        result = self.run_migrate(STRIP_TOTP='0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('totp: 1', result.stdout.split('Imported')[1])

    def test_failed_import_hides_bw_output_and_removes_the_export(self):
        result = self.run_migrate(FAKE_BW_MODE='fail-import')
        self.assertEqual(result.returncode, 1)
        self.assertIn('output is hidden', result.stderr)
        self.assertFalse(self.export.exists())

    def test_verify_catches_a_short_import(self):
        result = self.run_migrate(FAKE_BW_MODE='drop-one')
        self.assertEqual(result.returncode, 1)
        self.assertIn('verify FAILED', result.stderr)
        self.assertFalse(self.export.exists())

    def test_declined_confirmation_imports_nothing(self):
        environment = dict(os.environ, EXPORT=str(self.export), BW_CMD=f'{sys.executable} {self.work / "fakebw.py"}',
                           FAKE_BW_STATE=str(self.work / 'state.json'))
        result = subprocess.run([sys.executable, str(SCRIPT)], env=environment, input='n\n', capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn('Not imported', result.stdout)
        self.assertFalse(self.export.exists())

    def test_empty_export_fails_before_bw_and_is_removed(self):
        self.export.write_text('url,username,password,totp,extra,name,grouping,fav\n')
        result = self.run_migrate()
        self.assertEqual(result.returncode, 1)
        self.assertIn('no entries', result.stderr)
        self.assertFalse(self.export.exists())
        self.assertFalse((self.work / 'state.json').exists())

    def test_hangup_at_the_prompt_removes_the_export(self):
        environment = dict(os.environ, EXPORT=str(self.export), BW_CMD=f'{sys.executable} {self.work / "fakebw.py"}',
                           FAKE_BW_STATE=str(self.work / 'state.json'))
        process = subprocess.Popen([sys.executable, str(SCRIPT)], env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 30
        while not (self.work / 'state.json').exists() and time.monotonic() < deadline:
            time.sleep(0.1)  # bw list ran: the script is at the confirmation prompt
        time.sleep(0.5)
        process.send_signal(signal.SIGHUP)
        out, err = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(self.export.exists())
        for secret in SECRETS:
            self.assertNotIn(secret, out + err)

    def test_declined_confirmation_keeps_the_export_when_asked(self):
        environment = dict(os.environ, EXPORT=str(self.export), BW_CMD=f'{sys.executable} {self.work / "fakebw.py"}',
                           FAKE_BW_STATE=str(self.work / 'state.json'), KEEP_EXPORT='1')
        result = subprocess.run([sys.executable, str(SCRIPT)], env=environment, input='n\n', capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn('Export kept', result.stdout)
        self.assertTrue(self.export.exists())


if __name__ == '__main__':
    unittest.main()
