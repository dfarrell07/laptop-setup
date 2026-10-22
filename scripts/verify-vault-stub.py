#!/usr/bin/env python3
"""Accept only the restricted vault template on stdin.

Rules: an optional leading '---'; every other non-comment line is 'vault_<name>: <empty>'
(name at most 56 chars after the vault_ prefix, no duplicates, lines at most 1024 chars); a 'vault_placeholder'
sentinel key is required; comments must not contain PEM/OpenSSH armor, private-key words
or long base64-like runs (this does not detect arbitrary prose secrets).
"""
import re
import sys

# PEM/OpenSSH armor, private-key words, or a long base64 run (key or token body).
MAX_BYTES = 256 * 1024
KEY_MATERIAL = re.compile(r'-----BEGIN|PRIVATE[ _-]?KEY|OPENSSH|[A-Za-z0-9+/=_-]{40,}', re.IGNORECASE)


def is_stub(content):
    if '\0' in content or len(content) > MAX_BYTES:
        return False  # NUL bytes hide text from shell-based callers; no real stub is this large
    keys = set()
    started = False
    for line in content.splitlines():
        if not line.strip():
            continue
        if len(line) > 1024:
            return False  # no legitimate stub line is this long; avoid regex backtracking
        if line.lstrip(' \t').startswith('#'):
            # A commented-out key is still a leak in a public repository.
            if KEY_MATERIAL.search(line):
                return False
            continue
        if line == '---' and not started:
            started = True
            continue
        started = True
        if '#' in line and KEY_MATERIAL.search(line.split('#', 1)[1]):
            return False  # key material hidden in an inline comment
        match = re.fullmatch(r'(vault_[A-Za-z0-9_]{1,56}):[ \t]*(.*?)(?:[ \t]+#.*)?[ \t]*', line)
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
    sys.exit(0 if is_stub(sys.stdin.read(MAX_BYTES + 1)) else 1)
