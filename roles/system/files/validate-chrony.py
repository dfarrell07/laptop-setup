#!/usr/bin/python3
"""Validate rendered chrony directives without daemon access to private temp paths."""
from pathlib import Path
import subprocess
import sys


def validate(path, binary='/usr/sbin/chronyd'):
    directives = [line for line in Path(path).read_text().splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
    return subprocess.run([binary, '-p', '-f', '/dev/null', '--', *directives],
                          check=False, capture_output=True, text=True)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('Usage: validate-chrony.py CONFIG')
    result = validate(sys.argv[1])
    if result.returncode:
        sys.stderr.write(result.stderr)
    sys.exit(result.returncode)
