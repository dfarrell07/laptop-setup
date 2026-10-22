#!/usr/bin/env python3
"""Evaluate Debian common-auth after the faillock conversion (roles/system/files/pam-faillock-debian.py)."""
import importlib.util
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('pam_faillock', ROOT / 'roles/system/files/pam-faillock-debian.py')
CONVERT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONVERT)

STOCK = ('# primary block\n'
         'auth\t[success=1 default=ignore]\tpam_unix.so nullok\n'
         'auth\trequisite\t\t\tpam_deny.so\n'
         'auth\trequired\t\t\tpam_permit.so\n')
SSSD = ('auth\t[success=2 default=ignore]\tpam_unix.so nullok\n'
        'auth\t[success=1 default=ignore]\tpam_sss.so use_first_pass\n'
        'auth\trequisite\t\t\tpam_deny.so\n'
        'auth\trequired\t\t\tpam_permit.so\n')
# '-auth' (optional module) lines still count in numeric jumps
SSSD_OPTIONAL = ('auth\t[success=2 default=ignore]\tpam_unix.so nullok\n'
                 '-auth\t[success=1 default=ignore]\tpam_sss.so use_first_pass\n'
                 'auth\trequisite\t\t\tpam_deny.so\n'
                 'auth\trequired\t\t\tpam_permit.so\n')
OLD_AWK = ('auth    required                        pam_faillock.so preauth\n'
           'auth\t[success=1 default=ignore]\tpam_unix.so nullok\n'
           'auth    [default=die]                  pam_faillock.so authfail\n'
           'auth\trequisite\t\t\tpam_deny.so\n'
           'auth\trequired\t\t\tpam_permit.so\n')


def outcome(text, succeeds):
    """Simplified PAM walk: modules in `succeeds` (and pam_faillock) return success, others fail."""
    rules = [line for line in text.splitlines() if line.lstrip('-').startswith('auth')]
    pos = 0
    while pos < len(rules):
        rule = rules[pos]
        module = re.search(r'(pam_\w+)\.so', rule).group(1)
        if 'pam_faillock.so authfail' in rule:
            return 'locked-out-path'  # [default=die]
        if module == 'pam_deny':
            return 'denied'
        if module == 'pam_permit':
            return 'permitted'
        bracket = re.search(r'\[([^\]]*)\]', rule)
        ok = module in succeeds or module == 'pam_faillock'
        if bracket:
            actions = dict(item.split('=') for item in bracket.group(1).split())
            action = actions.get('success' if ok else 'default', actions.get('default'))
            if action and action.isdigit():
                pos += int(action) + 1
                continue
        pos += 1
    return 'fell-through'


class PamFaillockDebianTests(unittest.TestCase):
    def check(self, source, users):
        converted = CONVERT.convert(source)
        self.assertEqual(CONVERT.convert(converted), converted, 'not idempotent')
        self.assertLess(converted.index('preauth'), converted.index('pam_unix'))
        # the walker ignores plain control words, so pin them: a 'sufficient' preauth would skip the stack
        self.assertRegex(converted, r'(?m)^auth\s+required\s+pam_faillock\.so preauth$')
        self.assertRegex(converted, r'(?m)^auth\s+\[default=die\]\s+pam_faillock\.so authfail$')
        for module in users:
            self.assertEqual(outcome(converted, {module}), 'permitted', module)
        self.assertEqual(outcome(converted, set()), 'locked-out-path')
        return converted

    def test_stock_debian_stack(self):
        self.check(STOCK, ['pam_unix'])

    def test_sssd_users_reach_pam_sss_before_authfail(self):
        converted = self.check(SSSD, ['pam_unix', 'pam_sss'])
        self.assertLess(converted.index('pam_sss'), converted.index('authfail'))

    def test_optional_sss_module_keeps_its_jump(self):
        converted = self.check(SSSD_OPTIONAL, ['pam_unix', 'pam_sss'])
        self.assertIn('-auth\t[success=2 default=ignore]\tpam_sss.so', converted)

    def test_file_from_the_old_awk_conversion_is_repaired(self):
        self.assertEqual(outcome(OLD_AWK, {'pam_unix'}), 'denied')  # the original bug
        self.check(OLD_AWK, ['pam_unix'])


if __name__ == '__main__':
    unittest.main()
