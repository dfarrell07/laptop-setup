#!/usr/bin/env python3
"""Set top-level keys in a Codex config.toml, leaving every other line alone.

Usage: set-codex-keys.py CONFIG_TOML [--check] KEY=JSON_VALUE...

The result must parse to the old contents plus exactly these keys, or the file is left alone. Only the part of
the file before the first [table] header is top level, so a key of the same name inside a table
(for example [profiles.x]) is never touched. Prints "changed" or "unchanged"; creates the file if it is missing.
"""
import json
import os
import re
import sys
import tempfile

import tomllib  # Ansible's own Python (3.12+) runs this helper


def main(argv):
    args = [a for a in argv[1:] if a != '--check']
    check = '--check' in argv
    if len(args) < 2 or not all('=' in a for a in args[1:]):
        sys.exit('set-codex-keys: usage: CONFIG_TOML [--check] KEY=JSON_VALUE...')
    path = os.path.realpath(args[0])
    wanted, values = {}, {}
    for pair in args[1:]:
        key, value = pair.split('=', 1)
        if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
            sys.exit('set-codex-keys: invalid key %r' % key)
        values[key] = json.loads(value)
        wanted[key] = '%s = %s' % (key, json.dumps(values[key]))
    old = open(path, encoding='utf-8').read() if os.path.exists(path) else ''
    lines = old.splitlines()
    head_end = next((i for i, line in enumerate(lines) if re.match(r'\s*\[', line)), len(lines))
    head, tail = lines[:head_end], lines[head_end:]
    for key, line in wanted.items():
        pattern = re.compile(r'\s*%s\s*=' % re.escape(key))
        matches = [i for i, existing in enumerate(head) if pattern.match(existing)]
        if matches:
            head[matches[0]] = line
            for i in reversed(matches[1:]):
                del head[i]
        else:
            head.insert(0, line)
    new = '\n'.join(head + tail) + '\n'
    # The edit must change exactly these keys: a key-like line inside a multi-line string, a quoted or dotted key,
    # or a table header inside an array would otherwise slip through a parse-only check.
    try:
        ok = tomllib.loads(new) == dict(tomllib.loads(old), **values)
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        sys.exit('set-codex-keys: could not set %s safely in %s; set them by hand' % (', '.join(values), path))
    changed = new != old
    if changed and not check:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.config-')
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(new)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    print('changed' if changed else 'unchanged')


if __name__ == '__main__':
    main(sys.argv)
