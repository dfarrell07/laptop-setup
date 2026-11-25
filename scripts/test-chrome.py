#!/usr/bin/env python3
"""Tests for roles/desktop/files/chrome.py (docs/chrome.md): profile settings, page zoom, extension audit, policy check."""
import http.server
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / 'roles/desktop/files/chrome.py'
MAC_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'  # by its real path: a symlink trips its sandbox
CHROME = (shutil.which('google-chrome') or shutil.which('google-chrome-stable')
          or (MAC_CHROME if os.access(MAC_CHROME, os.X_OK) else None))
UBOL = 'ddkjiahejlhfcafbddmgiahcphecmpfh'
BITWARDEN = 'nngceckbapebfimnlniiiahkandclblb'
LASTPASS = 'hdokiejnpimakedhajhdlcegeplioahd'


def level(percent):
    return math.log(percent / 100) / math.log(1.2)


def consumer(user='owner@gmail.com'):
    return {'user_name': user, 'gaia_id': '1', 'hosted_domain': 'NO_HOSTED_DOMAIN', 'is_managed': 0}


WORK = {'user_name': 'owner@example.com', 'gaia_id': '2', 'hosted_domain': 'example.com', 'is_managed': 1}


class ChromeHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'chrome'
        self.data.mkdir()
        self.state = self.root / 'state'
        self.spec_file = self.root / 'chrome.json'
        self.spec = {'user_data_dir': str(self.data), 'state_dir': str(self.state), 'zoom_percent': None,
                     'personal_domains': ['gmail.com', 'googlemail.com'], 'policy_is_ours': False,
                     'policies': {}, 'policies_enforced': {}, 'color_scheme': '', 'site_settings': {},
                     'default_site_settings': {}, 'extensions': []}

    def tearDown(self):
        self.tmp.cleanup()

    def run_helper(self, *args, **spec):
        self.spec.update(spec)
        self.spec_file.write_text(json.dumps(self.spec))
        result = subprocess.run([sys.executable, '-I', str(HELPER), args[0], str(self.spec_file), *args[1:]],
                                capture_output=True, text=True)
        return result.returncode, result.stdout.strip(), result.stderr

    def out(self, *args, **spec):
        code, out, err = self.run_helper(*args, **spec)
        self.assertEqual(code, 0, err)
        return out

    def profile(self, name, prefs, info=None, secure=None):
        (self.data / name).mkdir()
        path = self.data / name / 'Preferences'
        path.write_text(json.dumps(prefs))
        path.chmod(0o600)
        if secure is not None:
            (self.data / name / 'Secure Preferences').write_text(json.dumps(secure))
        state_file = self.data / 'Local State'
        state = json.loads(state_file.read_text()) if state_file.exists() else {'profile': {'info_cache': {}}}
        if info is not None:
            state['profile']['info_cache'][name] = info
        state_file.write_text(json.dumps(state))
        return path

    def prefs(self, name):
        return json.loads((self.data / name / 'Preferences').read_text())

    # --- page zoom (every profile) ---
    def test_zoom_in_every_profile_keeps_other_prefs_and_is_idempotent(self):
        default = self.profile('Default', {'browser': {'x': 1}, 'partition': {'per_host_zoom_levels': {'a': 1}}}, WORK)
        other = self.profile('Profile 2', {'partition': {'default_zoom_level': {'x': 0.5, '123': 1.0}}})
        self.profile('System Profile', {})  # not a person's profile
        self.assertEqual(self.out('prefs', '--check', zoom_percent=80), 'changed')
        self.assertNotIn('default_zoom_level', default.read_text())  # check mode writes nothing
        self.assertEqual(self.out('prefs'), 'changed')
        for path in (default, other):
            self.assertAlmostEqual(json.loads(path.read_text())['partition']['default_zoom_level']['x'], level(80))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        prefs = json.loads(default.read_text())
        self.assertEqual(prefs['browser'], {'x': 1})
        self.assertEqual(prefs['partition']['per_host_zoom_levels'], {'a': 1})
        self.assertEqual(json.loads(other.read_text())['partition']['default_zoom_level']['123'], 1.0)
        self.assertNotIn('partition', json.loads((self.data / 'System Profile/Preferences').read_text()))
        self.assertEqual(self.out('prefs'), 'unchanged')
        self.assertEqual(self.out('prefs', zoom_percent=100), 'changed')
        self.assertAlmostEqual(json.loads(default.read_text())['partition']['default_zoom_level']['x'], 0.0)

    def test_never_writes_while_chrome_holds_the_profile(self):
        prefs = self.profile('Default', {})
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.data / 'SingletonLock')  # a live pid
        self.assertEqual(self.out('prefs', zoom_percent=80), 'running')
        self.assertEqual(json.loads(prefs.read_text()), {})
        (self.data / 'SingletonLock').unlink()
        os.symlink('otherhost-1', self.data / 'SingletonLock')  # another host's lock (shared home)
        self.assertEqual(self.out('prefs'), 'running')
        (self.data / 'SingletonLock').unlink()
        dead = subprocess.Popen(['true'])
        dead.wait()
        os.symlink(f'{socket.gethostname()}-{dead.pid}', self.data / 'SingletonLock')  # stale, from a crash
        self.assertEqual(self.out('prefs'), 'changed')

    def test_a_profile_already_set_is_unchanged_even_while_chrome_runs(self):
        self.profile('Default', {'partition': {'default_zoom_level': {'x': level(90)}}})
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.data / 'SingletonLock')
        self.assertEqual(self.out('prefs', zoom_percent=90), 'unchanged')

    def test_absent_and_bad_input(self):
        self.assertEqual(self.out('prefs', zoom_percent=80), 'absent')
        self.assertEqual(self.out('prefs', user_data_dir=str(self.root / 'nope')), 'absent')
        self.spec['user_data_dir'] = str(self.data)
        self.profile('Default', {})
        (self.data / 'Default/Preferences').write_text('{not json')
        code, _, err = self.run_helper('prefs')
        self.assertEqual(code, 1)
        self.assertIn('cannot read', err)
        self.assertNotIn('Traceback', err)
        for field, bad in (('zoom_percent', 0), ('zoom_percent', 80.5), ('zoom_percent', '80'), ('zoom_percent', 501),
                           ('color_scheme', 'blue'), ('extensions', [{'id': 'not-an-id'}]),
                           ('site_settings', {'file_system_write_guard': {'https://a.example': 'allow'}}),
                           ('site_settings', {'notifications': {'https://a.example': 'maybe'}}),
                           ('site_settings', {'notifications': {'a.example/path': 'allow'}}),
                           ('default_site_settings', {'popups': 'sometimes'})):
            spec = dict(self.spec, **{field: bad})
            self.spec_file.write_text(json.dumps(spec))
            result = subprocess.run([sys.executable, '-I', str(HELPER), 'prefs', str(self.spec_file)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, (field, bad))
            self.assertNotIn('Traceback', result.stderr, (field, bad))

    # --- personal profiles ---
    def test_personal_settings_only_in_consumer_profiles_of_the_listed_domains(self):
        self.profile('Default', {}, WORK)
        self.profile('Profile 1', {}, consumer())
        self.profile('Profile 2', {}, consumer('owner@googlemail.com'))
        self.profile('Profile 3', {}, consumer('owner@xgmail.com'))  # a look-alike domain
        self.profile('Profile 4', {}, dict(consumer(), hosted_domain='gmail.com'))  # a Workspace account
        self.profile('Profile 5', {}, dict(consumer(), gaia_id=''))  # signed out
        self.profile('Profile 6', {}, {'user_name': '', 'gaia_id': ''})  # never signed in
        self.profile('Profile 7', {}, dict(consumer(), is_managed=1))  # accepted management
        self.profile('Profile 8', {}, {'user_name': 'owner@gmail.com'})  # keys missing: not personal
        self.assertEqual(self.out('prefs', color_scheme='dark',
                                  site_settings={'notifications': {'https://app.slack.com': 'allow',
                                                                   'https://www.reddit.com': 'block'},
                                                 'popups': {'[*.]example.org': 'allow'}},
                                  default_site_settings={'geolocation': 'block'}), 'changed')
        for name in ('Profile 1', 'Profile 2'):
            prefs = self.prefs(name)
            self.assertEqual(prefs['browser']['theme']['color_scheme2'], 2)
            exceptions = prefs['profile']['content_settings']['exceptions']
            self.assertEqual(exceptions['notifications'], {'https://app.slack.com:443,*': {'setting': 1},
                                                           'https://www.reddit.com:443,*': {'setting': 2}})
            self.assertEqual(exceptions['popups'], {'[*.]example.org,*': {'setting': 1}})
            self.assertEqual(prefs['profile']['default_content_setting_values'], {'geolocation': 2})
        for name in ('Default', 'Profile 3', 'Profile 4', 'Profile 5', 'Profile 6', 'Profile 7', 'Profile 8'):
            self.assertEqual(self.prefs(name), {}, name)
        self.assertEqual(self.out('prefs'), 'unchanged')

    def test_a_site_dropped_from_the_settings_is_removed_and_others_are_kept(self):
        self.profile('Profile 1', {'profile': {'content_settings': {'exceptions': {'notifications': {
            'https://kept.example:443,*': {'setting': 1, 'last_modified': '1'}}}}}}, consumer())
        sites = {'notifications': {'https://a.example': 'allow', 'https://b.example:8443': 'block'}}
        self.assertEqual(self.out('prefs', site_settings=sites), 'changed')
        self.assertEqual(self.out('prefs', site_settings={'notifications': {'https://a.example': 'allow'}}),
                         'changed')
        notes = self.prefs('Profile 1')['profile']['content_settings']['exceptions']['notifications']
        self.assertEqual(notes, {'https://kept.example:443,*': {'setting': 1, 'last_modified': '1'},
                                 'https://a.example:443,*': {'setting': 1}})
        self.assertEqual(self.out('prefs'), 'unchanged')
        self.assertEqual((self.state / 'chrome-site-settings.json').stat().st_mode & 0o777, 0o600)

    def test_policies_become_preferences_only_where_machine_policy_is_not_ours(self):
        self.profile('Default', {}, WORK)
        self.profile('Profile 1', {'credentials_enable_service': True}, consumer())
        policies = {'PasswordManagerEnabled': False, 'BlockThirdPartyCookies': True, 'SafeBrowsingProtectionLevel': 2,
                    'BookmarkBarEnabled': True, 'HttpsOnlyMode': 'force_balanced_enabled'}
        self.assertEqual(self.out('prefs', policies=policies, policy_is_ours=True), 'unchanged')
        self.assertEqual(self.out('prefs', policy_is_ours=False), 'changed')
        prefs = self.prefs('Profile 1')
        self.assertIs(prefs['credentials_enable_service'], False)
        self.assertEqual(prefs['profile']['cookie_controls_mode'], 1)
        self.assertEqual(prefs['safebrowsing'], {'enabled': True, 'enhanced': True})
        self.assertIs(prefs['bookmark_bar']['show_on_all_tabs'], True)
        self.assertEqual(self.prefs('Default'), {})  # the work profile never gets them

    # --- extensions ---
    def test_extension_audit(self):
        exts = [{'id': UBOL, 'name': 'uBlock Origin Lite'}, {'id': BITWARDEN, 'name': 'Bitwarden'},
                {'id': 'ghbmnnjooekpmoecnnnilnnbdlolhkhi', 'name': 'Docs Offline', 'install': False}]
        self.assertEqual(self.out('extensions', extensions=exts), 'absent')
        self.profile('Default', {'extensions': {'settings': {LASTPASS: {'location': 1, 'from_webstore': True}}}}, WORK)
        self.profile('Profile 1', {'extensions': {'settings': {
            BITWARDEN: {'location': 1, 'from_webstore': True, 'disable_reasons': [1]},
            'ghbmnnjooekpmoecnnnilnnbdlolhkhi': {'location': 1, 'from_webstore': True},
            'gphhapmejobijbbhgpjhcjognlahblep': {'location': 6, 'from_webstore': True}}}},  # external: not ours to judge
            consumer(), secure={'extensions': {'settings': {
                LASTPASS: {'location': 1, 'from_webstore': True, 'manifest': {'name': 'LastPass'}}}}})
        self.assertEqual(self.out('extensions').splitlines(), [
            f'Profile 1: missing {UBOL} uBlock Origin Lite',
            f'Profile 1: disabled {BITWARDEN} Bitwarden',
            f'Profile 1: undeclared {LASTPASS} LastPass'])
        self.profile('Profile 2', {'extensions': {'settings': {
            UBOL: {'location': 1, 'from_webstore': True, 'disable_reasons': []},
            BITWARDEN: {'location': 6, 'state': 1}}}}, consumer('owner@googlemail.com'))
        self.assertNotIn('Profile 2', self.out('extensions'))
        self.profile('Profile 3', {'extensions': {'settings': {
            UBOL: {'location': 1, 'from_webstore': True}, BITWARDEN: {'location': 1, 'from_webstore': True}}}},
            dict(consumer(), is_consented_primary_account=True))
        self.assertIn('Profile 3: sync-on', self.out('extensions'))

    def test_open_lists_the_store_pages_of_missing_extensions(self):
        self.profile('Profile 1', {}, consumer())
        env = {k: v for k, v in os.environ.items() if k not in ('DISPLAY', 'WAYLAND_DISPLAY')}
        self.spec_file.write_text(json.dumps(dict(self.spec, extensions=[{'id': UBOL, 'name': 'uBOL'}])))
        out = subprocess.run([sys.executable, '-I', str(HELPER), 'open', str(self.spec_file)],
                             capture_output=True, text=True, env=env).stdout.strip()
        if sys.platform != 'darwin':  # macOS would open Chrome
            self.assertEqual(out, f'Profile 1: https://chromewebstore.google.com/detail/{UBOL}')

    def test_only_what_the_helper_wrote_is_removed_again(self):
        # A site the user had already set in Chrome is never recorded, so dropping it from the settings leaves it.
        self.profile('Profile 1', {'profile': {'content_settings': {'exceptions': {'popups': {
            'https://user.example:443,*': {'setting': 1}, 'https://other.example:443,*': {'setting': 2}}}}}},
            consumer())
        self.assertEqual(self.out('prefs', site_settings={'popups': {'https://user.example': 'allow',
                                                                     'https://other.example': 'allow'}},
                                  default_site_settings={'geolocation': 'block'}), 'changed')
        backup = json.loads((self.data / 'Profile 1' / 'Preferences.laptop-setup-bak').read_text())
        self.assertNotIn('default_content_setting_values', backup.get('profile', {}))  # as it was before
        self.assertEqual(self.out('prefs', site_settings={}, default_site_settings={}), 'changed')
        prefs = self.prefs('Profile 1')
        popups = prefs['profile']['content_settings']['exceptions']['popups']
        self.assertEqual(popups['https://user.example:443,*'], {'setting': 1})
        self.assertEqual(popups['https://other.example:443,*'], {'setting': 1})  # changed, but the user's: kept
        self.assertNotIn('geolocation', prefs['profile']['default_content_setting_values'])  # ours: removed
        self.assertEqual(self.out('prefs'), 'unchanged')

    def test_a_profile_signed_out_for_a_while_keeps_its_record(self):
        self.profile('Profile 1', {}, consumer())
        self.assertEqual(self.out('prefs', site_settings={'popups': {'https://a.example': 'allow'}}), 'changed')
        state = json.loads((self.data / 'Local State').read_text())
        state['profile']['info_cache']['Profile 1']['gaia_id'] = ''  # signed out
        (self.data / 'Local State').write_text(json.dumps(state))
        self.assertEqual(self.out('prefs', site_settings={}), 'unchanged')
        state['profile']['info_cache']['Profile 1']['gaia_id'] = '1'  # signed in again
        (self.data / 'Local State').write_text(json.dumps(state))
        self.assertEqual(self.out('prefs'), 'changed')
        self.assertEqual(self.prefs('Profile 1')['profile']['content_settings']['exceptions']['popups'], {})

    def test_spellings_of_one_site_and_typed_policy_values(self):
        self.profile('Profile 1', {}, consumer())
        self.assertEqual(self.out('prefs', site_settings={'popups': {'HTTPS://A.Example': 'allow',
                                                                     'https://a.example:443/': 'allow'}}), 'changed')
        for field, bad in (('site_settings', {'popups': {'https://a.example': 'allow',
                                                         'https://a.example:443': 'block'}}),
                           ('site_settings', {'popups': None}),
                           ('site_settings', {'popups': {'https://a.example:0': 'allow'}}),
                           ('extensions', ['uBOL']), ('default_site_settings', {'popups': ['allow']}),
                           ('policies', {'SafeBrowsingProtectionLevel': 'enhanced'}),
                           ('policies', {'BlockThirdPartyCookies': 'false'}),
                           ('policies', {'SafeBrowsingProtectionLevel': True})):
            code, _, err = self.run_helper('prefs', **{field: bad})
            self.assertEqual(code, 1, (field, bad))
            self.assertIn('chrome.py:', err, (field, bad))
            self.assertNotIn('Traceback', err, (field, bad))
            self.spec.update({'site_settings': {}, 'extensions': [], 'default_site_settings': {}, 'policies': {}})

    @unittest.skipIf(os.geteuid() == 0, 'root reads anything')
    def test_an_unreadable_data_dir_is_blocked_not_a_traceback(self):
        self.profile('Profile 1', {}, consumer())
        prefs = self.data / 'Profile 1' / 'Preferences'
        prefs.chmod(0)
        try:
            self.assertEqual(self.out('prefs', color_scheme='dark'), 'blocked')
            self.assertEqual(self.out('extensions', extensions=[{'id': UBOL}]), 'blocked')
        finally:
            prefs.chmod(0o600)

    def test_the_spec_can_come_on_stdin_as_the_role_passes_it(self):
        self.profile('Default', {})
        self.spec['zoom_percent'] = 90
        result = subprocess.run([sys.executable, '-I', '-c', HELPER.read_text(), 'prefs', '-', '--check'],
                                input=json.dumps(self.spec), capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'changed'), result.stderr)

    def test_extensions_that_machine_policy_blocks_are_not_missing(self):
        policies = self.root / 'policies'
        (policies / 'managed').mkdir(parents=True)
        (policies / 'managed' / 'it.json').write_text(json.dumps({
            'ExtensionInstallBlocklist': ['*'], 'ExtensionInstallAllowlist': [BITWARDEN],
            'ExtensionSettings': {f'{LASTPASS},{UBOL}': {'installation_mode': 'blocked'}}}))
        self.profile('Profile 1', {}, consumer())
        exts = [{'id': UBOL}, {'id': BITWARDEN}, {'id': 'fcoeoabgfenejglbffodgkkbkcdhcgfn'}]
        out = self.out('extensions', extensions=exts, policy_dir=str(policies))
        if sys.platform.startswith('linux'):
            self.assertEqual(out, f'Profile 1: missing {BITWARDEN}')  # the others are blocked by the wildcard or by ID

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux policy files')
    def test_policy_check_compares_our_files_only(self):
        policies = self.root / 'policies'
        (policies / 'recommended').mkdir(parents=True)
        want = {'BlockThirdPartyCookies': True}
        self.assertEqual(self.out('policy', policy_dir=str(policies), policies=want, policy_is_ours=False), 'ok')
        self.assertIn('differs', self.out('policy', policy_is_ours=True))
        (policies / 'recommended' / 'laptop-setup.json').write_text(json.dumps(want))
        (policies / 'recommended' / 'it.json').write_text('{"X": 1}')  # another party's: not ours to judge
        self.assertEqual(self.out('policy'), 'ok')
        self.assertIn('differs', self.out('policy', policy_is_ours=False))  # ours must go where policy is not ours

    @unittest.skipUnless(sys.platform.startswith('linux'), 'starts google-chrome on Linux')
    def test_open_does_not_wait_for_chrome(self):
        fake = self.root / 'fake-chrome'
        fake.write_text('#!/bin/sh\nsleep 5\n')
        fake.chmod(0o755)
        self.profile('Profile 1', {}, consumer())
        self.spec_file.write_text(json.dumps(dict(self.spec, chrome_binary=str(fake),
                                                  extensions=[{'id': UBOL}, {'id': BITWARDEN}])))
        import time
        start = time.monotonic()
        out = subprocess.run([sys.executable, '-I', str(HELPER), 'open', str(self.spec_file)], capture_output=True,
                             text=True, env=dict(os.environ, DISPLAY=':99'), timeout=30).stdout
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(len(out.splitlines()), 2)

    def test_ask_only_where_chromium_has_it_and_a_bad_local_state_still_zooms(self):
        self.profile('Default', {})
        for field, bad in (('default_site_settings', {'popups': 'ask'}),
                           ('site_settings', {'javascript': {'https://a.example': 'ask'}})):
            code, _, err = self.run_helper('prefs', **{field: bad})
            self.assertEqual(code, 1, field)
            self.assertIn('allow or block only', err)
            self.spec.update({'site_settings': {}, 'default_site_settings': {}})
        self.assertEqual(self.out('prefs', default_site_settings={'notifications': 'ask'}), 'unchanged')  # no personal
        for field, bad in (('default_site_settings', {'cookies': 'ask'}),
                           ('site_settings', {'cookies': {'https://a.example': 'ask'}})):
            code, _, err = self.run_helper('prefs', **{field: bad})
            self.assertEqual(code, 1, field)
            self.assertIn('allow or block only', err)
            self.spec.update({'site_settings': {}, 'default_site_settings': {}})
        (self.data / 'Local State').write_text('{broken')
        code, out, err = self.run_helper('prefs', zoom_percent=80)
        self.assertEqual((code, out), (0, 'changed\npersonal-unknown'), err)
        self.assertIn('cannot read', err)

    def test_an_unreadable_local_state_still_zooms_writes_nothing_personal_and_says_so(self):
        if os.geteuid() == 0:
            self.skipTest('root reads a mode 0 file')
        self.profile('Profile 1', {}, consumer())
        self.profile('Default', {}, {'user_name': 'me@example.com', 'gaia_id': '2', 'hosted_domain': 'example.com'})
        local_state = self.data / 'Local State'
        local_state.chmod(0)
        try:
            code, out, err = self.run_helper('prefs', zoom_percent=80, color_scheme='dark',
                                             site_settings={'javascript': {'https://a.example': 'block'}})
            self.assertEqual((code, out), (0, 'changed\npersonal-unknown'), err)
            self.assertEqual(self.out('extensions', extensions=[{'id': UBOL}]), 'blocked')
        finally:
            local_state.chmod(0o600)
        for name in ('Profile 1', 'Default'):
            prefs = self.prefs(name)
            self.assertIn('default_zoom_level', json.dumps(prefs), name)
            self.assertNotIn('theme', prefs.get('browser', {}), name)
            self.assertNotIn('content_settings', prefs.get('profile', {}), name)
        self.assertEqual(self.out('prefs', zoom_percent=80, color_scheme='dark',
                                  site_settings={'javascript': {'https://a.example': 'block'}}), 'changed')
        self.assertEqual(self.prefs('Profile 1')['browser']['theme']['color_scheme2'], 2)

    # --- a real Chrome reads what the helper writes ---
    def chrome(self, url):
        home = self.root / 'home'  # Chrome also writes Crash Reports, the NSS db and mimeapps under HOME
        env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                   XDG_CACHE_HOME=str(home / '.cache'), XDG_DATA_HOME=str(home / '.local/share'))
        out = subprocess.run([CHROME, f'--user-data-dir={self.data}', '--headless', '--no-first-run',
                              '--disable-gpu', '--password-store=basic', '--use-mock-keychain', '--dump-dom', url],
                             capture_output=True, text=True, timeout=120, env=env)
        return out

    @unittest.skipUnless(CHROME, 'google-chrome not installed')
    def test_chrome_renders_at_the_zoom_the_helper_writes(self):
        # page zoom multiplies devicePixelRatio
        url = 'data:text/html,<body><script>document.body.textContent="DPR="+devicePixelRatio</script></body>'

        def dpr():
            out = self.chrome(url)
            match = re.search(r'DPR=([0-9.]+)', out.stdout)
            self.assertTrue(match, out.stderr[-500:])
            return float(match.group(1))

        self.assertAlmostEqual(dpr(), 1.0, places=3)  # also creates Default/Preferences
        self.assertEqual(self.out('prefs', zoom_percent=80), 'changed')
        self.assertAlmostEqual(dpr(), 0.8, places=3)
        self.assertEqual(self.out('prefs'), 'unchanged')  # Chrome kept the value when it rewrote the file

    @unittest.skipUnless(CHROME, 'google-chrome not installed')
    def test_chrome_applies_the_site_settings_the_helper_writes(self):
        # JavaScript blocked by default, allowed for one origin: a script runs only on that origin. The profile is made
        # personal by its Local State entry, as a signed-in consumer account would be.
        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'<body>static<script>document.body.textContent="JS-RAN"</script></body>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        servers = [http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page) for _ in range(2)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        allowed, other = (f'http://127.0.0.1:{s.server_address[1]}' for s in servers)
        def ran(url):  # the script replaces the body's text "static"
            out = self.chrome(url)
            self.assertIn('<body>', out.stdout, out.stderr[-500:])
            return 'static' not in out.stdout

        self.assertTrue(ran(allowed + '/'))  # creates Default
        state = json.loads((self.data / 'Local State').read_text())
        state.setdefault('profile', {}).setdefault('info_cache', {}).setdefault('Default', {}).update(consumer())
        (self.data / 'Local State').write_text(json.dumps(state))
        self.assertEqual(self.out('prefs', default_site_settings={'javascript': 'block'},
                                  site_settings={'javascript': {allowed: 'allow'}}, color_scheme='dark'), 'changed')
        self.assertTrue(ran(allowed + '/'))
        self.assertFalse(ran(other + '/'))
        self.assertEqual(self.out('prefs'), 'unchanged')  # Chrome kept every value when it rewrote the file



