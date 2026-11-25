#!/usr/bin/env python3
"""Tests for roles/desktop/files/chrome-page-zoom.py (Chrome's default page zoom, desktop_chrome_page_zoom)."""
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / 'roles/desktop/files/chrome-page-zoom.py'
MAC_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'  # by its real path: a symlink trips its sandbox
CHROME = (shutil.which('google-chrome') or shutil.which('google-chrome-stable')
          or (MAC_CHROME if os.access(MAC_CHROME, os.X_OK) else None))


def level(percent):
    return math.log(percent / 100) / math.log(1.2)


class ChromePageZoomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_helper(self, *args):
        result = subprocess.run([sys.executable, '-I', str(HELPER), str(self.data), *args],
                                capture_output=True, text=True)
        return result.returncode, result.stdout.strip(), result.stderr

    def profile(self, name, prefs):
        (self.data / name).mkdir()
        path = self.data / name / 'Preferences'
        path.write_text(json.dumps(prefs))
        path.chmod(0o600)
        return path

    def zoom(self, path):
        return json.loads(path.read_text())['partition']['default_zoom_level']['x']

    def test_sets_every_profile_keeps_other_prefs_and_is_idempotent(self):
        default = self.profile('Default', {'browser': {'x': 1}, 'partition': {'per_host_zoom_levels': {'a': 1}}})
        other = self.profile('Profile 2', {'partition': {'default_zoom_level': {'x': 0.5, '123': 1.0}}})
        self.profile('System Profile', {})  # not a person's profile
        self.assertEqual(self.run_helper('80', '--check'), (0, 'changed', ''))
        self.assertNotIn('default_zoom_level', default.read_text())  # check mode writes nothing
        self.assertEqual(self.run_helper('80')[1], 'changed')
        for path in (default, other):
            self.assertAlmostEqual(self.zoom(path), level(80))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        prefs = json.loads(default.read_text())
        self.assertEqual(prefs['browser'], {'x': 1})
        self.assertEqual(prefs['partition']['per_host_zoom_levels'], {'a': 1})
        self.assertEqual(json.loads(other.read_text())['partition']['default_zoom_level']['123'], 1.0)
        self.assertNotIn('partition', json.loads((self.data / 'System Profile/Preferences').read_text()))
        self.assertEqual(self.run_helper('80')[1], 'unchanged')
        self.assertEqual(self.run_helper('100')[1], 'changed')
        self.assertAlmostEqual(self.zoom(default), 0.0)

    def test_never_writes_while_chrome_holds_the_profile(self):
        prefs = self.profile('Default', {})
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.data / 'SingletonLock')  # a live pid
        self.assertEqual(self.run_helper('80')[1], 'running')
        self.assertEqual(json.loads(prefs.read_text()), {})
        (self.data / 'SingletonLock').unlink()
        os.symlink('otherhost-1', self.data / 'SingletonLock')  # another host's lock (shared home)
        self.assertEqual(self.run_helper('80')[1], 'running')
        (self.data / 'SingletonLock').unlink()
        dead = subprocess.Popen(['true'])
        dead.wait()
        os.symlink(f'{socket.gethostname()}-{dead.pid}', self.data / 'SingletonLock')  # stale, from a crash
        self.assertEqual(self.run_helper('80')[1], 'changed')

    def test_a_profile_already_at_the_zoom_is_unchanged_even_while_chrome_runs(self):
        self.profile('Default', {'partition': {'default_zoom_level': {'x': level(90)}}})
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.data / 'SingletonLock')
        self.assertEqual(self.run_helper('90')[1], 'unchanged')

    def test_absent_and_bad_input(self):
        self.assertEqual(self.run_helper('80')[1], 'absent')
        self.assertEqual(subprocess.run([sys.executable, '-I', str(HELPER), str(self.data / 'nope'), '80'],
                                        capture_output=True, text=True).stdout.strip(), 'absent')
        self.profile('Default', {})
        (self.data / 'Default/Preferences').write_text('{not json')
        code, _, err = self.run_helper('80')
        self.assertEqual(code, 1)
        self.assertIn('cannot read', err)
        self.assertNotIn('Traceback', err)
        for bad in ('0', '80.5', 'abc', '501'):
            code, _, err = self.run_helper(bad)
            self.assertNotEqual(code, 0, bad)
            self.assertIn('percent must be', err)

    @unittest.skipUnless(CHROME, 'google-chrome not installed')
    def test_chrome_renders_at_the_zoom_the_helper_writes(self):
        # A real Chrome, headless, in a throwaway profile: page zoom multiplies devicePixelRatio
        url = 'data:text/html,<body><script>document.body.textContent="DPR="+devicePixelRatio</script></body>'

        def dpr():
            home = Path(self.tmp.name) / 'home'  # Chrome also writes Crash Reports, the NSS db and mimeapps under HOME
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                       XDG_CACHE_HOME=str(home / '.cache'), XDG_DATA_HOME=str(home / '.local/share'))
            out = subprocess.run([CHROME, f'--user-data-dir={self.data}', '--headless', '--no-first-run',
                                  '--disable-gpu', '--password-store=basic', '--use-mock-keychain',
                                  '--dump-dom', url], capture_output=True, text=True, timeout=120, env=env)
            match = re.search(r'DPR=([0-9.]+)', out.stdout)
            self.assertTrue(match, out.stderr[-500:])
            return float(match.group(1))

        self.assertAlmostEqual(dpr(), 1.0, places=3)  # also creates Default/Preferences
        self.assertEqual(self.run_helper('80')[1], 'changed')
        self.assertAlmostEqual(dpr(), 0.8, places=3)
        self.assertEqual(self.run_helper('80')[1], 'unchanged')  # Chrome kept the value when it rewrote the file


if __name__ == '__main__':
    unittest.main()
