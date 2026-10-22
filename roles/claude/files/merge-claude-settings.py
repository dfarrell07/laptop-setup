#!/usr/bin/env python3
"""Merge the Ansible-managed Claude Code security policy into a user settings.json.

Usage: merge-claude-settings.py SETTINGS_JSON [--check] [--remove-legacy LOCAL_JSON] < policy.json

Prints "changed" or "unchanged". Exits non-zero (and leaves the file alone) when
the existing file or the policy is not a JSON object.

Merge rules: user keys are kept; permissions.deny is the de-duplicated union
(policy entries first); permissions.allow/ask stay the user's; every other policy
key (sandbox, enableAllProjectMcpServers, cleanupPeriodDays, env keys) overrides
the user's value, merging dictionaries recursively.
"""
import json
import os
import sys
import tempfile
import time

POLICY_KEYS = {'env', 'sandbox', 'cleanupPeriodDays', 'enableAllProjectMcpServers', 'permissions'}


def fail(message):
    sys.exit('merge-claude-settings: ' + message)


def load_object(text, what):
    try:
        value = json.loads(text)
    except ValueError as exc:
        fail('%s is not valid JSON (%s); refusing to overwrite it' % (what, exc))
    if not isinstance(value, dict):
        fail('%s is not a JSON object; refusing to overwrite it' % what)
    return value


def deep_merge(base, override):
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def merge(existing, policy):
    policy = dict(policy)
    perms = policy.pop('permissions', {})
    merged = deep_merge(existing, policy)
    current = merged.get('permissions', {})
    if not isinstance(current, dict):
        fail('existing "permissions" is not an object; refusing to overwrite it')
    deny = list(dict.fromkeys(list(perms.get('deny', [])) + list(current.get('deny', []))))
    out_perms = deep_merge(current, {k: v for k, v in perms.items() if k not in ('deny', 'allow', 'ask')})
    for key in ('allow', 'ask'):
        if key not in current and key in perms:
            out_perms[key] = perms[key]
    out_perms['deny'] = deny
    merged['permissions'] = out_perms
    return merged


def is_legacy_policy_file(data):
    perms = data.get('permissions', {})
    return (set(data) <= POLICY_KEYS and 'sandbox' in data and isinstance(perms, dict)
            and set(perms) <= {'deny'})


def main(argv):
    args = argv[1:]
    check = '--check' in args
    legacy = None
    if '--remove-legacy' in args:
        legacy = args[args.index('--remove-legacy') + 1]
        args = [a for a in args if a not in ('--check', '--remove-legacy', legacy)]
    args = [a for a in args if a != '--check']
    if len(args) != 1:
        fail('usage: SETTINGS_JSON [--check] [--remove-legacy LOCAL_JSON] < policy.json')
    # Write through a symlinked settings.json instead of replacing the link.
    path = os.path.realpath(args[0])
    policy = load_object(sys.stdin.read(), 'policy')
    existing = {}
    old_text = None
    if os.path.exists(path):
        with open(path, encoding='utf-8') as handle:
            old_text = handle.read()
        existing = load_object(old_text, path) if old_text else {}
    new_text = json.dumps(merge(existing, policy), indent=2) + '\n'
    changed = old_text is None or existing != json.loads(new_text) or \
        (os.stat(path).st_mode & 0o777) != 0o600
    if changed and not check:
        if old_text is not None:
            stamp = time.strftime('%Y%m%dT%H%M%S')
            for attempt in range(100):
                backup = '%s.bak-%s%s' % (path, stamp, '-%d' % attempt if attempt else '')
                try:
                    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    break
                except FileExistsError:
                    continue
            else:
                fail('could not create a unique backup name for ' + path)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(old_text)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.', prefix='.settings-')
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(new_text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    if legacy and os.path.exists(legacy):
        try:
            with open(legacy, encoding='utf-8') as handle:
                data = json.load(handle)
        except ValueError:
            data = None
        if isinstance(data, dict) and is_legacy_policy_file(data):
            changed = True
            if not check:
                os.remove(legacy)
    print('changed' if changed else 'unchanged')


if __name__ == '__main__':
    main(sys.argv)
