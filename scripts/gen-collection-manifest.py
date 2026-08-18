#!/usr/bin/env python3
"""
Generate a Python dependency manifest for Ansible collections.

Usage:
    gen-collection-manifest.py <collections-dist-dir> <output-manifest>

This script generates a manifest of all Python modules, module_utils, and roles
from each collection tarball. The manifest is committed alongside SHA256SUMS
to enable detection of unexpected transitive Python code additions.

Manifest format (JSON):
{
  "ansible-posix-2.2.2": {
    "plugins": {...module names...},
    "module_utils": {...module_utils names...},
    "roles": {...role names...}
  },
  ...
}
"""

import json
import sys
import tarfile
from pathlib import Path
from collections import defaultdict


def extract_collection_content(tarball_path):
    """Extract Python content metadata from a collection tarball."""
    content = {
        'plugins': defaultdict(list),
        'module_utils': [],
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
                        content['runtime_requires_ansible'] = runtime_data['requires_ansible']
            except Exception:
                pass

        # Extract plugins (by plugin type)
        plugin_types = set()
        for member in members:
            if '/plugins/' in member and member.endswith('.py'):
                parts = member.split('/plugins/', 1)[1].split('/')
                if len(parts) >= 2:
                    plugin_type = parts[0]
                    plugin_types.add(plugin_type)
                    plugin_name = parts[-1].replace('.py', '')
                    if plugin_name != '__init__':
                        content['plugins'][plugin_type].append(plugin_name)

        # Extract module_utils
        module_utils = set()
        for member in members:
            if '/module_utils/' in member and member.endswith('.py'):
                parts = member.split('/module_utils/', 1)[1].split('/')
                module_name = parts[-1].replace('.py', '')
                if module_name != '__init__':
                    module_utils.add(module_name)
        content['module_utils'] = sorted(module_utils)

        # Extract roles
        roles = set()
        for member in members:
            if '/roles/' in member:
                parts = member.split('/roles/', 1)[1].split('/')
                if len(parts) >= 1:
                    role_name = parts[0]
                    roles.add(role_name)
        content['roles'] = sorted(roles)

    # Convert defaultdict to regular dict for JSON serialization
    content['plugins'] = {k: sorted(v) for k, v in content['plugins'].items()}
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
        collection_id = tarball.stem  # e.g., 'community-general-13.2.0'
        try:
            content = extract_collection_content(tarball)
            manifest[collection_id] = content
            print(f"✓ {collection_id}")
        except Exception as e:
            print(f"ERROR: Failed to process {tarball.name}: {e}", file=sys.stderr)
            sys.exit(1)

    # Write manifest with sorted keys for reproducibility
    with open(output_manifest, 'w') as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write('\n')

    print(f"\n✓ Manifest written to {output_manifest}")


if __name__ == '__main__':
    main()
