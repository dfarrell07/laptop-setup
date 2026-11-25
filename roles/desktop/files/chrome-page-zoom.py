#!/usr/bin/env python3
"""Set Chrome's default page zoom (Settings > Appearance > Page zoom) in every profile of a user data dir.

Usage: chrome-page-zoom.py USER_DATA_DIR PERCENT [--check]

Chrome has no policy for the default zoom; it is the profile preference partition.default_zoom_level, keyed "x" for
the default storage partition, holding log(percent / 100) / log(1.2). Chrome reads Preferences at start and rewrites
it from memory, so a write while Chrome runs would be lost: then nothing is written and the result is "running".

Prints one word: "changed", "unchanged", "running" (Chrome holds the profile; close it and re-run) or "absent"
(no profiles yet: Chrome never started). --check reports "changed" without writing.
"""
import json
import math
import os
import sys
import tempfile
from pathlib import Path


def zoom_level(percent):
    return math.log(percent / 100) / math.log(1.2)


def chrome_running(data_dir):
    # Chrome holds SingletonLock, a symlink to "<host>-<pid>", while it runs (Linux and macOS)
    try:
        target = os.readlink(data_dir / 'SingletonLock')
    except OSError:
        return False
    host, _, pid = target.rpartition('-')
    if host != os.uname().nodename or not pid.isdigit():
        return True  # another host's lock (shared home) or an unknown form: never write under it
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False  # a stale lock from a crash
    except PermissionError:
        return True
    return True


def profiles(data_dir):
    return sorted(p / 'Preferences' for p in data_dir.iterdir()
                  if (p.name == 'Default' or p.name.startswith('Profile ')) and (p / 'Preferences').is_file())


def main(argv):
    check = '--check' in argv
    args = [a for a in argv if a != '--check']
    if len(args) != 2:
        sys.exit(__doc__)
    data_dir, percent = Path(args[0]).expanduser(), args[1]
    if not percent.isdigit() or not 25 <= int(percent) <= 500:
        sys.exit(f'chrome-page-zoom: percent must be a whole number from 25 to 500, not {percent!r}')
    want = zoom_level(int(percent))
    found = profiles(data_dir) if data_dir.is_dir() else []
    if not found:
        return 'absent'
    stale = []
    for prefs_file in found:
        try:
            prefs = json.loads(prefs_file.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, ValueError) as err:
            sys.exit(f'chrome-page-zoom: cannot read {prefs_file}: {err}')
        if not isinstance(prefs, dict):
            sys.exit(f'chrome-page-zoom: {prefs_file} is not a JSON object')
        partition = prefs.get('partition')
        levels = partition.get('default_zoom_level', {}) if isinstance(partition, dict) else {}
        have = levels.get('x', 0.0) if isinstance(levels, dict) else None
        if not isinstance(have, (int, float)) or abs(have - want) > 1e-9:
            stale.append((prefs_file, prefs))
    if not stale:
        return 'unchanged'
    if chrome_running(data_dir):
        return 'running'
    if check:
        return 'changed'
    for prefs_file, prefs in stale:
        if not isinstance(prefs.get('partition'), dict):
            prefs['partition'] = {}
        partition = prefs['partition']
        if not isinstance(partition.get('default_zoom_level'), dict):
            partition['default_zoom_level'] = {}
        partition['default_zoom_level']['x'] = want
        mode = prefs_file.stat().st_mode & 0o777
        fd, tmp = tempfile.mkstemp(dir=prefs_file.parent, prefix='.Preferences.')
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(prefs, out, separators=(',', ':'))
        os.chmod(tmp, mode)
        os.replace(tmp, prefs_file)
    return 'changed'


if __name__ == '__main__':
    print(main(sys.argv[1:]))
