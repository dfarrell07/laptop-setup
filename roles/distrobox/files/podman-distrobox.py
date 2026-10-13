#!/usr/bin/python3 -I
"""Keep Distrobox's crun selection compatible with Podman's global options."""
import os
import sys

# Even an empty CONTAINER_HOST selects Podman's remote client mode.
for name in ['CONTAINER_HOST', 'CONTAINER_CONNECTION', 'CONTAINER_SSHKEY',
             'DOCKER_HOST', 'REGISTRY_AUTH_FILE', 'DOCKER_CONFIG']:
    os.environ.pop(name, None)

arguments = sys.argv[1:]
if '--runtime=crun' in arguments:
    arguments = ['--runtime=crun', *[argument for argument in arguments if argument != '--runtime=crun']]
os.execv('/usr/bin/podman', ['/usr/bin/podman', *arguments])
