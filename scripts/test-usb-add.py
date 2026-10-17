#!/usr/bin/env python3
"""Unit tests for scripts/usb-add.py (make usb-add)."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('usb_add', ROOT / 'scripts/usb-add.py')
usb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usb)

LINE = ('7: block id 3297:1969 serial "" name "Moonlander Mark I" hash "abc=" parent-hash "def=" '
        'via-port "3-1" with-interface { 03:01:01 03:00:00 } with-connect-type "hotplug"')


class UsbAddTests(unittest.TestCase):
    def test_blocked_line_becomes_id_name_interface_rule(self):
        device = usb.parse_device_line(LINE)
        self.assertEqual(device['number'], 7)
        self.assertEqual(usb.build_rule(device),
                         'allow id 3297:1969 name "Moonlander Mark I" with-interface { 03:01:01 03:00:00 }')

    def test_unsafe_or_empty_names_are_dropped(self):
        for name in ('', 'a"b', 'x{{y', 'tab\there', 'café', 'pct%'):
            self.assertEqual(usb.safe_name(name), '')
        device = usb.parse_device_line('2: block id 10ab:9309 serial "" name "" with-interface e0:01:01')
        self.assertEqual(usb.build_rule(device), 'allow id 10ab:9309 with-interface e0:01:01')
        self.assertIsNone(usb.parse_device_line('garbage'))

    def test_wildcard_ids_cover_devices(self):
        rules = ['allow id 1050:* name "YubiKey*"', 'allow with-connect-type "hardwired"']
        self.assertTrue(usb.covered({'id': '1050:0407'}, rules))
        self.assertFalse(usb.covered({'id': '3297:1969'}, rules))

    def test_sysfs_devices_read_with_interfaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev = root / '3-1'
            dev.mkdir()
            (dev / 'idVendor').write_text('3297\n')
            (dev / 'idProduct').write_text('1969\n')
            (dev / 'product').write_text('Moonlander Mark I\n')
            for n, sub in ((0, '01'), (1, '00')):
                iface = root / ('3-1:1.%d' % n)
                iface.mkdir()
                (iface / 'bInterfaceClass').write_text('03\n')
                (iface / 'bInterfaceSubClass').write_text(sub + '\n')
                (iface / 'bInterfaceProtocol').write_text('01\n')
            (dev / ('3-1:1.0')).symlink_to(root / '3-1:1.0')
            (dev / ('3-1:1.1')).symlink_to(root / '3-1:1.1')
            devices = usb.read_sysfs(root)
        self.assertEqual([usb.build_rule(d) for d in devices],
                         ['allow id 3297:1969 name "Moonlander Mark I" with-interface { 03:00:01 03:01:01 }'])

    def test_config_edits_keep_comments_and_order(self):
        entries = [('allow id 1234:5678 name "It\'s"', 'It, added')]
        block = ('a: 1\n' + usb.KEY + ':\n  - "allow id 1:2"  # first\n  # note\n\nb: 2\n')
        out = usb.add_rules_to_config(block, entries)
        self.assertIn('  # note\n  - \'allow id 1234:5678 name "It\'\'s"\'  # It, added\n\nb: 2\n', out)
        self.assertEqual(usb.load_yaml(out)[usb.KEY], ['allow id 1:2', 'allow id 1234:5678 name "It\'s"'])
        self.assertEqual(usb.load_yaml(out)['b'], 2)
        empty = usb.add_rules_to_config('a: 1\n' + usb.KEY + ': []  # none\nb: 2', entries)
        self.assertEqual(usb.load_yaml(empty)[usb.KEY], ['allow id 1234:5678 name "It\'s"'])
        missing = usb.add_rules_to_config('a: 1', entries)
        self.assertEqual(usb.load_yaml(missing), {'a': 1, usb.KEY: ['allow id 1234:5678 name "It\'s"']})
        with self.assertRaises(ValueError):
            usb.add_rules_to_config(usb.KEY + ': ["x"]\n', entries)

    def test_unsafe_tag_config_still_parses(self):
        text = 'dotfiles_goprivate: !unsafe "a,b"\n' + usb.KEY + ':\n  - "allow id 1:2"\n'
        out = usb.add_rules_to_config(text, [('allow id 3:4', 'x')])
        self.assertEqual(usb.load_yaml(out)[usb.KEY], ['allow id 1:2', 'allow id 3:4'])

    def test_main_without_usbguard_saves_uncovered_devices_only(self):
        devices = [
            {'number': None, 'target': 'present', 'id': '1d6b:0002', 'name': 'xHCI Host Controller', 'interfaces': ''},
            {'number': None, 'target': 'present', 'id': '10ab:9309', 'name': '', 'interfaces': 'e0:01:01'},
            {'number': None, 'target': 'present', 'id': '2109:0817', 'name': 'USB3.0 Hub', 'interfaces': '09:00:00'},
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yml'
            config.write_text(usb.KEY + ':\n  - "allow id 10ab:9309"\n')
            config.chmod(0o600)
            with mock.patch.object(usb, 'usbguard_active', return_value=False), \
                    mock.patch.object(usb, 'read_sysfs', return_value=devices), \
                    mock.patch('builtins.print'):
                self.assertEqual(usb.main(['--config', str(config), '--yes']), 0)
            rules = usb.load_yaml(config.read_text())[usb.KEY]
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(rules, ['allow id 10ab:9309',
                                 'allow id 2109:0817 name "USB3.0 Hub" with-interface 09:00:00'])

    def test_main_with_usbguard_allows_blocked_devices_and_hubs_children(self):
        rounds = [[usb.parse_device_line('3: block id 2109:0817 name "USB3.0 Hub" with-interface 09:00:00')],
                  [usb.parse_device_line(LINE)], []]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yml'
            config.write_text('a: 1\n')
            with mock.patch.object(usb, 'usbguard_active', return_value=True), \
                    mock.patch.object(usb, 'blocked_devices', side_effect=rounds), \
                    mock.patch.object(usb.subprocess, 'run') as run, \
                    mock.patch.object(usb.time, 'sleep'), \
                    mock.patch('builtins.print'):
                self.assertEqual(usb.main(['--config', str(config), '--yes']), 0)
            rules = usb.load_yaml(config.read_text())[usb.KEY]
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [['sudo', 'usbguard', 'allow-device', '3'], ['sudo', 'usbguard', 'allow-device', '7']])
        self.assertEqual(len(rules), 2)

    def test_declining_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.yml'
            config.write_text('a: 1\n')
            device = {'number': None, 'target': 'present', 'id': '2109:0817', 'name': '', 'interfaces': ''}
            with mock.patch.object(usb, 'usbguard_active', return_value=False), \
                    mock.patch.object(usb, 'read_sysfs', return_value=[device]), \
                    mock.patch('builtins.input', return_value='n'), \
                    mock.patch('builtins.print'):
                usb.main(['--config', str(config)])
            self.assertEqual(config.read_text(), 'a: 1\n')


if __name__ == '__main__':
    unittest.main()
