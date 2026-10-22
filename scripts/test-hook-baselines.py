#!/usr/bin/env python3
"""Exercise staged baseline validation with real isolated Git indexes."""
import hashlib
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
# A provisioned host sets a global core.hooksPath; keep fixture commits independent of it.
os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


class BaselineTests(unittest.TestCase):
    def test_index_hashes_atomic_updates_and_mode_only_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            source = repo / 'Makefile'
            baseline = repo / 'Makefile.sha256'
            def write(content):
                source.write_bytes(content)
                baseline.write_text(hashlib.sha256(content).hexdigest() + '  Makefile\n')
            write(b'original\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@ci.invalid',
                            '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Fixture'], check=True)
            hook = (ROOT / '.githooks/pre-commit').read_text()
            function = hook[hook.index('check_modified_and_hash_baseline()'):hook.index('# Guard scripts')]
            def check(accepted, path=None):
                result = subprocess.run(['/bin/bash', '-c', 'failed=0\n' + function +
                                         '\ncheck_modified_and_hash_baseline Makefile Makefile.sha256 Makefile critical\nexit "$failed"'],
                                        cwd=repo, env=dict(os.environ, PATH=path or os.environ['PATH']),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)
                if accepted:
                    self.assertNotIn('WARN:', result.stderr)
            write(b'updated\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            source.write_bytes(b'unstaged unrelated content\n')
            check(True)  # The staged pair, not the worktree, is authoritative.
            if shutil.which('shasum'):  # perl-Digest-SHA is not in a default Fedora install
                portable = repo / 'tools'
                portable.mkdir()
                for tool in ['git', 'shasum']:
                    (portable / tool).symlink_to(shutil.which(tool))
                check(True, str(portable))  # Native macOS checksum tool, with no GNU sha256sum.
            baseline.write_text('0' * 64 + '  Makefile\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'Makefile.sha256'], check=True)
            check(False)
            subprocess.run(['git', '-C', str(repo), 'reset', '--hard', '-q', 'HEAD'], check=True)
            source.write_bytes(b'mismatch\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'Makefile'], check=True)
            check(False)
            subprocess.run(['git', '-C', str(repo), 'reset', '--hard', '-q', 'HEAD'], check=True)
            subprocess.run(['git', '-C', str(repo), 'update-index', '--chmod=+x', 'Makefile'], check=True)
            check(True)
            baseline.write_text('malformed baseline\n')
            subprocess.run(['git', '-C', str(repo), 'add', 'Makefile.sha256'], check=True)
            check(False)


if __name__ == '__main__':
    unittest.main()
