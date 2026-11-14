#!/usr/bin/env python3
"""Pre-trust directories in an agent CLI's own state file, so it does not ask "trust this folder?" there.

Usage: agent-trust.py claude|codex|agy STATE_FILE [--check] DIR...

  claude  .claude.json: projects[DIR].hasTrustDialogAccepted = true
  codex   config.toml:  [projects."DIR"] trust_level = "trusted" (an existing [projects."DIR"] table is left alone)
  agy     antigravity-cli/settings.json: DIR appended to trustedWorkspaces

Only adds trust; never removes or changes anything else. A missing state file is left missing (the tool creates it
on first run; the next provisioning run adds the entries). The tools rewrite these files while running, so the write
is skipped, not forced, when the file changes between reading and replacing it. Prints "changed", "unchanged",
"missing" or "busy".
"""
import json
import os
import re
import sys
import tempfile

import tomllib  # Python 3.11+; Ansible's own Python runs this helper


def claude(text, dirs):
    data = json.loads(text)
    projects = data.setdefault('projects', {})
    for path in dirs:
        # A new entry gets the structural fields Claude Code itself writes for a project, so nothing reads a
        # missing list or map.
        entry = projects.setdefault(path, {
            'allowedTools': [], 'mcpContextUris': [], 'mcpServers': {}, 'enabledMcpjsonServers': [],
            'disabledMcpjsonServers': [], 'hasClaudeMdExternalIncludesApproved': False,
            'hasClaudeMdExternalIncludesWarningShown': False})
        entry['hasTrustDialogAccepted'] = True
    return json.dumps(data, indent=2, ensure_ascii=False) + '\n'


def agy(text, dirs):
    data = json.loads(text)
    trusted = data.setdefault('trustedWorkspaces', [])
    if not isinstance(trusted, list):
        sys.exit('agent-trust: trustedWorkspaces is not a list')
    trusted.extend(path for path in dirs if path not in trusted)
    return json.dumps(data, indent=2, ensure_ascii=False) + '\n'


def codex(text, dirs):
    old = tomllib.loads(text)
    projects = old.get('projects', {})
    missing = [path for path in dirs if path not in projects]
    if not missing:
        return text
    tables = ''.join('\n[projects.%s]\ntrust_level = "trusted"\n' % json.dumps(path) for path in missing)
    new = text if not text or text.endswith('\n') else text + '\n'
    new += tables
    expected = dict(old, projects=dict(projects, **{path: {'trust_level': 'trusted'} for path in missing}))
    if tomllib.loads(new) != expected:
        sys.exit('agent-trust: could not add project trust safely; add [projects."DIR"] tables by hand')
    return new


EDITORS = {'claude': claude, 'codex': codex, 'agy': agy}


def main(argv):
    args = [a for a in argv[1:] if a != '--check']
    check = '--check' in argv
    if len(args) < 2 or args[0] not in EDITORS:
        sys.exit('agent-trust: usage: claude|codex|agy STATE_FILE [--check] DIR...')
    kind, path = args[0], os.path.realpath(args[1])
    for d in args[2:]:
        if not d.startswith('/') or re.search(r'[\x00-\x1f\x7f]', d):
            sys.exit('agent-trust: %r is not an absolute path' % d)
    # The tools key trust by the resolved path, so a directory under a symlink (~/src -> /data/src) is trusted
    # under both spellings.
    normal = [os.path.normpath(d) for d in args[2:]]
    dirs = list(dict.fromkeys(normal + [os.path.realpath(d) for d in normal]))
    if not os.path.exists(path):
        print('missing')
        return
    before = os.stat(path)
    with open(path, encoding='utf-8') as handle:
        old = handle.read()
    try:
        new = EDITORS[kind](old, dirs) if dirs else old
    except (ValueError, tomllib.TOMLDecodeError) as error:
        sys.exit('agent-trust: %s does not parse (%s); left unchanged' % (path, error))
    if kind != 'codex' and json.loads(new) == json.loads(old):
        new = old  # already trusted: keep the tool's own formatting
    changed = new != old
    if changed and not check:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.agent-trust-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(new)
            os.chmod(tmp, 0o600)
            now = os.stat(path)
            if (now.st_mtime_ns, now.st_size, now.st_ino) != (before.st_mtime_ns, before.st_size, before.st_ino):
                print('busy')  # a running session rewrote it; the next run adds the entries
                return
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    print('changed' if changed else 'unchanged')


if __name__ == '__main__':
    main(sys.argv)
