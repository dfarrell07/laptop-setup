#!/usr/bin/env python3
"""Compile persistent kernel and GRUB arguments without evaluating shell data."""
from collections import Counter
import json
from pathlib import Path
import re
import shlex
import stat
import sys


def merge_arguments(text, settings):
    pattern = re.compile(r'''(?:[^\s"'\\]+|\\.|"(?:[^"\\]|\\.)*"|'[^']*')+''')
    tokens, position = [], 0
    for match in pattern.finditer(text):
        if text[position:match.start()].strip():
            raise ValueError('Malformed boot argument quoting')
        tokens.append(match.group())
        position = match.end()
    if text[position:].strip():
        raise ValueError('Malformed boot argument quoting')
    values = []
    for token in tokens:
        decoded = shlex.split(token)
        if len(decoded) != 1:
            raise ValueError('Malformed boot argument')
        key, _, value = decoded[0].partition('=')
        if key in {'BOOT_IMAGE', 'lockdown', 'vsyscall', 'init_on_free', 'page_alloc.shuffle'}:
            continue
        if key in {'amd_iommu', 'intel_iommu'} and settings[key[:key.index('_')]]:
            continue
        if key == 'iommu' and (settings['passthrough'] or value == 'pt'):
            continue
        if key == 'rd.luks.key' and settings['remove_luks_key']:
            continue
        # Same policy as the grubby discard removal, so kernel installs and
        # grub2-mkconfig do not restore what that task removed.
        if not settings['luks_discards'] and (key == 'rd.luks.allow-discards'
                                              or (key == 'rd.luks.options' and value == 'discard')):
            continue
        values.append(token)
    mode = settings['lockdown']
    if mode not in {'', 'none', 'integrity', 'confidentiality'}:
        raise ValueError('Invalid lockdown mode')
    if mode:
        values.append('lockdown=' + mode)
    values.append('vsyscall=none')
    if settings['init_on_free']:
        values.append('init_on_free=1')
    values.append('page_alloc.shuffle=1')
    for vendor in ['amd', 'intel']:
        if settings[vendor]:
            values.append(vendor + '_iommu=on')
    if settings['passthrough'] and (settings['amd'] or settings['intel']):
        values.append('iommu=pt')
    # grub2-mkconfig can reorder equivalent arguments. Keep its text when
    # the complete token multiset already matches the enforced policy.
    if Counter(tokens) == Counter(values):
        return text.rstrip('\r\n')
    return ' '.join(values)


def merge_grub(text, settings):
    lines = text.splitlines()
    positions = [i for i, line in enumerate(lines) if re.match(r'^\s*(?:export\s+)?GRUB_CMDLINE_LINUX=', line)]
    if len(positions) > 1:
        raise ValueError('Multiple GRUB_CMDLINE_LINUX assignments')
    if positions:
        index = positions[0]
        prefix, value = lines[index].split('=', 1)
        value = value.strip()
        if not value or value[0] not in {'"', "'"} or value[-1] != value[0]:
            raise ValueError('GRUB_CMDLINE_LINUX must use one quoted assignment')
        quote, value = value[0], value[1:-1]
        lines[index] = prefix + '=' + quote + merge_arguments(value, settings) + quote
    else:
        lines.append('GRUB_CMDLINE_LINUX="' + merge_arguments('', settings) + '"')
    return '\n'.join(lines) + '\n'


def read_config(path):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise ValueError('Boot configuration must be a root-owned non-writable regular file')
    return path.read_text(), format(stat.S_IMODE(metadata.st_mode), '04o')


def main():
    settings = json.loads(sys.argv[1])
    kernel = Path('/etc/kernel/cmdline')
    current, kernel_mode = read_config(kernel) if kernel.exists() or kernel.is_symlink() else (Path('/proc/cmdline').read_text(), '0644')
    result = {'kernel': merge_arguments(current, settings) + '\n', 'kernel_mode': kernel_mode}
    grub = Path('/etc/default/grub')
    if grub.exists() or grub.is_symlink():
        current, grub_mode = read_config(grub)
        result.update(grub=merge_grub(current, settings), grub_mode=grub_mode)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
