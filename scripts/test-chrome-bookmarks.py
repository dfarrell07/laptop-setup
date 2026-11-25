#!/usr/bin/env python3
"""make chrome-bookmarks: validation and the rendered bookmarks HTML (docs/chrome-bookmarks.md)."""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts/chrome-bookmarks.py'


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.folders, self._tag = [], [], None

    def handle_starttag(self, tag, attrs):
        self._tag = tag
        if tag == 'a':
            self.links.append([dict(attrs)['href'], ''])

    def handle_data(self, data):
        if self._tag == 'a' and self.links:
            self.links[-1][1] += data
        elif self._tag == 'h3':
            self.folders.append(data)

    def handle_endtag(self, tag):
        self._tag = None


class ChromeBookmarksTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def run_script(self, data):
        source = self.dir / 'local_bookmarks.yml'
        source.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding='utf-8')
        return subprocess.run([sys.executable, '-I', str(SCRIPT), '--source', str(source),
                               '--output-dir', str(self.dir / 'out')], capture_output=True, text=True)

    def test_the_example_renders_one_file_per_profile(self):
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--source',
                                 str(ROOT / 'local_bookmarks.yml.example'),
                                 '--output-dir', str(self.dir / 'out')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in (self.dir / 'out').iterdir()),
                         ['chrome-bookmarks-personal.html', 'chrome-bookmarks-work.html'])
        work = self.dir / 'out/chrome-bookmarks-work.html'
        self.assertEqual(work.stat().st_mode & 0o777, 0o600)
        parser = Links()
        parser.feed(work.read_text())
        self.assertEqual(parser.folders, ['laptop-setup', 'Docs'])  # one folder holds the whole set
        self.assertEqual([link for link, _ in parser.links],
                         ['https://tracker.example.internal/team', 'https://docs.example.internal/runbook'])
        self.assertTrue(work.read_text().startswith('<!DOCTYPE NETSCAPE-Bookmark-file-1>'))

    def test_names_and_urls_are_escaped(self):
        result = self.run_script({'chrome_bookmarks': {'personal': [
            {'name': '<script>alert(1)</script> & co', 'url': 'https://example.com/?a=1&b="2"'}]}})
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.dir / 'out/chrome-bookmarks-personal.html').read_text()
        self.assertNotIn('<script>', text)
        parser = Links()
        parser.feed(text)
        self.assertEqual(parser.links, [['https://example.com/?a=1&b="2"', '<script>alert(1)</script> & co']])

    def test_apostrophes_use_the_entity_chrome_decodes(self):
        result = self.run_script({'chrome_bookmarks': {'p': [{'folder': "Dan's", 'items': [
            {'name': "O'Brien's notes", 'url': "https://example.com/o'brien"}]}]}})
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.dir / 'out/chrome-bookmarks-p.html').read_text()
        self.assertNotIn('&#x27;', text)
        self.assertIn('<H3>Dan&#39;s</H3>', text)
        self.assertIn('HREF="https://example.com/o&#39;brien">O&#39;Brien&#39;s notes</A>', text)

    def test_a_file_that_is_not_utf8_is_refused_cleanly(self):
        source = self.dir / 'local_bookmarks.yml'
        source.write_bytes(b'chrome_bookmarks:\n  p: [{name: caf\xe9, url: "https://a.example/"}]\n')
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--source', str(source),
                                 '--output-dir', str(self.dir / 'out')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('not UTF-8', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_unsafe_or_malformed_input_is_refused_and_nothing_is_written(self):
        deep = {'name': 'x', 'url': 'https://example.com/'}
        for _ in range(6):
            deep = {'folder': 'f', 'items': [deep]}
        for data in [
                {'chrome_bookmarks': {'p': [{'name': 'x', 'url': 'javascript:alert(1)'}]}},
                {'chrome_bookmarks': {'p': [{'name': 'x', 'url': 'data:text/html,hi'}]}},
                {'chrome_bookmarks': {'p': [{'name': 'x', 'url': 'file:///etc/passwd'}]}},
                {'chrome_bookmarks': {'p': [{'name': 'x', 'url': 'https://user:pw@example.com/'}]}},
                {'chrome_bookmarks': {'p': [{'name': '', 'url': 'https://example.com/'}]}},
                {'chrome_bookmarks': {'p': [{'name': 'x', 'url': 'https://example.com/', 'extra': 1}]}},
                {'chrome_bookmarks': {'p': [deep]}},
                {'chrome_bookmarks': {'../evil': []}},
                {'chrome_bookmarks': {'p': [{'name': 'x', 'url': 'https://exa mple.com/'}]}},
                {'bookmarks': {}}, 'chrome_bookmarks: [unclosed', {'chrome_bookmarks': {}},
                {'chrome_bookmarks': {'p': []}},
                'chrome_bookmarks:\n  p: [{name: a, url: "https://a.example/"}]\n  p: [{name: b, url: "https://b.example/"}]\n',
                'chrome_bookmarks:\n  p: [{name: a, name: b, url: "https://a.example/"}]\n']:
            result = self.run_script(data)
            self.assertEqual(result.returncode, 1, data)
            self.assertTrue(result.stderr.startswith('chrome-bookmarks: '), result.stderr)
            self.assertFalse((self.dir / 'out').exists(), data)

    def test_fuzz_finds_are_refused_cleanly(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('cb', SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for name in ('ok \ud800', 'a\x85b', 'a\u2028b', 'a\x7fb'):
            with self.subTest(name=repr(name)), self.assertRaises(mod.Invalid):
                mod.check_name(name, 'p')
        for url in ('https://e.com:99999/', 'https://e.com:abc/', 'https://e.com/\x85', 'https://e.com/\u2028',
                    'https://e.com/\ud800'):
            with self.subTest(url=repr(url)), self.assertRaises(mod.Invalid):
                mod.check_url(url, 'p')
        for url in ('https://e.com/\u202e', 'https://e\u200b.com/', 'https://e.com/\ufeff'):
            with self.subTest(url=repr(url)), self.assertRaises(mod.Invalid):
                mod.check_url(url, 'p')
        mod.check_name('Family \U0001F468\u200d\U0001F469', 'p')  # a ZWJ emoji title stays fine
        mod.check_url('https://e.com:8443/a', 'p')  # a real port is fine
        mod.check_name('Café ☕', 'p')

    def test_a_missing_file_points_at_the_example(self):
        result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--source', str(self.dir / 'none.yml'),
                                 '--output-dir', str(self.dir / 'out')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('none.yml.example', result.stderr)


if __name__ == '__main__':
    unittest.main()
