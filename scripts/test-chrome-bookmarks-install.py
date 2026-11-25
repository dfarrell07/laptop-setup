#!/usr/bin/env python3
"""Tests for chrome-bookmarks-backup.py and chrome-bookmarks-install.py: fixtures, throwaway age keys, real Chrome."""
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / 'scripts/chrome-bookmarks-backup.py'
INSTALL = ROOT / 'scripts/chrome-bookmarks-install.py'
CHROME = shutil.which('google-chrome') or shutil.which('google-chrome-stable')
HAVE_AGE = shutil.which('age') and shutil.which('age-keygen')
TOKEN = 'https://svc.example.com/reset?token=SECRET123'  # left out by the export, kept by the backup
IDS = iter(range(100, 10 ** 6))


def url(n, u, **extra):
    return {'name': n, 'type': 'url', 'url': u, 'id': str(next(IDS)), **extra}


def folder(n, kids):
    return {'name': n, 'type': 'folder', 'children': kids, 'id': str(next(IDS))}


def bookmarks(bar, other=()):
    return {'version': 1, 'checksum': 'x', 'roots': {'bookmark_bar': folder('Bookmarks bar', list(bar)),
                                                       'other': folder('Other bookmarks', list(other)),
                                                       'synced': folder('Mobile bookmarks', [])}}


def urls_of(node, path=()):
    out = []
    if isinstance(node.get('url'), str):
        out.append((path, node['url']))
    for c in node.get('children', []):
        out += urls_of(c, path + ((c.get('name'),) if 'url' not in c else ()))
    return out


