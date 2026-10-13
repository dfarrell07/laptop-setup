#!/usr/bin/env python3
"""Render the production launch agent and verify native metadata-only commands."""
from pathlib import Path
import plistlib
import unittest

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent


class BrewUpdateTests(unittest.TestCase):
    def test_prefixes_xml_escaping_and_metadata_only_arguments(self):
        template = Environment(undefined=StrictUndefined).from_string(
            (ROOT / 'roles/packages/templates/homebrew-update.plist.j2').read_text())
        for prefix in ['/opt/homebrew', '/usr/local']:
            for interval in [60, 86400]:
                with self.subTest(prefix=prefix, interval=interval):
                    home = '/Users/Test & Work'
                    content = template.render(_packages_brew_prefix={'stdout': prefix},
                                              packages_brew_autoupdate_interval=interval,
                                              ansible_facts={'user_dir': home})
                    agent = plistlib.loads(content.encode())
                    self.assertEqual(agent['ProgramArguments'], [prefix + '/bin/brew', 'update'])
                    self.assertEqual(agent['StartInterval'], interval)
                    self.assertEqual(agent['StandardOutPath'], home + '/.local/state/log/homebrew-update.log')


if __name__ == '__main__':
    unittest.main()
