#!/usr/bin/env python3
"""Check runtime option compatibility against real Podman without creating containers."""
from pathlib import Path
import configparser
import os
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / 'roles/distrobox/files/podman-distrobox.py'
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))


class PodmanRuntimeTests(unittest.TestCase):
    def test_template_selects_exactly_one_network(self):
        for host_network in [True, False]:
            with self.subTest(host_network=host_network), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                ini = directory / 'distrobox.ini'
                play = directory / 'render.yml'
                play.write_text(yaml.safe_dump([{
                    'name': 'Render production Distrobox network configuration',
                    'hosts': 'localhost', 'gather_facts': False,
                    'vars': {'distrobox_container_name': 'network-fixture',
                             'distrobox_container_image': 'fixture-image',
                             'distrobox_use_host_network': host_network,
                             'distrobox_packages': [], 'distrobox_host_exec_commands': []},
                    'tasks': [{'ansible.builtin.template': {
                        'src': str(ROOT / 'roles/distrobox/templates/distrobox.ini.j2'),
                        'dest': str(ini), 'mode': '0600'}}],
                }]))
                env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'))
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)],
                                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                config = configparser.ConfigParser()
                config.read(ini)
                section = config['network-fixture']
                # Distrobox supplies host networking unless unshare_netns is set.
                networks = [] if section.getboolean('unshare_netns', fallback=False) else ['host']
                for flag in shlex.split(section['additional_flags']):
                    if flag.startswith('--network='):
                        networks.append(flag.split('=', 1)[1])
                self.assertEqual(networks, ['host' if host_network else 'pasta'])

    def test_helper_moves_runtime_first_and_drops_remote_settings(self):
        remote = {'CONTAINER_HOST': '', 'CONTAINER_CONNECTION': 'untrusted-fixture',
                  'CONTAINER_SSHKEY': '/fixture/key', 'DOCKER_HOST': 'tcp://fixture'}
        with mock.patch.dict(os.environ, remote), \
                mock.patch.object(sys, 'argv', [str(HELPER), 'create', '--runtime=crun', '--name', 'x']), \
                mock.patch('os.execv') as execv:
            runpy.run_path(str(HELPER), run_name='__main__')
            for name in remote:
                self.assertNotIn(name, os.environ)
        execv.assert_called_once_with('/usr/bin/podman', ['/usr/bin/podman', '--runtime=crun', 'create', '--name', 'x'])

    @unittest.skipUnless(shutil.which('podman') and Path('/usr/bin/podman').exists(), 'Native Podman unavailable')
    def test_real_global_runtime_option(self):
        for arguments in [['create', '--runtime=crun', '--help'], ['create', '--help']]:
            with self.subTest(arguments=arguments):
                env = dict(os.environ, CONTAINER_HOST='', CONTAINER_CONNECTION='untrusted-fixture', CONTAINER_SSHKEY='/fixture/key')
                result = subprocess.run([sys.executable, str(HELPER), *arguments], env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Create', result.stdout)


if __name__ == '__main__':
    unittest.main()
