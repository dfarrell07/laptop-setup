#!/usr/bin/env python3
"""Mutation tests for .github/scripts/verify-critical-assertions.py."""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / '.github/scripts/verify-critical-assertions.py'
NAME = 'Assert identity vars are not CHANGE_ME placeholder values'
MUTATIONS = {
    'comparison without CHANGE_ME': lambda t: t.replace(
        "(dotfiles_github_user | default('CHANGE_ME') != 'CHANGE_ME')",
        "(dotfiles_github_user | default('x') != 'y')", 1),
    'always tag removed': lambda t: t.replace(
        "      when: not _pf_is_molecule\n      tags: [always]\n",
        "      when: not _pf_is_molecule\n", 1),
    'when weakened': lambda t: t.replace(
        "      when: not _pf_is_molecule\n      tags: [always]\n",
        "      when: not _pf_is_molecule or true\n      tags: [always]\n", 1),
}


def run(path):
    return subprocess.run([sys.executable, str(CHECK), str(path)], cwd=ROOT,
                          capture_output=True, text=True, check=False).returncode


def main():
    real = (ROOT / 'site.yml').read_text()
    assert run(ROOT / 'site.yml') == 0, 'real site.yml must pass'
    start = real.index(NAME)
    with tempfile.TemporaryDirectory() as directory:
        for label, mutate in MUTATIONS.items():
            # Only mutate from the first identity task onward.
            mutated = real[:start] + mutate(real[start:])
            assert mutated != real, 'mutation %s did not change site.yml' % label
            path = Path(directory) / 'site.yml'
            path.write_text(mutated)
            assert run(path) != 0, 'mutation passed the guard: %s' % label
    print('OK: critical assertion guard accepts site.yml and rejects %d mutations' % len(MUTATIONS))


if __name__ == '__main__':
    main()
