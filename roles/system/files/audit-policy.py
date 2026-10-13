#!/usr/bin/env python3
"""Compile audit policy for the target filesystem, preserving immutable repeats."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import stat
import sys


def compile_policy(policy, exists=os.path.exists):
    lines = []
    omitted = []
    for line in policy.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            lines.append(line)
            continue
        fields = shlex.split(line)
        executable = next((field[4:] for field in fields if field.startswith('exe=')), None)
        if executable and not exists(executable):
            if fields[:2] != ['-a', 'never,exit']:
                raise ValueError(f'Required audit executable missing: {executable}')
            omitted.append(executable)
            continue  # More auditing until the next explicit policy change/reboot.
        lines.append(line)
    return '\n'.join(lines) + '\n', sorted(set(omitted))


def select_policy(policy, current, receipt, exists=os.path.exists):
    digest = hashlib.sha256(policy.encode()).hexdigest()
    if receipt and receipt['policy_sha256'] == digest:
        if hashlib.sha256(current.encode()).hexdigest() != receipt['compiled_sha256']:
            raise ValueError('Installed audit policy does not match its root-owned receipt')
        return current, receipt
    compiled, omitted = compile_policy(policy, exists)
    return compiled, {'policy_sha256': digest,
                      'compiled_sha256': hashlib.sha256(compiled.encode()).hexdigest(),
                      'omitted_executables': omitted}


def prepare_watch_directories(policy, home, uid, gid, apply=True):
    """File watches can track absent leaves; directory watches need real trees."""
    created = []
    home_input = home.absolute()
    home = home.resolve()
    for line in policy.splitlines():
        if not line.startswith('-w '):
            continue
        fields = shlex.split(line)
        target = Path(fields[1])
        directory = target if fields[1].endswith('/') else target.parent
        user_path = (directory == home_input or home_input in directory.parents
                     or directory == home or home in directory.parents)
        canonical = directory.resolve()
        if user_path and canonical != home and home not in canonical.parents:
            if not directory.is_dir():
                raise ValueError(f'User audit directory escapes home: {directory}')
            continue  # Existing external directories need no privileged creation.
        directory = canonical
        missing = []
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        if not directory.is_dir():
            raise ValueError(f'Audit watch parent is not a directory: {directory}')
        if not apply:
            continue
        if user_path:
            # Walk the resolved path through directory FDs so a user cannot
            # swap a parent for an external symlink during privileged creation.
            descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in canonical.relative_to(home).parts:
                    made = False
                    try:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    except FileNotFoundError:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=descriptor)
                            made = True
                        except FileExistsError:
                            pass
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                    if made:
                        os.fchown(descriptor, uid, gid)
                        created.append(str(canonical))
            finally:
                os.close(descriptor)
        else:
            for directory in reversed(missing):
                directory.mkdir(mode=0o755)
                created.append(str(directory))
    return created


def main():
    policy = sys.stdin.read()
    home = Path(sys.argv[1])
    account = pwd.getpwnam(sys.argv[2])
    if account.pw_uid == 0 or home.stat().st_uid != account.pw_uid or not home.is_dir():
        raise ValueError('Audit home must be an existing directory owned by the real non-root user')
    created = prepare_watch_directories(policy, home, account.pw_uid, account.pw_gid, sys.argv[3] != 'check')
    destination = Path('/etc/audit/rules.d/claude-code.rules')
    receipt_path = Path('/etc/audit/claude-code-policy.json')
    current = ''
    receipt = None
    if receipt_path.exists():
        metadata = receipt_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise ValueError('Audit receipt must be a private root-owned regular file')
        receipt = json.loads(receipt_path.read_text())
        metadata = destination.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError('Installed audit policy must be root-owned and non-writable by others')
        current = destination.read_text()
    compiled, receipt = select_policy(policy, current, receipt)
    print(json.dumps({'content': compiled, 'receipt': receipt, 'created_directories': created}))


if __name__ == '__main__':
    main()
