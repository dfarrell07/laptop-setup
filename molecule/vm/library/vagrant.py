#!/usr/bin/python
"""Expose the hash-locked Vagrant driver beside its controller-only playbooks."""
# Include basic in the AnsiballZ payload; the installed driver's main uses it.
from ansible.module_utils import basic  # noqa: F401
from molecule_plugins.vagrant.modules.vagrant import main

if __name__ == '__main__':
    main()
