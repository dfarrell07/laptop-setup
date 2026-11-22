#!/usr/bin/env python3
"""Verify conservative Go checksum exceptions and supported prefix glob semantics."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('go_checksum', ROOT / 'roles/packages/filter_plugins/go_checksum.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
TOOLS = ['github.com/submariner-io/subctl/cmd@v0.24.0',
         'golang.org/x/tools/gopls@v0.23.0', 'mvdan.cc/gofumpt@v0.11.0']


class GoChecksumTests(unittest.TestCase):
    def test_public_host_and_owner_globs_are_rejected(self):
        for pattern in ['*', '*.*', 'github.*', 'github.com', 'github.com/',
                        'github.com/*', 'github.com/*/tool', 'golang.org/*',
                        'k8s.io/', 'corp.invalid,github.com/*']:
            with self.subTest(pattern=pattern):
                self.assertFalse(policy.go_checksum_exceptions_safe(pattern, TOOLS))

    def test_private_domains_and_literal_owners_are_allowed(self):
        for pattern in ['', 'none', '*.redhat.com,*.example.internal',
                        'corp.invalid/', 'github.com/private-owner',
                        'github.com/private-owner/*', 'github.com/private-owner/project']:
            with self.subTest(pattern=pattern):
                self.assertTrue(policy.go_checksum_exceptions_safe(pattern, TOOLS))

    def test_installed_public_targets_remain_protected(self):
        for pattern in ['github.com/submariner-io', 'github.com/submariner-io/subctl',
                        'golang.org/x/tools/*', 'mvdan.cc/gofumpt']:
            with self.subTest(pattern=pattern):
                self.assertFalse(policy.go_checksum_exceptions_safe(pattern, TOOLS))

    def test_public_project_dependencies_remain_protected(self):
        for pattern in ['golang.org/x/mod', 'k8s.io/apimachinery', 'sigs.k8s.io/json',
                        'mvdan.cc/xurls/v2', 'gopkg.in/yaml.v3']:
            with self.subTest(pattern=pattern):
                self.assertFalse(policy.go_checksum_exceptions_safe(pattern, TOOLS))

    def test_unsupported_globs_are_rejected(self):
        for pattern in ['github.com/[a-z]*', 'github.co?', r'github\.com',
                        'corp.invalid\n', ' corp.invalid', None]:
            with self.subTest(pattern=pattern):
                self.assertFalse(policy.go_checksum_exceptions_safe(pattern, TOOLS))

    def test_prefix_globs_do_not_cross_components(self):
        for pattern, target, expected in [
                ('github.com/*', 'github.com/owner/repo', True),
                ('github.*', 'github.com/owner/repo', True),
                ('*.example.internal', 'git.example.internal/owner/repo', True),
                ('github.com/private-owner', 'github.com/private-owner/repo', True),
                ('github.com/private-owner', 'github.com/private-owner-other/repo', False),
                ('github.com/*/cmd', 'github.com/owner/repo/cmd', False),
                ('github.com/*/*', 'github.com/owner', False)]:
            with self.subTest(pattern=pattern, target=target):
                self.assertEqual(policy.prefix_matches(pattern, target), expected)


    def test_distrobox_copy_is_identical(self):
        self.assertEqual((ROOT / 'roles/packages/filter_plugins/go_checksum.py').read_bytes(),
                         (ROOT / 'roles/distrobox/filter_plugins/go_checksum.py').read_bytes())


class GoToolGuardTests(unittest.TestCase):
    """install_go_tools.yml rebuilds only the tools whose installed build info differs from the pin."""

    def test_only_missing_or_outdated_tools_are_rebuilt(self):
        tasks = yaml.safe_load((ROOT / 'roles/packages/tasks/install_go_tools.yml').read_text())
        wanted = ['Read the current Go toolchain version', 'Read the build info of installed Go tools',
                  'Start an empty list of Go tools to build',
                  'List Go tools whose installed build differs from the pin or predates the toolchain']
        picked = [task for task in tasks if task.get('name') in wanted]
        self.assertEqual([task['name'] for task in picked], wanted)
        info = {  # what `go version -m` prints for each installed binary
            'gofumpt': 'x: go1.25.3-X:nodwarf5\n\tpath\tmvdan.cc/gofumpt\n\tmod\tmvdan.cc/gofumpt\tv0.11.0\th1:x=\n',
            'golangci-lint': ('x: go1.25.3\n\tpath\tgithub.com/golangci/golangci-lint/v2/cmd/golangci-lint\n'
                              '\tmod\tgithub.com/golangci/golangci-lint/v2\tv2.12.1\th1:x=\n'),
            'subctl': ('x: go1.26.0\n\tpath\tgithub.com/submariner-io/subctl/cmd\n'
                       '\tmod\tgithub.com/submariner-io/subctl\tv0.24.0\th1:x=\n'),
            'stern': 'x: go1.24.9\n\tpath\tgithub.com/stern/stern\n\tmod\tgithub.com/stern/stern\tv1.34.0\th1:x=\n',
        }
        tools = ['mvdan.cc/gofumpt@v0.11.0',  # current
                 'github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.12.2',  # outdated
                 'github.com/submariner-io/subctl/cmd@v0.24.0',  # current (newer Go is fine), installed as subctl
                 'golang.org/x/tools/gopls@v0.23.0',  # not installed
                 'github.com/stern/stern@v1.34.0']  # current pin, built by a Go older than the toolchain
        with tempfile.TemporaryDirectory() as name:
            home = Path(name)
            (home / 'go/bin').mkdir(parents=True)
            stubs = home / 'stubs'
            stubs.mkdir()
            for tool, text in info.items():
                (home / 'go/bin' / tool).write_text(text)
            (stubs / 'go').write_text('#!/bin/sh\n[ "$1 $2" = "env GOVERSION" ] && exec echo go1.25.3-X:nodwarf5\n'
                                      '[ "$1 $2" = "version -m" ] && [ -f "$3" ] && exec cat "$3"\nexit 1\n')
            (stubs / 'go').chmod(0o755)
            out = home / 'todo.json'
            play = [{'hosts': 'localhost', 'gather_facts': False,
                     'vars': {'ansible_facts': {'user_dir': name}, 'profile': 'work', 'packages_go_install': [],
                              'packages_go_install_work': tools, '_golang_available': True,
                              '_packages_subctl_go_modules': ['github.com/submariner-io/subctl/cmd@v0.24.0']},
                     'tasks': picked + [{'name': 'Save', 'ansible.builtin.copy': {
                         'content': '{{ _packages_go_todo | to_json }}', 'dest': str(out), 'mode': '0600'}}]}]
            (home / 'play.yml').write_text(yaml.safe_dump(play))
            ansible = ROOT / '.venv/bin/ansible-playbook'
            if not ansible.exists():
                ansible = Path(shutil.which('ansible-playbook'))
            result = subprocess.run([str(ansible), '-i', 'localhost,', '-c', 'local', str(home / 'play.yml')],
                                    capture_output=True, text=True, timeout=180,
                                    env=dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", ANSIBLE_NOCOLOR='1'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(out.read_text()), [tools[1], tools[3], tools[4]])


if __name__ == '__main__':
    unittest.main()
