#!/usr/bin/env python3
"""Generate full-path SHA256 baselines for vendored collection runtime code.

Usage: gen-collection-manifest.py <collections-dist-dir> <output-manifest>
Each .tar key records a runtime_files mapping of
complete relative .py/.so filenames under plugins, module_utils, and roles.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile


def extract_collection_content(tarball_path):
    content = {'runtime_files': {}}
    with tarfile.open(tarball_path, 'r:gz') as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError(f'Invalid archive path: {member.name}')
            name = path.as_posix()
            if path.parts and path.parts[0] in ('plugins', 'module_utils', 'roles') and path.suffix in ('.py', '.so'):
                if not member.isfile() or name in content['runtime_files']:
                    raise ValueError(f'Invalid or duplicate runtime file: {name}')
                digest = hashlib.sha256()
                with tar.extractfile(member) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                content['runtime_files'][name] = digest.hexdigest()
    return content


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    directory, output = map(Path, sys.argv[1:])
    archives = sorted(directory.glob('*.tar.gz'))
    if not archives:
        raise ValueError(f'No collection archives found in {directory}')
    manifest = {archive.stem: extract_collection_content(archive) for archive in archives}
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    for name, entry in manifest.items():
        print(f'{name}: {len(entry["runtime_files"])} runtime files')
    return 0


if __name__ == '__main__':
    sys.exit(main())