class SmokeAloneTests(unittest.TestCase):
    @unittest.skipUnless(os.path.exists('/opt/google/chrome/chrome') or os.path.exists('/Applications/Google Chrome.app'),
                         'Chrome not installed')
    def test_a_copy_of_smoke_test_run_alone_still_reports(self):
        """Molecule's script module runs a copy without the repo beside it: the Chrome checks must not end the run."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            (home / '.config/laptop-setup').mkdir(parents=True)
            (home / '.config/laptop-setup/chrome.json').write_text(json.dumps({'user_data_dir': str(home / 'none')}))
            shutil.copy(ROOT / 'scripts/smoke-test.sh', Path(tmp) / 'smoke-test.sh')
            out = subprocess.run(['bash', str(Path(tmp) / 'smoke-test.sh'), '--only',
                                  'chrome-profiles,chrome-extensions,chrome-policy'],
                                 capture_output=True, text=True, timeout=300, env=dict(os.environ, HOME=str(home)))
        self.assertIn('chrome-policy', out.stdout, out.stderr[-500:])
        self.assertIn('helper missing', out.stdout)

    def test_chunk_enabled_reads_rollout_enable(self):
        source = (ROOT / 'scripts/smoke-test.sh').read_text()
        start = source.index('_chunk_enabled() {')
        end = source.index('\n}\n', start) + 3
        cases = [(None, 'yes'), ('profile: work\n', 'yes'), ('rollout_enable: all\n', 'yes'),
                 ("rollout_enable: 'all'  # every chunk\n", 'yes'),
                 ('rollout_enable: [packages, chrome]\n', 'yes'), ('rollout_enable: [packages, desktop]\n', 'no'),
                 ('rollout_enable: [packages, chromebook]\n', 'no'),
                 ('rollout_enable:\n  - packages\n  - "chrome"  # yes\nother: 1\n', 'yes'),
                 ('rollout_enable:\n  - packages\n  - desktop\nother: [chrome]\n', 'no'),
                 ('rollout_enable:\n  - packages\n', 'no'),
                 ('rollout_enable: [desktop,\n  chrome]\n', 'yes'),
                 ('rollout_enable: [\n  desktop,  # comment\n  chrome,\n]\nother: 1\n', 'yes'),
                 ('rollout_enable:\n  [desktop, chrome]\n', 'yes'),
                 ('rollout_enable: [\n  desktop,\n]\nother: [chrome]\n', 'no'),
                 ('rollout_enable:\n  [desktop]\nother:\n  - chrome\n', 'no')]
        for config, want in cases:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as tmp:
                cfg = Path(tmp) / 'config.yml'
                if config is not None:
                    cfg.write_text(config)
                out = subprocess.run(['bash', '-c', f'_cfg={cfg}\n' + source[start:end]
                                      + '\nif _chunk_enabled chrome; then echo yes; else echo no; fi'],
                                     capture_output=True, text=True).stdout.strip()
                self.assertEqual(out, want)

class ShippedTaskTests(unittest.TestCase):
    """roles/desktop/tasks/chrome.yml: nothing machine-wide unless this machine's Chrome policy is ours."""

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.tasks = yaml.safe_load((ROOT / 'roles/desktop/tasks/chrome.yml').read_text())
        cls.defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())

    def walk(self, tasks):
        for task in tasks:
            yield task
            yield from self.walk(task.get('block', []))

    def task(self, name):
        return next(t for t in self.walk(self.tasks) if t.get('name') == name)

    def test_policy_is_ours_only_off_csb_and_without_other_policy_files(self):
        decide = self.task("Decide whether this machine's Chrome policy is ours")
        ours = decide['ansible.builtin.set_fact']['_chrome_policy_ours']
        self.assertIn("not (csb_detected | bool)", ours)
        self.assertIn('_chrome_foreign_linux | length == 0', ours)
        self.assertIn("stat.exists", ours)  # a macOS configuration profile for Chrome
        self.assertIn("reject('match', '^/etc/opt/chrome/policies/(managed|recommended)/laptop-setup",
                      decide['vars']['_chrome_foreign_linux'])

    @unittest.skipUnless((ROOT / '.venv/bin/ansible-playbook').exists(), 'no dev venv (make bootstrap)')
    def test_the_ownership_decision_evaluated_by_ansible(self):
        """The shipped Jinja, run by Ansible on found-file lists: our own files only, anything else is foreign."""
        import yaml
        decide = self.task("Decide whether this machine's Chrome policy is ours")
        base = '/etc/opt/chrome/policies'
        cases = [  # (files, csb_detected, skipped, ours, own); profile personal unless the case says work
            ([], False, {}, True, 0),
            ([], 'work', {}, False, 0),  # a work machine nothing else manages (yet): policy would reach its work profile
            ([f'{base}/managed/laptop-setup.json', f'{base}/recommended/laptop-setup.json'], False, {}, True, 2),
            ([f'{base}/managed/laptop-setup.json', f'{base}/managed/it.json'], False, {}, False, 1),
            ([f'{base}/managed/.hidden'], False, {}, False, 0),
            ([f'{base}/enrollment/CloudManagementEnrollmentToken'], False, {}, False, 0),
            ([f'{base}/managed/laptop-setupXjson'], False, {}, False, 0),
            ([], True, {}, False, 0),
            ([], False, {f'{base}/managed': 'Permission denied'}, False, 0),
        ]
        tasks = []
        for i, (files, csb, skipped, _, _) in enumerate(cases):
            tasks.append({'name': f'case {i}', 'ansible.builtin.set_fact': decide['ansible.builtin.set_fact'],
                          'vars': dict(decide['vars'], csb_detected=csb is True, is_linux=True, is_macos=False,
                                       profile='work' if csb == 'work' else 'personal',
                                       _chrome_manage=True, ansible_facts={'user_dir': '/home/x'},
                                       _chrome_policy_files={'files': [{'path': f, 'isdir': False} for f in files],
                                                             'skipped_paths': skipped})})
            tasks.append({'ansible.builtin.debug': {'msg': f'CASE {i} {{{{ _chrome_policy_ours | bool }}}} '
                                                           f'{{{{ _chrome_own_linux | length }}}}'}})
        with tempfile.TemporaryDirectory() as tmp:
            play = Path(tmp) / 'p.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                                             'tasks': tasks}]))
            out = subprocess.run([str(ROOT / '.venv/bin/ansible-playbook'), str(play)], capture_output=True,
                                 text=True, timeout=300, env=dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default'))
        self.assertEqual(out.returncode, 0, out.stdout[-2000:])
        for i, (_, _, _, ours, own) in enumerate(cases):
            self.assertIn(f'CASE {i} {ours} {own}', out.stdout, i)

    @unittest.skipUnless((ROOT / '.venv/bin/ansible-playbook').exists(), 'no dev venv (make bootstrap)')
    def test_external_extension_files_skip_package_owned_ones_and_bad_state(self):
        """The shipped Jinja: write the declared IDs, remove only ones recorded before, never a package's file."""
        import base64
        import yaml
        block = self.task('Chrome external extensions')
        removal = self.task('Remove Chrome external extension files no longer declared')
        gnome, old = 'gphhapmejobijbbhgpjhcjognlahblep', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        env = dict(os.environ, ANSIBLE_STDOUT_CALLBACK='default')
        before = base64.b64encode(json.dumps([old, gnome, UBOL, '../../etc/x', 7]).encode()).decode()
        variables = dict(block['vars'], is_macos=False, is_linux=True, _chrome_policy_ours=True,
                         _chrome_state_dir='/home/x/.config/laptop-setup',
                         _chrome_extensions=[{'id': UBOL}, {'id': gnome}, {'id': BITWARDEN, 'install': False}],
                         _chrome_ext_before={'content': before},
                         _chrome_ext_owned={'stdout_lines': [f'owned /usr/share/google-chrome/extensions/{gnome}.json',
                                                             f'exists /usr/share/google-chrome/extensions/{BITWARDEN}.json',
                                                             f'exists /usr/share/google-chrome/extensions/{UBOL}.json']})
        tasks = [{'ansible.builtin.debug': {'msg': 'WANT={{ _chrome_ext_want | join(",") }}='}},
                 {'ansible.builtin.debug': {'msg': 'REMOVE={{ ' + removal['loop'][3:-3] + ' | join(",") }}='}}]
        with tempfile.TemporaryDirectory() as tmp:
            play = Path(tmp) / 'p.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                                             'vars': variables, 'tasks': tasks}]))
            out = subprocess.run([str(ROOT / '.venv/bin/ansible-playbook'), str(play)], capture_output=True,
                                 text=True, timeout=300, env=env).stdout
        self.assertIn(f'WANT={UBOL}=', out)
        self.assertIn(f'REMOVE={old}=', out)
        variables.update(_chrome_policy_ours=False)  # ownership lost: nothing new, nothing declared removed
        with tempfile.TemporaryDirectory() as tmp:
            play = Path(tmp) / 'p.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
                                             'vars': variables, 'tasks': tasks}]))
            out = subprocess.run([str(ROOT / '.venv/bin/ansible-playbook'), str(play)], capture_output=True,
                                 text=True, timeout=300, env=env).stdout
        self.assertIn('WANT==', out)
        self.assertIn(f'REMOVE={old}=', out)  # dropped from the list: still removed

    def test_package_ownership_probe_fails_closed(self):
        """The shell probe: unowned by rpm and dpkg -> exists; owned by either, or a database error -> owned (never
        touched); a dangling symlink counts as present; with neither tool (macOS) every present file is "exists"."""
        import re as _re
        probe = self.task('Find Chrome external extension files present or a package\'s')
        self.assertNotIn('when', probe)  # macOS too
        self.assertEqual(probe['environment'], {'LC_ALL': 'C'})
        script = _re.search(r"\['sh', '-c', '(.*?)', 'sh'\]", probe['ansible.builtin.command']['argv'], _re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'bin').mkdir()
            (tmp / 'none').mkdir()
            for name in ('mine', 'pkg', 'deb', 'broken', 'debbroken'):
                (tmp / f'{name}.json').write_text('{}')
            (tmp / 'link.json').symlink_to(tmp / 'nowhere')
            (tmp / 'bin/rpm').write_text('#!/bin/sh\ncase "$2" in\n'
                                         '  *pkg*) echo pkg-1.0; exit 0;;\n'
                                         '  *broken*) echo "error: rpmdb open failed"; exit 1;;\n'
                                         '  *) echo "file $2 is not owned by any package"; exit 1;;\nesac\n')
            (tmp / 'bin/dpkg').write_text('#!/bin/sh\ncase "$2" in\n'
                                          '  *deb*) echo "deb: $2"; exit 0;;\n'
                                          '  *) echo "dpkg-query: no path found matching pattern $2"; exit 1;;\nesac\n')
            for tool in ('rpm', 'dpkg'):
                (tmp / f'bin/{tool}').chmod(0o755)
            names = ('mine', 'pkg', 'deb', 'broken', 'link', 'absent')

            def probe_with(path):
                out = subprocess.run(['/bin/sh', '-c', script, 'sh', *(str(tmp / f'{n}.json') for n in names)],
                                     capture_output=True, text=True, env=dict(os.environ, PATH=path)).stdout
                return [line.split(' ')[0] + ' ' + Path(line.split(' ', 1)[1]).name for line in out.split('\n') if line]
            self.assertEqual(probe_with(f"{tmp / 'bin'}:/usr/bin:/bin"),
                             ['exists mine.json', 'owned pkg.json', 'owned deb.json', 'owned broken.json',
                              'exists link.json'])
            self.assertEqual(probe_with(str(tmp / 'none')),
                             ['exists mine.json', 'exists pkg.json', 'exists deb.json', 'exists broken.json',
                              'exists link.json'])

    def test_every_write_under_etc_or_library_needs_our_policy(self):
        self.assertIn("profile | default('work') == 'personal'",
                      self.task("Decide whether this machine's Chrome policy is ours")['ansible.builtin.set_fact']
                      ['_chrome_policy_ours'])
        for name in ('Create the Chrome policy directories', 'Write the Chrome policy files'):
            self.assertIn('_chrome_policy_ours', self.task(name)['when'], name)
        removal = self.task('Remove Chrome policy files that are no longer wanted')
        self.assertEqual(removal['loop'], '{{ _chrome_own_linux }}')  # only our own file name
        external = self.task('Chrome external extensions')
        self.assertIn('if _chrome_policy_ours | bool else []', external['vars']['_chrome_ext_want_all'])
        mac = self.task('Chrome policies (macOS)')
        self.assertIn('if _chrome_policy_ours | bool else {}', mac['vars']['_chrome_mac_policies'])
        self.assertIn("rejectattr('key', 'equalto', 'RestoreOnStartup')", mac['vars']['_chrome_mac_policies'])

    def test_shipped_extension_ids_are_web_store_ids_and_the_reasons_are_documented(self):
        doc = (ROOT / 'docs/chrome.md').read_text()
        for ext in self.defaults['desktop_chrome_extensions']:
            self.assertRegex(ext['id'], r'^[a-p]{32}$')
            self.assertIn(f"`{ext['id']}`", doc, ext['name'])

    def test_shipped_policies_have_preference_equivalents_or_are_documented_as_policy_only(self):
        import importlib.util
        loader = importlib.util.spec_from_file_location('chrome_helper', HELPER)
        helper = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(helper)
        doc = (ROOT / 'docs/chrome.md').read_text()
        for name in self.defaults['desktop_chrome_policies_recommended']:
            if name not in helper.POLICY_PREFS:
                self.assertIn(f'others (`{name}`)', doc, name)


if __name__ == '__main__':
    unittest.main()