def keypair(path):
    subprocess.run(['age-keygen', '-o', str(path)], check=True, capture_output=True)
    return subprocess.run(['age-keygen', '-y', str(path)], check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(HAVE_AGE, 'age and age-keygen required')
class BackupInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key, self.key2 = self.root / 'key.txt', self.root / 'key2.txt'
        self.recipients = self.root / 'recipients'
        self.recipients.write_text(f'# test keys\n{keypair(self.key)}\n{keypair(self.key2)}\n')
        # the old machine: a personal profile with work links mixed in, a signed-out "redhat.com" profile, an empty one
        self.old = self.root / 'old'
        profiles = {'Default': ('me@gmail.com', 'Person 1'), 'Profile 1': ('', 'redhat.com'), 'Profile 2': ('', 'x')}
        for d in profiles:
            (self.old / d).mkdir(parents=True)
        (self.old / 'Local State').write_text(json.dumps({'profile': {'info_cache': {
            d: {'user_name': e, 'name': n} for d, (e, n) in profiles.items()}}}))
        typeless = {'name': 'No type', 'url': 'https://typeless.example/', 'id': '99'}
        (self.old / 'Default/Bookmarks').write_text(json.dumps(bookmarks(
            [url('Mail', 'https://mail.example.com/'), url('Mail twice', 'https://mail.example.com/'), url('Reset', TOKEN),
             folder('Team', [url('Jira', 'https://issues.redhat.com/x'), url('Bookmarklet', 'javascript:void(0)')]),
             folder('Team', [url('Jira', 'https://issues.redhat.com/x'), typeless]),
             folder('Other bookmarks', [url('Bar folder', 'https://bar-folder.example/')]),
             folder('Empty', [])],
            [url('Docs', 'https://docs.redhat.com/a'), url('News', 'https://news.example.org/')])))
        (self.old / 'Profile 1/Bookmarks').write_text(json.dumps(bookmarks([url('Repo', 'https://github.com/o/r')])))
        self.store = self.root / 'store'
        self.store.mkdir()
        self.archive = self.store / 'old.tar.age'
        # the new machine: work and personal profiles under other names, one with a link of its own
        self.new = self.root / 'new'
        for d in ('Default', 'Profile 3'):
            (self.new / d).mkdir(parents=True)
        (self.new / 'Local State').write_text(json.dumps({'profile': {'info_cache': {
            'Default': {'user_name': 'me@redhat.com'}, 'Profile 3': {'user_name': 'me@gmail.com'}}}}))
        (self.new / 'Default/Bookmarks').write_text(json.dumps(bookmarks([url('Own', 'https://own.example.net/')])))

    def tearDown(self):
        self.tmp.cleanup()

    def backup(self, *extra):
        return subprocess.run([sys.executable, '-I', str(BACKUP), '--machine', 'old', '--chrome-dir', str(self.old),
                               '--recipients', str(self.recipients), '--out', str(self.archive), *extra],
                              capture_output=True, text=True)

    def install(self, *extra, key=None):
        return subprocess.run([sys.executable, '-I', str(INSTALL), '--archive', str(self.archive), '--recipients',
                               str(self.recipients), '--identity', str(key or self.key), '--chrome-dir', str(self.new),
                               *extra], capture_output=True, text=True)

    def test_backup_is_complete_encrypted_to_both_keys_and_counts_only(self):
        result = self.backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sets: work 4, personal 7 = all 11 links, checked against the source on disk', result.stdout)
        self.assertIn('Profile 2: none, 0 links (0 unique); no bookmark file', result.stdout)  # listed, not missed
        for secret in ('SECRET123', 'mail.example', 'gmail', 'Person 1'):
            self.assertNotIn(secret, result.stdout + result.stderr)
        data = self.archive.read_bytes()
        self.assertTrue(data.startswith(b'age-encryption.org/v1'))
        self.assertNotIn(b'SECRET123', data)
        counts = json.loads((self.store / 'old.counts.json').read_text())
        self.assertEqual(set(counts), {'machine', 'created', 'archive_sha256', 'recipients', 'profiles', 'links', 'sets'})
        self.assertEqual((counts['profiles'], counts['links']), (3, 11))
        for key in (self.key, self.key2):  # each key alone opens it
            result = self.install('--verify-only', key=key)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('verified: 3 profiles, 11 links (work 4, personal 7)', result.stdout)
        self.assertIn('exists: --force', self.backup().stderr)  # no silent overwrite
        self.assertEqual(self.backup('--force').returncode, 0)

    def test_refusals_before_writing(self):
        self.assertIn('fewer than 3', self.backup('--min-recipients', '3').stderr)
        self.assertIn('--machine', subprocess.run([sys.executable, '-I', str(BACKUP), '--chrome-dir', str(self.old)],
                                                   capture_output=True, text=True, env={'PATH': os.environ['PATH']}).stderr)
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.old / 'SingletonLock')
        self.assertIn('Chrome is running', self.backup().stderr)
        (self.old / 'SingletonLock').unlink()
        broken = json.loads((self.old / 'Default/Bookmarks').read_text())
        broken['roots']['other']['children'].append({'name': 'odd', 'id': '5', 'url': 7, 'note': {'url': 'https://x/'}})
        (self.old / 'Default/Bookmarks').write_text(json.dumps(broken))  # a url field the walk would not read
        self.assertIn('url fields but', self.backup().stderr)
        self.assertFalse(self.archive.exists())
        self.recipients.write_text('ssh-ed25519 AAAA not-age\n')
        self.assertIn('not an age recipient', self.backup().stderr)

    def test_unparsable_urls_are_kept_and_bad_recipient_characters_refused(self):
        data = json.loads((self.old / 'Default/Bookmarks').read_text())
        data['roots']['other']['children'] += [url('V6', 'http://[::1'), url('Odd', 'https://a\u2100b.com/')]
        (self.old / 'Default/Bookmarks').write_text(json.dumps(data))
        result = self.backup()
        self.assertEqual(result.returncode, 0, result.stderr)  # kept (personal), no traceback
        self.assertIn('all 13 links', result.stdout)
        self.assertIn('sets: work 6, personal 7', result.stdout)  # unparsable URLs fail closed: work
        recips = self.recipients.read_text()
        self.recipients.write_text(recips + 'age1yubikey1qbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n')  # b is not bech32
        result = self.backup('--force')
        self.assertIn('not an age recipient line', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_archive_must_match_recipients_and_published_hash(self):
        self.assertEqual(self.backup().returncode, 0)
        other = self.root / 'other-recipients'
        other.write_text(self.recipients.read_text().splitlines()[1] + '\n')  # one key fewer than the archive has
        result = subprocess.run([sys.executable, '-I', str(INSTALL), '--archive', str(self.archive), '--recipients',
                                 str(other), '--identity', str(self.key), '--verify-only'], capture_output=True, text=True)
        self.assertIn('recipient stanzas', result.stderr)
        counts = self.store / 'old.counts.json'
        published = json.loads(counts.read_text())
        counts.write_text(json.dumps(dict(published, archive_sha256='0' * 64)))
        self.assertIn('does not match the SHA-256', self.install('--verify-only').stderr)

    def test_install_restores_every_link_in_place_idempotently(self):
        self.assertEqual(self.backup().returncode, 0)
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.new / 'SingletonLock')
        result = self.install()
        self.assertEqual(result.returncode, 2)
        self.assertIn('running', result.stdout)
        (self.new / 'SingletonLock').unlink()
        self.assertIn('work: profile Default: would add 4', self.install('--check').stdout)
        self.assertFalse((self.new / 'Profile 3/Bookmarks').exists())
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('personal: profile Profile 3: added 7, 0 already in place; every link verified in place',
                      result.stdout)
        work = json.loads((self.new / 'Default/Bookmarks').read_text())
        self.assertNotIn('checksum', work)
        bar = urls_of(work['roots']['bookmark_bar'])
        self.assertIn(((), 'https://own.example.net/'), bar)  # the profile's own link stays
        self.assertEqual(Counter(bar)[(('Team',), 'https://issues.redhat.com/x')], 2)  # two same-named folders
        self.assertEqual([c['name'] for c in work['roots']['bookmark_bar']['children']].count('Team'), 2)
        self.assertIn((('redhat.com (Profile 1)',), 'https://github.com/o/r'), bar)  # signed-out profile, own folder
        self.assertEqual(urls_of(work['roots']['other']), [((), 'https://docs.redhat.com/a')])
        personal = json.loads((self.new / 'Profile 3/Bookmarks').read_text())
        p_bar = urls_of(personal['roots']['bookmark_bar'])
        self.assertEqual(Counter(p_bar)[((), 'https://mail.example.com/')], 2)  # duplicates as in the source
        self.assertIn(((), TOKEN), p_bar)
        self.assertIn((('Team',), 'javascript:void(0)'), p_bar)
        self.assertIn((('Team',), 'https://typeless.example/'), p_bar)
        self.assertIn((('Other bookmarks',), 'https://bar-folder.example/'), p_bar)  # a bar folder, not the root
        self.assertIn('Empty', [c['name'] for c in personal['roots']['bookmark_bar']['children']])
        self.assertTrue(int(personal['roots']['bookmark_bar']['children'][0]['date_added']) > 13300000000000000)

        def all_ids(node):
            return [node['id']] + [i for c in node.get('children', []) for i in all_ids(c)]
        ids = [i for r in work['roots'].values() for i in all_ids(r)]
        self.assertEqual(len(ids), len(set(ids)))
        before = (self.new / 'Default/Bookmarks').read_text()
        result = self.install()
        self.assertIn('work: profile Default: added 0, 4 already in place', result.stdout)
        self.assertEqual((self.new / 'Default/Bookmarks').read_text(), before)
        kept = list((self.new / 'Default').glob('Bookmarks.laptop-setup-bak-*'))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].stat().st_mode & 0o777, 0o600)

    def test_nothing_is_removed_and_account_storage_is_not_counted(self):
        self.assertEqual(self.backup().returncode, 0)
        own = bookmarks([folder('Team', [url('Mine', 'https://mine.example/')])])  # an owner folder of the same name
        (self.new / 'Default/Bookmarks').write_text(json.dumps(own))
        (self.new / 'Default/AccountBookmarks').write_text(json.dumps(bookmarks([url('J', 'https://issues.redhat.com/x')])))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        team = urls_of(json.loads((self.new / 'Default/Bookmarks').read_text())['roots']['bookmark_bar'])
        self.assertIn((('Team',), 'https://mine.example/'), team)  # kept
        self.assertIn((('Team',), 'https://issues.redhat.com/x'), team)  # written although Sync's copy has it

    def test_extract_writes_the_raw_files(self):
        self.assertEqual(self.backup().returncode, 0)
        out = self.root / 'raw'
        result = self.install('--extract', str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((out / 'profiles/Default/Bookmarks').read_bytes(), (self.old / 'Default/Bookmarks').read_bytes())
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)

    def test_a_crafted_archive_with_extra_members_is_refused(self):
        import hashlib
        import importlib.util
        spec = importlib.util.spec_from_file_location('b', BACKUP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(self.backup().returncode, 0)
        out = self.root / 'raw-ok'
        self.assertEqual(self.install('--extract', str(out)).returncode, 0)
        members = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob('*') if p.is_file()}
        decrypted = subprocess.run(['age', '-d', '-i', str(self.key), str(self.archive)], capture_output=True, check=True)
        members = mod.read_tar(decrypted.stdout)
        members['profiles/../../planted.txt'] = b'planted'
        tar = mod.build_tar(members)
        recips = [l for l in self.recipients.read_text().splitlines() if l.startswith('age1')]
        subprocess.run(['age', '-e', '-o', str(self.archive)] + [a for r in recips for a in ('-r', r)], input=tar,
                       check=True)
        counts = self.store / 'old.counts.json'
        counts.write_text(json.dumps(dict(json.loads(counts.read_text()),
                                          archive_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())))
        result = self.install('--extract', str(self.root / 'raw-bad'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unexpected archive member', result.stderr)
        self.assertFalse((self.root / 'planted.txt').exists())
        counts.unlink()
        self.assertIn('counts.json is missing', self.install('--verify-only').stderr)

    def test_yubikey_tag_matches_a_real_stanza_and_names_are_checked(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('b', BACKUP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # a real public recipient and the tag age-plugin-yubikey wrote for it (piv-p256 dlgHCg ...)
        recipient = (ROOT / 'chrome-bookmarks/recipients').read_text().split('age1yubikey1', 1)
        if len(recipient) == 2:
            line = 'age1yubikey1' + recipient[1].split()[0]
            if line.startswith('age1yubikey1q2tzlcs2'):
                self.assertEqual(mod.recipient_tag(line), 'dlgHCg')
        self.assertIsNone(mod.recipient_tag('age1xyz'))
        self.assertIn('must end in .tar.age', self.backup('--out', str(self.store / 'old.bin')).stderr.replace(
            str(self.archive), ''))
        host = __import__('re').sub(r'[^a-z0-9-]+', '-', socket.gethostname().split('.')[0].lower())
        result = subprocess.run([sys.executable, '-I', str(BACKUP), '--machine', host, '--chrome-dir', str(self.old),
                                 '--recipients', str(self.recipients)], capture_output=True, text=True)
        self.assertIn("host's name", result.stderr)

    def fake_plugin(self, serials, pin_222='Once'):
        """A fake age-plugin-yubikey on PATH: keys by serial, each a throwaway X25519 key (recipient and identity)."""
        bindir = self.root / 'bin'
        bindir.mkdir(exist_ok=True)
        keys = {'111': self.key, '222': self.key2}
        listing = ''.join(f'#       Serial: {s}, Slot: 1\n#   PIN policy: {"Never" if s == "111" else pin_222}\n'
                          f'{subprocess.run(["age-keygen", "-y", str(keys[s])], capture_output=True, text=True).stdout}\n'
                          for s in serials)
        cases = ''.join(f'  *"--serial {s} "*) cat {keys[s]} ;;\n' for s in serials)
        script = bindir / 'age-plugin-yubikey'
        script.write_text(f'#!/bin/sh\ncase "$* " in\n  *--list-all*) printf %s "{listing}" ;;\n{cases}  *) exit 1 ;;\nesac\n')
        script.chmod(0o755)
        return dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}')

    def test_several_keys_plugged_in_each_named_by_serial(self):
        self.assertEqual(self.backup().returncode, 0)
        verify = [sys.executable, '-I', str(INSTALL), '--archive', str(self.archive), '--recipients',
                  str(self.recipients), '--verify-only']
        # no terminal here (a test, or an agent's background job): the key that asks for its PIN is left for one
        result = subprocess.run(verify, capture_output=True, text=True, env=self.fake_plugin(['222', '111']),
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('YubiKey 222: not checked here: it asks for its PIN, which needs a terminal', result.stdout)
        self.assertIn('YubiKey 111 opens it', result.stdout)
        self.assertIn('not checked: ', result.stdout)
        self.assertIn('YubiKey 111: touch it when it blinks', result.stderr)  # PIN policy Never: no PIN named
        # two touch-only keys: each opens it, touch-only order kept by serial
        result = subprocess.run(verify, capture_output=True, text=True, env=self.fake_plugin(['111', '222'], 'Never'),
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(result.stdout.index('YubiKey 111 opens it'), result.stdout.index('YubiKey 222 opens it'))
        self.assertIn('all 2 listed keys open the archive: safe to rely on', result.stdout)
        # one key failing (a missed touch) does not stop the others; the run fails at the end
        env = self.fake_plugin(['111', '222'], 'Never')
        plugin = self.root / 'bin/age-plugin-yubikey'
        bad = self.root / 'bad-key.txt'
        subprocess.run(['age-keygen', '-o', str(bad)], check=True, capture_output=True)
        plugin.write_text(plugin.read_text().replace(f'cat {self.key} ', f'cat {bad} '))
        result = subprocess.run(verify, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('YubiKey 111: failed', result.stdout)
        self.assertIn('YubiKey 222 opens it', result.stdout)
        self.assertNotIn('safe to rely on', result.stdout.replace('safe to rely on once', ''))
        env = self.fake_plugin(['111'])
        result = subprocess.run([sys.executable, '-I', str(INSTALL), '--archive', str(self.archive), '--recipients',
                                 str(self.recipients), '--verify-only'], capture_output=True, text=True, env=env)
        self.assertIn('not checked: ', result.stdout)
        # the keys listing names keys by serial and adds the missing one
        self.recipients.write_text(self.recipients.read_text().splitlines()[2] + '\n')  # only key 222 listed
        env = self.fake_plugin(['111', '222'])
        keys = [sys.executable, '-I', str(BACKUP), '--recipients', str(self.recipients), '--keys']
        result = subprocess.run(keys, capture_output=True, text=True, env=env)  # listing alone adds nothing
        self.assertIn('YubiKey 111 slot 1: NOT listed', result.stdout)
        self.assertIn('YubiKey 222 slot 1: listed', result.stdout)
        self.assertEqual(len([l for l in self.recipients.read_text().splitlines() if l.startswith('age1')]), 1)
        self.assertIn('no plugged-in YubiKey with serial 999',
                      subprocess.run(keys + ['--add', '999'], capture_output=True, text=True, env=env).stderr)
        result = subprocess.run(keys + ['--add', '111'], capture_output=True, text=True, env=env)
        self.assertIn('added YubiKey 111', result.stdout)
        self.assertEqual(len([l for l in self.recipients.read_text().splitlines() if l.startswith('age1')]), 2)

    def test_profiles_must_match_exactly_one_account(self):
        self.assertEqual(self.backup().returncode, 0)
        state = json.loads((self.new / 'Local State').read_text())
        state['profile']['info_cache']['Default']['user_name'] = 'me@example.com'
        (self.new / 'Local State').write_text(json.dumps(state))
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn('work: no Chrome profile(s) signed in to redhat.com', result.stdout)

    @unittest.skipUnless(CHROME, 'google-chrome not installed')
    def test_chrome_loads_the_restored_profile(self):
        self.assertEqual(self.backup().returncode, 0)
        home = self.root / 'home'
        env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                   XDG_CACHE_HOME=str(home / '.cache'), XDG_DATA_HOME=str(home / '.local/share'))
        real = self.root / 'real'
        chrome = [CHROME, f'--user-data-dir={real}', '--headless', '--no-first-run', '--disable-gpu',
                  '--password-store=basic', '--use-mock-keychain', '--dump-dom', 'data:text/html,<p>x</p>']
        subprocess.run(chrome, env=env, capture_output=True, timeout=120)
        state = json.loads((real / 'Local State').read_text())
        state.setdefault('profile', {}).setdefault('info_cache', {}).setdefault('Default', {})['user_name'] = 'a@gmail.com'
        (real / 'Local State').write_text(json.dumps(state))
        result = subprocess.run([sys.executable, '-I', str(INSTALL), '--archive', str(self.archive), '--recipients',
                                 str(self.recipients), '--identity', str(self.key), '--chrome-dir', str(real)],
                                capture_output=True, text=True)
        self.assertIn('personal: profile Default: added 7', result.stdout, result.stderr)
        subprocess.run(chrome, env=env, capture_output=True, timeout=120)
        data = json.loads((real / 'Default/Bookmarks').read_text())
        self.assertIn('checksum', data)  # Chrome loaded the restored file and rewrote it
        self.assertIn(((), TOKEN), urls_of(data['roots']['bookmark_bar']))


if __name__ == '__main__':
    unittest.main()
