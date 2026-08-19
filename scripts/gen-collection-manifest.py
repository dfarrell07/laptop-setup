#!/usr/bin/env python3
"""
Generate a Python dependency manifest for Ansible collections.

Usage:
    gen-collection-manifest.py <collections-dist-dir> <output-manifest>

This script generates a manifest of all Python modules, module_utils, and roles
from each collection tarball, including SHA256 content hashes for each .py file.
The manifest is committed alongside SHA256SUMS to enable detection of unexpected
transitive Python code additions and TOCTOU (Time-of-Check-Time-of-Use) attacks
via per-file content hash verification.

Manifest format (JSON):
{
  "ansible-posix-2.2.2.tar": {
    "plugins": {
      "modules": {
        "ping": "68143c60d730863b...",
        ...
      },
      ...
    },
    "module_utils": {
      "version": "ab063b19d6340894...",
      ...
    },
    "roles": [...role names...]
  },
  ...
}
"""

import hashlib
import json
import sys
import tarfile
from pathlib import Path
from collections import defaultdict


def compute_file_hash(file_content):
    """Compute SHA256 hash of file content (bytes)."""
    sha256 = hashlib.sha256()
    sha256.update(file_content)
    return sha256.hexdigest()


def extract_collection_content(tarball_path):
    """Extract Python content metadata from a collection tarball with per-file hashes."""
    content = {
        'plugins': defaultdict(dict),
        'module_utils': {},
        'so_plugins': defaultdict(dict),
        'so_module_utils': {},
        'roles': set(),
        'runtime_requires_ansible': None,
    }

    with tarfile.open(tarball_path, 'r:gz') as tar:
        members = tar.getnames()

        # Extract meta/runtime.yml if present
        runtime_members = [m for m in members if m.endswith('/meta/runtime.yml')]
        if runtime_members:
            try:
                runtime_file = tar.extractfile(runtime_members[0])
                if runtime_file:
                    import yaml
                    runtime_data = yaml.safe_load(runtime_file)
                    if runtime_data and 'requires_ansible' in runtime_data:
                        content['runtime_requires_ansible'] = (
                            runtime_data['requires_ansible'])
            except Exception:
                pass

        # Extract plugins (by plugin type) with content hashes
        plugin_types = set()
        for member in members:
            if '/plugins/' in member and member.endswith('.py'):
                parts = member.split('/plugins/', 1)[1].split('/')
                if len(parts) >= 2:
                    plugin_type = parts[0]
                    plugin_types.add(plugin_type)
                    plugin_name = parts[-1].replace('.py', '')
                    if plugin_name != '__init__':
                        try:
                            file_obj = tar.extractfile(member)
                            if file_obj:
                                file_content = file_obj.read()
                                file_hash = compute_file_hash(file_content)
                                content['plugins'][plugin_type][plugin_name] = (
                                    file_hash)
                        except Exception:
                            pass
            elif '/plugins/' in member and member.endswith('.so'):
                parts = member.split('/plugins/', 1)[1].split('/')
                if len(parts) >= 2:
                    plugin_type = parts[0]
                    # Store full filename (with .so) to distinguish from .py entries
                    so_name = parts[-1]
                    try:
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            file_content = file_obj.read()
                            file_hash = compute_file_hash(file_content)
                            content['so_plugins'][plugin_type][so_name] = (
                                file_hash)
                    except Exception:
                        pass

        # Extract module_utils with content hashes
        for member in members:
            if '/module_utils/' in member and member.endswith('.py'):
                parts = member.split('/module_utils/', 1)[1].split('/')
                module_name = parts[-1].replace('.py', '')
                if module_name != '__init__':
                    try:
                        file_obj = tar.extractfile(member)
                        if file_obj:
                            file_content = file_obj.read()
                            file_hash = compute_file_hash(file_content)
                            content['module_utils'][module_name] = file_hash
                    except Exception:
                        pass
            elif '/module_utils/' in member and member.endswith('.so'):
                parts = member.split('/module_utils/', 1)[1].split('/')
                # Store full filename (with .so) to distinguish from .py entries
                so_name = parts[-1]
                try:
                    file_obj = tar.extractfile(member)
                    if file_obj:
                        file_content = file_obj.read()
                        file_hash = compute_file_hash(file_content)
                        content['so_module_utils'][so_name] = file_hash
                except Exception:
                    pass

        # Extract roles (names only — roles have complex structures)
        roles = set()
        for member in members:
            if '/roles/' in member:
                parts = member.split('/roles/', 1)[1].split('/')
                if len(parts) >= 1:
                    role_name = parts[0]
                    roles.add(role_name)
        content['roles'] = sorted(roles)

    # Convert defaultdict to regular dict for JSON serialization
    content['plugins'] = {k: dict(v) for k, v in content['plugins'].items()}
    content['so_plugins'] = {k: dict(v) for k, v in content['so_plugins'].items()}
    return content


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    collections_dist = Path(sys.argv[1])
    output_manifest = Path(sys.argv[2])

    if not collections_dist.is_dir():
        print(f"ERROR: {collections_dist} is not a directory", file=sys.stderr)
        sys.exit(1)

    manifest = {}
    for tarball in sorted(collections_dist.glob('*.tar.gz')):
        # Use tarball name with .tar suffix (without .gz) to match verify-collections.sh
        tarball_id = tarball.stem + '.tar'  # e.g., 'community-general-13.2.0.tar'
        try:
            content = extract_collection_content(tarball)
            manifest[tarball_id] = content
            print(f"✓ {tarball_id}")
        except Exception as e:
            print(f"ERROR: Failed to process {tarball.name}: {e}",
                  file=sys.stderr)
            sys.exit(1)

    # Write manifest with sorted keys for reproducibility
    with open(output_manifest, 'w') as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write('\n')

    print(f"\n✓ Manifest written to {output_manifest}")


if __name__ == '__main__':
    main()
