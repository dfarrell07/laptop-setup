#!/usr/bin/env python3
"""Exercise the production tmpfiles template without changing the real home."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which('systemd-tmpfiles'), 'Requires Linux systemd-tmpfiles')
class ClaudePrivacyTests(unittest.TestCase):
    def test_recursive_privacy_preserves_file_modes_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            default = home / '.claude'
            personal = home / '.claude-personal'
            nested = default / 'nested'
            nested.mkdir(parents=True)
            personal.mkdir()
            for path in [default, nested, personal]:
                path.chmod(0o755)
            modes = {default / 'CLAUDE.md': 0o600, default / 'settings.local.json': 0o644,
                     nested / 'session.json': 0o644, nested / 'run.sh': 0o755,
                     personal / 'settings.local.json': 0o644}
            for path, mode in modes.items():
                path.write_text('public test fixture\n')
                path.chmod(mode)
            outside = home / 'outside.txt'
            outside.write_text('public outside fixture\n')
            outside.chmod(0o644)
            (nested / 'outside-link').symlink_to(outside)
            template = Environment(undefined=StrictUndefined).from_string(
                (ROOT / 'roles/claude/templates/claude-privacy.conf.j2').read_text())
            config = home / 'privacy.conf'
            config.write_text(template.render(_claude_instances=[str(default), str(personal)],
                                              claude_default_config_dir=str(default)).replace('%h', str(home)))
            for _ in range(2):
                result = subprocess.run(['systemd-tmpfiles', '--user', '--create', str(config)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                for path in [default, nested]:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o700)
                for path in modes:
                    expected = 0o700 if path.name == 'run.sh' else 0o600
                    self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))
                self.assertEqual(outside.stat().st_mode & 0o777, 0o644)


if __name__ == '__main__':
    unittest.main()
