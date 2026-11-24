#!/usr/bin/env python3
"""The drive-sync command's guards (roles/drive_sync, docs/drive-sync.md), with stub rclone and gitleaks."""
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))

# Stub rclone: logs every call; behaviour comes from STUB_* variables.
RCLONE = r'''#!/bin/bash
echo "$*" >> "$STUB_LOG"
while [[ $1 == --config || $1 == --ask-password=false ]]; do [[ $1 == --config ]] && shift; shift; done
cmd=$1; shift
target=""; for a in "$@"; do [[ $a == -* || $a == *.partial* || $a == "*"* || $a == /* && ! -e $a ]] && continue; target=$a; done
case $cmd in
  listremotes) [[ -n $STUB_CONF_BROKEN ]] && { echo "CRITICAL: failed to load config" >&2; exit 1; }; echo "drive-owner:" ;;
  about) [[ -n $STUB_NO_TOKEN ]] && exit 1; [[ " $* " == *" --json "* ]] && echo "{\"free\": ${STUB_FREE:-1000000}}" || echo "Free: 1 GiB" ;;
  lsf)
    for a in "$@"; do
      case $a in
        drive-*) [[ -n $STUB_REMOTE_ERR ]] && exit 5; [[ -n $STUB_REMOTE_MISSING ]] && exit 3
                 [[ -n $STUB_REMOTE_FILES ]] && echo "old.bin"
                 # files three levels down, which a depth-limited listing would not see
                 [[ -n $STUB_REMOTE_DEEP && " $* " != *" --max-depth "* ]] && echo "proj/run1/step.ckpt"
                 exit 0 ;;
        /*) [[ -d $a ]] && find "$a" -type f ! -name .gitleaksignore -printf '%P\n'; exit 0 ;;
      esac
    done ;;
  lsjson)
    if [[ " $* " == *" --stat "* ]]; then [[ " $* " == *.bin* ]] && echo '{"IsDir": false}' || echo '{"IsDir": true}'; exit 0; fi
    target=${@: -1}
    if [[ $target == drive-* ]]; then
      [[ -n $STUB_LIST_ERR ]] && exit 5
      [[ -n $STUB_REMOTE_FILES ]] && echo '[{"Path": "old.bin", "Size": 1}]' || echo '[]'
    else
      python3 -I -c 'import json, os, sys
r = sys.argv[1]
print(json.dumps([{"Path": os.path.relpath(os.path.join(d, n), r), "Size": os.path.getsize(os.path.join(d, n))}
                  for d, _, fs in os.walk(r) for n in fs if n != ".gitleaksignore"]))' "$target"
    fi ;;
  sync|copy|copyto) [[ -n $STUB_HOLD ]] && sleep "$STUB_HOLD"; exit 0 ;;
esac
'''


class DriveSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        script = cls.dir / 'drive-sync'
        play = [{'hosts': 'localhost', 'gather_facts': False,
                 'vars': {'drive_sync_config': '/tmp/conf', 'drive_sync_root': '@ROOT@', 'drive_sync_host': 'lap1',
                          'drive_sync_accounts': ['owner'], 'ansible_managed': 'test'},
                 'tasks': [{'name': 'Render', 'ansible.builtin.template': {
                     'src': str(ROOT / 'roles/drive_sync/templates/drive-sync.sh.j2'), 'dest': str(script),
                     'mode': '0755'}}]}]
        (cls.dir / 'play.yml').write_text(yaml.safe_dump(play))
        result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(cls.dir / 'play.yml')],
                                capture_output=True, text=True, timeout=180, env=dict(os.environ, ANSIBLE_NOCOLOR='1'))
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.template = script.read_text()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory()
        base = Path(self.case.name)
        self.root = base / 'drive'
        (self.root / 'owner').mkdir(parents=True)
        self.bin = base / 'bin'
        self.bin.mkdir()
        (self.bin / 'rclone').write_text(RCLONE)
        (self.bin / 'gitleaks').write_text('#!/bin/sh\necho "$*" >> "$STUB_LOG.gitleaks"\nexit ${STUB_GITLEAKS_RC:-0}\n')
        (self.bin / 'getent').write_text('#!/bin/sh\nexit 0\n')  # online
        for name in ('rclone', 'gitleaks', 'getent'):
            (self.bin / name).chmod(0o755)
        self.script = base / 'drive-sync'
        self.conf = base / 'drive-sync.conf'
        self.conf.write_text('[drive-owner]\ntype = drive\ntoken = present\n')
        self.script.write_text(self.template.replace("'@ROOT@'", str(self.root)).replace('@ROOT@', str(self.root))
                               .replace('CONF=/tmp/conf', f'CONF={self.conf}'))
        self.script.chmod(0o755)
        self.log = base / 'rclone.log'
        self.env = dict(os.environ, PATH=f'{self.bin}:/usr/bin:/bin', HOME=str(base), XDG_RUNTIME_DIR=str(base),
                        STUB_LOG=str(self.log))

    def tearDown(self):
        self.case.cleanup()

    def run_sync(self, *args, **stub):
        env = dict(self.env, **{f'STUB_{k.upper()}': str(v) for k, v in stub.items()})
        return subprocess.run([str(self.script), *args], capture_output=True, text=True, env=env, timeout=60)

    def calls(self, verb):
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if f' {verb} ' in f' {line} ']

    def test_push_uploads_with_the_filters_to_this_hosts_folder(self):
        (self.root / 'owner/model.bin').write_text('x')
        result = self.run_sync('push', 'owner')
        self.assertEqual(result.returncode, 0, result.stderr)
        sync = self.calls('sync')
        self.assertEqual(len(sync), 1)
        self.assertIn(f'{self.root}/owner drive-owner:laptop-drive/lap1', sync[0])
        for flag in ("/.pulled/**", "*.partial", "*.pem", "--drive-stop-on-upload-limit"):
            self.assertIn(flag, sync[0])

    def test_empty_tree_never_empties_a_non_empty_drive_copy(self):
        (self.root / 'owner/.gitleaksignore').write_text('')  # does not count as content
        result = self.run_sync('push', 'owner', remote_files=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to push an empty', result.stderr)
        self.assertIn('drive-sync pull owner lap1', result.stderr)
        self.assertEqual(self.calls('sync'), [])

    def test_empty_tree_never_empties_a_drive_copy_with_deep_files_only(self):
        result = self.run_sync('push', 'owner', remote_deep=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to push an empty', result.stderr)
        self.assertEqual(self.calls('sync'), [])

    def test_file_names_rclone_encodes_are_sized(self):
        (self.root / 'owner/tab\tname.bin').write_text('x' * 7)  # rclone shows the tab as a symbol
        result = self.run_sync('push', 'owner', free=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('needs 7', result.stderr)

    def test_file_names_with_spaces_are_sized(self):
        (self.root / 'owner/ notes.txt').write_text('x' * 10)
        result = self.run_sync('push', 'owner', free=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('needs 10', result.stderr)

    def test_timer_skips_an_account_not_set_up_yet_but_fails_on_a_refused_token(self):
        (self.root / 'owner/model.bin').write_text('x')
        self.conf.write_text('[drive-owner]\ntype = drive\n')  # no token yet
        result = self.run_sync('push', '--timer')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipping owner: not authorized yet', result.stdout)
        self.assertNotEqual(self.run_sync('push', 'owner').returncode, 0)  # a manual push still fails
        self.conf.write_text('[drive-owner]\ntype = drive\ntoken = present\n')  # a token Drive refuses
        result = self.run_sync('push', '--timer', no_token=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('revoked or expired', result.stderr)
        result = self.run_sync('push', '--timer', conf_broken=1)  # an unreadable config is not "not set up yet"
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot read', result.stderr)
        self.assertEqual(self.calls('sync'), [])

    def test_secret_finding_or_missing_gitleaks_stops_the_push(self):
        (self.root / 'owner/model.bin').write_text('x')
        result = self.run_sync('push', 'owner', gitleaks_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('.gitleaksignore', result.stderr)
        (self.bin / 'gitleaks').unlink()
        result = self.run_sync('push', 'owner')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('gitleaks not found', result.stderr)
        self.assertEqual(self.calls('sync'), [])

    def test_push_needing_more_than_the_free_space_is_refused(self):
        (self.root / 'owner/big.bin').write_bytes(b'x' * 5000)
        result = self.run_sync('push', 'owner', free=1000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bytes free but this push needs 5000', result.stderr)
        self.assertEqual(self.calls('sync'), [])

    def test_missing_rclone_is_named(self):
        empty = self.root.parent / 'nobin'
        empty.mkdir()
        result = subprocess.run(['/bin/bash', str(self.script), 'push', 'owner'], capture_output=True, text=True,
                                env=dict(self.env, PATH=str(empty)), timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn('rclone not found', result.stderr)

    def test_unauthorized_account_is_refused(self):
        (self.root / 'owner/model.bin').write_text('x')
        result = self.run_sync('push', 'owner', no_token=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('drive-sync auth owner', result.stderr)

    def test_timer_skips_while_a_push_runs(self):
        (self.root / 'owner/model.bin').write_text('x')
        env = dict(self.env, STUB_HOLD='3')
        first = subprocess.Popen([str(self.script), 'push', 'owner'], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(50):  # wait until the first push holds the lock and is uploading
                if self.calls('sync'):
                    break
                subprocess.run(['sleep', '0.1'], check=True)
            result = self.run_sync('push', '--timer')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('already running', result.stdout)
        finally:
            first.communicate(timeout=30)
        self.assertEqual(len(self.calls('sync')), 1)

    def test_listing_or_sizing_errors_fail_closed(self):
        (self.root / 'owner/model.bin').write_text('x')
        result = self.run_sync('push', 'owner', remote_err=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot list drive-owner', result.stderr)
        result = self.run_sync('push', 'owner', list_err=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot list drive-owner', result.stderr)
        self.assertEqual(self.calls('sync'), [])

    def test_first_push_sizes_the_whole_tree(self):
        (self.root / 'owner/a.bin').write_text('x' * 600)
        (self.root / 'owner/sub').mkdir()
        (self.root / 'owner/sub/b.bin').write_text('x' * 600)
        result = self.run_sync('push', 'owner', remote_missing=1, free=1000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('needs 1200', result.stderr)  # the whole tree

    def test_pull_goes_to_pulled_and_this_host_restores_into_the_root(self):
        result = self.run_sync('pull', 'owner', 'mac1/voice/run1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'drive-owner:laptop-drive/mac1/voice/run1 {self.root}/owner/.pulled/mac1/voice/run1',
                      self.calls('copy')[-1])
        result = self.run_sync('pull', 'owner', 'lap1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'drive-owner:laptop-drive/lap1 {self.root}/owner ', self.calls('copy')[-1] + ' ')
        self.assertIn('--ignore-existing', self.calls('copy')[-1])  # a restore never overwrites local files
        result = self.run_sync('pull', 'owner', 'mac1/voice/step-1.bin')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'drive-owner:laptop-drive/mac1/voice/step-1.bin {self.root}/owner/.pulled/mac1/voice/step-1.bin',
                      self.calls('copyto')[-1])
        for bad in ('../x', '/abs/y', 'mac1/../../etc'):
            self.assertNotEqual(self.run_sync('pull', 'owner', bad).returncode, 0, bad)


class RealToolTests(unittest.TestCase):
    """The push's rclone filters and its gitleaks rules, with the real tools (skipped where they are missing)."""

    def render(self, template, dest, root):
        play = [{'hosts': 'localhost', 'gather_facts': False,
                 'vars': {'drive_sync_config': '/tmp/conf', 'drive_sync_root': str(root), 'drive_sync_host': 'lap1',
                          'drive_sync_accounts': ['owner'], 'ansible_managed': 'test'},
                 'tasks': [{'name': 'Render', 'ansible.builtin.template': {
                     'src': str(ROOT / 'roles/drive_sync/templates' / template), 'dest': str(dest), 'mode': '0600'}}]}]
        (dest.parent / 'play.yml').write_text(yaml.safe_dump(play))
        result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(dest.parent / 'play.yml')],
                                capture_output=True, text=True, timeout=180, env=dict(os.environ, ANSIBLE_NOCOLOR='1'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def tree(self, base):
        files = {'proj/model.bin': 'x', '.pulled/mac1/a.bin': 'x', '.gitleaksignore': '', 'proj/ck.partial': 'x',
                 'proj/run.partial/a': 'x', 'proj/.env': 'x', 'proj/x.pem': 'x', 'proj/id_rsa': 'x',
                 'proj/id_mapping.csv': 'x',
                 'proj/sub/.pulled/kept.bin': 'x', 'proj/.DS_Store': 'x'}
        for name, text in files.items():
            (base / name).parent.mkdir(parents=True, exist_ok=True)
            (base / name).write_text(text)

    @unittest.skipUnless(shutil.which('rclone'), 'rclone required')
    def test_filters_push_exactly_the_intended_files(self):
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            self.render('drive-sync.sh.j2', base / 'drive-sync', base / 'drive')
            src = base / 'drive/owner'
            self.tree(src)
            filters = re.search(r'FILTERS=\((.*?)\)\n', (base / 'drive-sync').read_text(), re.S).group(1)
            result = subprocess.run(['bash', '-c', f'FILTERS=({filters}); rclone --config /dev/null lsf -R --files-only '
                                     f'"${{FILTERS[@]}}" "$1"', '_', str(src)], capture_output=True, text=True, check=True)
            self.assertEqual(sorted(result.stdout.split()),
                             ['proj/id_mapping.csv', 'proj/model.bin', 'proj/sub/.pulled/kept.bin'])

    @unittest.skipUnless(shutil.which('gitleaks'), 'gitleaks required')
    def test_gitleaks_rules_scan_what_is_pushed_and_skip_the_rest(self):
        token = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            root = base / 'drive'
            self.render('gitleaks.toml.j2', base / 'gitleaks.toml', root)
            src = root / 'owner'
            for skipped in ('.pulled/mac1/t.txt', 'proj/.env', 'proj/x.pem', 'proj/a.partial', 'proj/run.partial/t'):
                (src / skipped).parent.mkdir(parents=True, exist_ok=True)
                (src / skipped).write_text(f'token = "{token}"\n')
            scan = ['gitleaks', 'dir', str(src), '--config', str(base / 'gitleaks.toml'), '--no-banner', '--redact']
            self.assertEqual(subprocess.run(scan, capture_output=True, text=True).returncode, 0)
            (src / 'proj/sub/.pulled').mkdir(parents=True)  # nested .pulled is pushed, so it is scanned
            (src / 'proj/sub/.pulled/t.txt').write_text(f'token = "{token}"\n')
            self.assertEqual(subprocess.run(scan, capture_output=True, text=True).returncode, 1)


if __name__ == '__main__':
    unittest.main()
