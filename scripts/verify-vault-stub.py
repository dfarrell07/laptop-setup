#!/usr/bin/env python3
"""Accept only the restricted, secret-free YAML vault template on stdin."""
import re
import sys


def is_stub(content):
    keys = set()
    started = False
    for line in content.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line == '---' and not started:
            started = True
            continue
        started = True
        match = re.fullmatch(r'(vault_[A-Za-z0-9_]+):[ \t]*(.*?)(?:[ \t]+#.*)?[ \t]*', line)
        if not match:
            return False
        key, value = match.groups()
        if key in keys:
            return False
        keys.add(key)
        allowed = ('encrypt-me', "'encrypt-me'", '"encrypt-me"') if key == 'vault_placeholder' else ('[]', "''", '""')
        if value not in allowed:
            return False
    return 'vault_placeholder' in keys


if __name__ == '__main__':
    sys.exit(0 if is_stub(sys.stdin.read()) else 1)
