#!/usr/bin/env python3
"""Exercise the production compiler for both persistent boot argument sources."""
import importlib.util
from pathlib import Path
import shlex
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('kernel_cmdline', ROOT / 'roles/system/files/kernel-cmdline.py')
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


class KernelCmdlineTests(unittest.TestCase):
    def settings(self, mode='integrity', processor='amd', free=True, passthrough=False):
        return {'lockdown': mode, 'amd': processor == 'amd', 'intel': processor == 'intel',
                'init_on_free': free, 'passthrough': passthrough, 'remove_luks_key': False,
                'luks_discards': False}

    def test_both_boot_sources_and_repeat(self):
        for mode, processor, free, passthrough in [('integrity', 'amd', True, False),
                                                 ('confidentiality', 'intel', True, True),
                                                 ('none', 'amd', False, False),
                                                 ('', 'intel', False, False)]:
            with self.subTest(mode=mode, processor=processor):
                settings = self.settings(mode, processor, free, passthrough)
                original = 'root=UUID=fixture ro rd.luks.uuid=preserve vsyscall=emulate init_on_free=0 lockdown=none'
                merged = compiler.merge_arguments(original, settings)
                arguments = shlex.split(merged)
                self.assertIn('root=UUID=fixture', arguments)
                self.assertIn('rd.luks.uuid=preserve', arguments)
                self.assertIn('vsyscall=none', arguments)
                self.assertNotIn('vsyscall=emulate', arguments)
                self.assertIn('page_alloc.shuffle=1', arguments)
                self.assertEqual([v for v in arguments if v.startswith('lockdown=')], ['lockdown=' + mode] if mode else [])
                self.assertIn(processor + '_iommu=on', arguments)
                self.assertEqual('iommu=pt' in arguments, passthrough)
                self.assertEqual('init_on_free=1' in arguments, free)
                if free:
                    self.assertNotIn('init_on_free=0', arguments)
                self.assertEqual(compiler.merge_arguments(merged, settings), merged)
                grub = '# retain site settings\nGRUB_TIMEOUT=5\nGRUB_CMDLINE_LINUX="' + original + '"\n'
                result = compiler.merge_grub(grub, settings)
                self.assertIn('GRUB_TIMEOUT=5', result)
                self.assertIn('GRUB_CMDLINE_LINUX="' + merged + '"', result)
                self.assertEqual(compiler.merge_grub(result, settings), result)

    def test_preserve_grub_regenerated_argument_order(self):
        text = ('root=UUID=fixture ro rootflags=subvol=root lockdown=integrity '
                'vsyscall=none init_on_free=1 page_alloc.shuffle=1 amd_iommu=on '
                'no_timer_check console=tty1 console=ttyS0,115200n8 rootflags=subvol=root')
        self.assertEqual(compiler.merge_arguments(text, self.settings()), text)
        changed = text.replace('lockdown=integrity', 'lockdown=none')
        restored = shlex.split(compiler.merge_arguments(changed, self.settings()))
        self.assertIn('lockdown=integrity', restored)
        self.assertNotIn('lockdown=none', restored)
        duplicate = text + ' lockdown=none'
        self.assertNotIn('lockdown=none', compiler.merge_arguments(duplicate, self.settings()))

    def test_preserve_quoted_values_and_variables(self):
        text = 'root=UUID=fixture foo="keep these spaces" $tuned_params rd.luks.key=/private/key'
        settings = self.settings()
        merged = compiler.merge_arguments(text, settings)
        self.assertIn('foo="keep these spaces"', merged)
        self.assertIn('$tuned_params', merged)
        self.assertIn('rd.luks.key=/private/key', merged)
        settings['remove_luks_key'] = True
        self.assertNotIn('rd.luks.key=', compiler.merge_arguments(text, settings))
        for malformed in ['foo="unterminated', 'foo=bare\\']:
            with self.assertRaises(ValueError):
                compiler.merge_arguments(malformed, settings)

    def test_luks_discards_follow_policy(self):
        text = 'root=UUID=fixture rd.luks.allow-discards rd.luks.options=discard rd.luks.options=tpm2-device=auto'
        settings = self.settings()
        merged = shlex.split(compiler.merge_arguments(text, settings))
        self.assertNotIn('rd.luks.allow-discards', merged)
        self.assertNotIn('rd.luks.options=discard', merged)
        self.assertIn('rd.luks.options=tpm2-device=auto', merged)
        settings['luks_discards'] = True
        kept = shlex.split(compiler.merge_arguments(text, settings))
        self.assertIn('rd.luks.allow-discards', kept)
        self.assertIn('rd.luks.options=discard', kept)

    def test_luks_discard_removed_from_option_lists(self):
        settings = self.settings()
        merged = shlex.split(compiler.merge_arguments(
            'root=UUID=fixture rd.luks.options=discard,tpm2-device=auto', settings))
        self.assertIn('rd.luks.options=tpm2-device=auto', merged)
        merged = shlex.split(compiler.merge_arguments(
            'root=UUID=fixture rd.luks.options=tpm2-device=auto,discard,x', settings))
        self.assertIn('rd.luks.options=tpm2-device=auto,x', merged)
        merged = shlex.split(compiler.merge_arguments(
            'root=UUID=fixture rd.luks.options=discard,discard', settings))
        self.assertFalse([t for t in merged if t.startswith('rd.luks.options')])
        settings['luks_discards'] = True
        kept = shlex.split(compiler.merge_arguments(
            'root=UUID=fixture rd.luks.options=discard,tpm2-device=auto', settings))
        self.assertIn('rd.luks.options=discard,tpm2-device=auto', kept)

    def test_summary_redacts_key_references(self):
        before = 'root=UUID=fixture rd.luks.key=uuid=/keyfile:UUID=other rd.luks.allow-discards'
        after = compiler.merge_arguments(before, dict(self.settings(), remove_luks_key=True))
        summary = compiler.summarize(before, after)
        self.assertIn('rd.luks.key=<redacted>', summary['removed'])
        self.assertIn('rd.luks.allow-discards', summary['removed'])
        self.assertIn('lockdown=integrity', summary['added'])
        self.assertNotIn('keyfile', ' '.join(summary['added'] + summary['removed']))

    def test_reject_unquoted_or_duplicate_grub_sources(self):
        for value in ['GRUB_CMDLINE_LINUX=bare\n', 'GRUB_CMDLINE_LINUX="a"\nGRUB_CMDLINE_LINUX="b"\n']:
            with self.assertRaises(ValueError):
                compiler.merge_grub(value, self.settings())


if __name__ == '__main__':
    unittest.main()
