#!/usr/bin/env python3
"""make chrome-bookmarks-export: Chrome profiles' Bookmarks files to local_bookmarks.yml (docs/chrome-bookmarks.md)."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXPORT = ROOT / 'scripts/chrome-bookmarks-export.py'
RENDER = ROOT / 'scripts/chrome-bookmarks.py'


def url(name, link):
    return {'type': 'url', 'name': name, 'url': link}


def folder(name, *children):
    return {'type': 'folder', 'name': name, 'children': list(children)}


class ChromeBookmarksExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.chrome = self.dir / 'chrome'
        self.chrome.mkdir()
        self.out = self.dir / 'local_bookmarks.yml'

    def profile(self, folder_name, display, roots):
        (self.chrome / folder_name).mkdir()
        (self.chrome / folder_name / 'Bookmarks').write_text(json.dumps({'roots': roots}))
        state = self.chrome / 'Local State'
        data = json.loads(state.read_text()) if state.exists() else {'profile': {'info_cache': {}}}
        data['profile']['info_cache'][folder_name] = {'name': display, 'user_name': 'secret@example.com'}
        state.write_text(json.dumps(data))

    def export(self):
        return subprocess.run([sys.executable, '-I', str(EXPORT), '--chrome-dir', str(self.chrome),
                               '--output', str(self.out)], capture_output=True, text=True)

    def test_profiles_are_exported_in_a_form_make_chrome_bookmarks_accepts(self):
        deep = url('Deep', 'https://deep.example/')
        for level in range(7):
            deep = folder(f'L{level}', deep)
        self.profile('Default', 'Work', {
            'bookmark_bar': folder('Bookmarks bar', url('Tracker', 'https://tracker.example/x?a=1'),
                                   folder('Docs', url('Run "book"', 'https://docs.example/')), deep,
                                   url('JS', 'javascript:alert(1)'), url('Creds', 'https://u:p@example.com/')),
            'other': folder('Other', url('Other one', 'http://other.example/')),
            'synced': folder('Mobile', )})
        self.profile('Profile 1', 'Work', {'bookmark_bar': folder('Bar', url('Mail', 'https://mail.example/'))})
        self.profile('Profile 2', 'Empty one', {'bookmark_bar': folder('Bar', url('Local', 'file:///etc/passwd'))})
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o600)
        for private in ('tracker.example', 'Tracker', 'secret@example.com', 'mail.example'):
            self.assertNotIn(private, result.stdout + result.stderr)  # counts only
        self.assertIn('2 not http(s) or with credentials left out', result.stdout)
        self.assertIn('Profile 2: no http(s) bookmarks, skipped', result.stdout)
        data = yaml.safe_load(self.out.read_text())['chrome_bookmarks']
        self.assertEqual(list(data), ['work', 'work-2'])
        work = data['work']
        self.assertEqual(work[0], {'name': 'Tracker', 'url': 'https://tracker.example/x?a=1'})
        self.assertEqual(work[1], {'folder': 'Docs', 'items': [{'name': 'Run "book"', 'url': 'https://docs.example/'}]})
        self.assertEqual(work[-1], {'folder': 'Other bookmarks',
                                    'items': [{'name': 'Other one', 'url': 'http://other.example/'}]})
        self.assertIn('Deep', self.out.read_text())  # moved up from below the depth limit, not dropped
        self.assertNotIn('secret@example.com', self.out.read_text())
        render = subprocess.run([sys.executable, '-I', str(RENDER), '--source', str(self.out),
                                 '--output-dir', str(self.dir / 'html')], capture_output=True, text=True)
        self.assertEqual(render.returncode, 0, render.stderr)

    def test_line_separator_titles_are_cleaned_not_fatal(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('ex', ROOT / 'scripts/chrome-bookmarks-export.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for title in ('Line\u2028sep', 'Para\u2029sep'):
            cleaned = mod.clean_name(title, 'x')
            self.assertNotIn('\u2028', cleaned)
            self.assertNotIn('\u2029', cleaned)
            mod.render.check_name(cleaned, 'p')  # what the export writes, the renderer accepts

    def test_previous_files_are_kept_and_nothing_is_written_without_bookmarks(self):
        self.out.write_text('curated\n')
        self.profile('Default', 'Personal', {'bookmark_bar': folder('Bar', url('A', 'https://a.example/'))})
        self.assertEqual(self.export().returncode, 0)
        self.assertEqual(self.export().returncode, 0)  # a second run keeps the first backup too
        backups = sorted(self.dir.glob('local_bookmarks.yml.bak-*'))
        self.assertEqual(len(backups), 2)
        self.assertEqual(backups[0].read_text(), 'curated\n')
        self.assertEqual(list(self.dir.glob('.local_bookmarks.yml.*')), [])  # no temporary file left
        empty = self.dir / 'empty'
        empty.mkdir()
        result = subprocess.run([sys.executable, '-I', str(EXPORT), '--chrome-dir', str(empty), '--output',
                                 str(self.dir / 'none.yml')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.dir / 'none.yml').exists())
        for name in ('local_bookmarks.yml', 'local_bookmarks.yml.bak-20261125-120000-000001',
                     '.local_bookmarks.yml.k3j2x1'):  # the temporary file a killed run can leave
            ignored = subprocess.run(['git', 'check-ignore', '-q', name], cwd=ROOT, capture_output=True)
            self.assertEqual(ignored.returncode, 0, name)  # the export and its backups never reach git

    def test_account_bookmarks_malformed_input_and_the_entry_limit(self):
        self.profile('Default', 'Signed in', {})  # account storage only
        (self.chrome / 'Default/Bookmarks').unlink()
        (self.chrome / 'Default/AccountBookmarks').write_text(json.dumps(
            {'roots': {'bookmark_bar': folder('Bar', url('Acct', 'https://acct.example/'))}}))
        self.profile('Profile 6', 'Half', {})  # local store truncated, account store fine
        (self.chrome / 'Profile 6/Bookmarks').write_text('{"roots": {"bookmark_bar"')
        (self.chrome / 'Profile 6/AccountBookmarks').write_text(json.dumps(
            {'roots': {'bookmark_bar': folder('Bar', url('Kept', 'https://kept.example/'))}}))
        self.profile('Profile 7', 'Twice', {'bookmark_bar': folder(  # one file, one link in two folders: both kept
            'Bar', folder('Home', url('Same', 'https://twice.example/')),
            folder('Work', url('Same', 'https://twice.example/')))})
        self.profile('Profile 5', 'Both', {'bookmark_bar': folder('Bar', url('Same', 'https://same.example/')),
                                           'other': folder('O', url('Local other', 'https://lo.example/'))})
        (self.chrome / 'Profile 5/AccountBookmarks').write_text(json.dumps({'roots': {
            'bookmark_bar': folder('Bar', url('Same', 'https://same.example/')),
            'other': folder('O', url('Account other', 'https://ao.example/'))}}))
        (self.chrome / 'Profile 2').mkdir()
        (self.chrome / 'Profile 2/Bookmarks').write_text(json.dumps({'roots': ['not', 'a', 'dict']}))
        deep = url('Bottom', 'https://bottom.example/')
        for _ in range(1500):
            deep = folder('d', deep)
        (self.chrome / 'Profile 10').mkdir()
        (self.chrome / 'Profile 10/Bookmarks').write_text(json.dumps({'roots': {'bookmark_bar': folder('Bar', deep)}}))
        many = [url(f'L{i}', f'https://l{i}.example/') for i in range(1990)]
        many.append(folder('Last', *[url(f'F{i}', f'https://f{i}.example/') for i in range(30)]))
        self.profile('Profile 3', 'Big', {'bookmark_bar': folder('Bar', *many)})
        full = [url(f'M{i}', f'https://m{i}.example/') for i in range(2000)]
        full.append(folder('Refused', *[url(f'R{i}', f'https://r{i}.example/') for i in range(30)]))
        self.profile('Profile 4', 'Full', {'bookmark_bar': folder('Bar', *full),
                                           'other': folder('O', url('Late', 'https://late.example/'))})
        state = json.loads((self.chrome / 'Local State').read_text())
        state['profile']['info_cache']['Profile 10'] = {'name': None}
        (self.chrome / 'Local State').write_text(json.dumps(state))
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('Profile 2: no http(s) bookmarks, skipped (1 unreadable bookmarks file left out)', result.stdout)
        data = yaml.safe_load(self.out.read_text())['chrome_bookmarks']
        self.assertEqual(data['signed-in'], [{'name': 'Acct', 'url': 'https://acct.example/'}])
        self.assertNotIn('bottom', json.dumps(data))  # beyond the nesting bound: not read, not a crash
        self.assertIn('Profile 10: no http(s) bookmarks, skipped (1 nested too deep left out)', result.stdout)
        # a link in both files once, and one "Other bookmarks" folder for both
        self.assertEqual(data['both'], [{'name': 'Same', 'url': 'https://same.example/'},
                                        {'folder': 'Other bookmarks', 'items': [
                                            {'name': 'Local other', 'url': 'https://lo.example/'},
                                            {'name': 'Account other', 'url': 'https://ao.example/'}]}])
        self.assertIn('Profile 4 -> full: 2000 entries (31 over the entry limit left out)', result.stdout)
        self.assertIn('Profile 5 -> both: 4 entries (1 already in Bookmarks left out)', result.stdout)
        self.assertIn('Profile 6 -> half: 1 entries (1 unreadable bookmarks file left out)', result.stdout)
        self.assertEqual([entry['folder'] for entry in data['twice']], ['Home', 'Work'])
        big = data['big']
        links = sum(1 for e in big if 'url' in e) + sum(len(e['items']) for e in big if 'folder' in e)
        self.assertEqual(len(big), 1991)  # 1990 links + the folder, which kept 9 of its 30 links
        self.assertEqual(links, 1999)
        self.assertIn('21 over the entry limit left out', result.stdout)


    def test_earlier_imports_secret_links_and_private_names_stay_out(self):
        self.profile('Default', 'someone@example.com', {
            'bookmark_bar': folder('Bar', url('', 'https://nameless.example/path'),
                                   url('Signed', 'https://s3.example/obj?X-Amz-Signature=abc&x=1'),
                                   url('Reset', 'https://app.example/reset#access_token=abc'),
                                   url('Search', 'https://search.example/?q=keyboard'),
                                   url('Camel', 'https://camel.example/?accessToken=abc'),
                                   url('Shop', 'https://shop.example/?barcode=1&keyboard=2'),
                                   folder('Imported', folder('laptop-setup', url('Old', 'https://old.example/'))))})
        result = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('someone', result.stdout + self.out.read_text())  # email-shaped name: the folder is the key
        self.assertIn('3 with a secret-looking parameter', result.stdout)
        self.assertNotIn('camel.example', result.stdout)  # hosts only on request
        self.assertIn('1 earlier laptop-setup import', result.stdout)
        text = self.out.read_text()
        for secret in ('abc', 'old.example'):
            self.assertNotIn(secret, text)
        data = yaml.safe_load(text)['chrome_bookmarks']['default']
        self.assertEqual(data, [{'name': 'nameless.example', 'url': 'https://nameless.example/path'},
                                {'name': 'Search', 'url': 'https://search.example/?q=keyboard'},
                                {'name': 'Shop', 'url': 'https://shop.example/?barcode=1&keyboard=2'}])
        shown = subprocess.run([sys.executable, '-I', str(EXPORT), '--chrome-dir', str(self.chrome), '--output',
                                str(self.out), '--show-left-out-hosts'], capture_output=True, text=True)
        self.assertIn('hosts: app.example, camel.example, s3.example', shown.stdout)
        self.assertNotIn('abc', shown.stdout)

    def test_only_the_newest_backups_are_kept_owner_only(self):
        self.profile('Default', 'Personal', {'bookmark_bar': folder('Bar', url('A', 'https://a.example/'))})
        self.out.write_text('first\n')
        self.out.chmod(0o644)
        mine = self.dir / 'local_bookmarks.yml.bak-mine'  # a backup the owner named: never pruned
        mine.write_text('kept\n')
        for _ in range(7):
            self.assertEqual(self.export().returncode, 0)
        self.assertTrue(mine.exists())
        backups = sorted(b for b in self.dir.glob('local_bookmarks.yml.bak-*') if b != mine)
        self.assertEqual(len(backups), 5)
        self.assertTrue(all(b.stat().st_mode & 0o777 == 0o600 for b in backups))

    def test_the_renderer_refuses_a_url_that_is_not_valid_text(self):
        source = self.dir / 'bad.yml'
        source.write_text('chrome_bookmarks:\n  p: [{name: x, url: "https://a.example/\\ud800"}]\n')
        result = subprocess.run([sys.executable, '-I', str(RENDER), '--source', str(source),
                                 '--output-dir', str(self.dir / 'html')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.dir / 'html').exists())


if __name__ == '__main__':
    unittest.main()
