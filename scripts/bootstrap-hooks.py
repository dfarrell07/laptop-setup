#!/usr/bin/env python3
"""Install the SHA256-pinned secret scanner without workstation provisioning."""
import argparse
import hashlib
import io
import os
from pathlib import Path
import platform
import tarfile
import tempfile
import urllib.request

VERSION = '8.30.1'
# Linux pins match roles/packages/defaults/main.yml; Darwin pins are release asset digests.
RELEASES = {
    ('Linux', 'x86_64'): ('linux_x64', '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'),
    ('Linux', 'aarch64'): ('linux_arm64', 'e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080'),
    ('Darwin', 'x86_64'): ('darwin_x64', 'dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709'),
    ('Darwin', 'arm64'): ('darwin_arm64', 'b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5'),
}


def verified_binary(archive, expected):
    if hashlib.sha256(archive).hexdigest() != expected:
        raise ValueError('gitleaks archive SHA256 mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
        matches = [entry for entry in source.getmembers() if entry.name == 'gitleaks']
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError('gitleaks archive must contain one regular executable')
        return source.extractfile(matches[0]).read()


def install(binary, directory):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'gitleaks'
    if target.is_file() and not target.is_symlink() and target.read_bytes() == binary:
        if target.stat().st_mode & 0o7777 != 0o755:
            target.chmod(0o755)
        return target
    with tempfile.NamedTemporaryFile(dir=directory, prefix='.gitleaks-', delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(binary)
            output.flush()
            os.fsync(output.fileno())
            temporary.chmod(0o755)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    args = parser.parse_args()
    key = (platform.system(), platform.machine())
    if key not in RELEASES:
        parser.error(f'Unsupported platform: {key}')
    asset, expected = RELEASES[key]
    url = f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_{asset}.tar.gz'
    with urllib.request.urlopen(url, timeout=120) as response:
        binary = verified_binary(response.read(), expected)
    target = install(binary, args.bin_dir)
    print(f'Installed verified gitleaks {VERSION}: {target}')
    if str(args.bin_dir.resolve()) not in os.environ.get('PATH', '').split(os.pathsep):
        print(f'Add {args.bin_dir} to PATH before committing.')


if __name__ == '__main__':
    main()
