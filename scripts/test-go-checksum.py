#!/usr/bin/env python3
"""Verify conservative Go checksum exceptions and supported prefix glob semantics."""
import importlib.util
from pathlib import Path
import unittest

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
        for pattern in ['', 'none', '*.redhat.com,*.engineering.redhat.com',
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
                ('*.redhat.com', 'gitlab.cee.redhat.com/owner/repo', True),
                ('github.com/private-owner', 'github.com/private-owner/repo', True),
                ('github.com/private-owner', 'github.com/private-owner-other/repo', False),
                ('github.com/*/cmd', 'github.com/owner/repo/cmd', False),
                ('github.com/*/*', 'github.com/owner', False)]:
            with self.subTest(pattern=pattern, target=target):
                self.assertEqual(policy.prefix_matches(pattern, target), expected)


if __name__ == '__main__':
    unittest.main()
