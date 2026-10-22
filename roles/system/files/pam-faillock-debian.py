#!/usr/bin/env python3
"""Insert pam_faillock preauth/authfail into a Debian common-auth, keeping jumps valid.

Debian stacks use numeric jumps ("[success=1 default=ignore] pam_unix.so") that count the
auth rules that follow. Inserting a rule inside a jump's range must widen the jump, or a
successful pam_unix lands on pam_deny and every login fails. Existing faillock rules are
first removed with the inverse adjustment, so a run on an already-converted (or an earlier,
miscounted) file is idempotent and self-healing.

Usage: pam-faillock-debian.py SRC DST   (exit 0; prints "changed" when DST differs from SRC)
"""
import re
import sys

PREAUTH = 'auth    required                        pam_faillock.so preauth'
AUTHFAIL = 'auth    [default=die]                  pam_faillock.so authfail'
CONTROL = re.compile(r'^(-?auth\s+)\[([^\]]*)\](.*)$')


def is_rule(line):
    # '-auth' (module optional if missing) still counts as a rule for numeric jumps
    return line.lstrip('-').startswith('auth')


def adjust(lines, at, delta, keep_if_landing_on=None):
    """Shift numeric jumps of auth rules before rule position `at` whose skip range covers it.

    A jump "[x=N]" at rule p skips rules p+1..p+N. keep_if_landing_on leaves jumps that land
    on a matching rule unchanged (used to undo an earlier insertion that never widened them).
    """
    rules = [i for i, line in enumerate(lines) if is_rule(line)]
    for pos, index in enumerate(rules):
        if pos >= at:
            break
        match = CONTROL.match(lines[index])
        if not match:
            continue

        def bump(m):
            value = int(m.group(2))
            if not pos < at <= pos + value:
                return m.group(0)
            landing = pos + value + 1
            if (keep_if_landing_on and landing < len(rules)
                    and keep_if_landing_on in lines[rules[landing]]):
                return m.group(0)
            return m.group(1) + str(value + delta)

        controls = re.sub(r'(\w+=)(\d+)', bump, match.group(2))
        lines[index] = f'{match.group(1)}[{controls}]{match.group(3)}'


def rule_position(lines, index):
    return sum(1 for line in lines[:index] if is_rule(line))


def convert(text):
    lines = text.split('\n')
    while True:  # remove existing faillock rules, narrowing jumps that crossed them
        found = next((i for i, line in enumerate(lines)
                      if is_rule(line) and 'pam_faillock.so' in line), None)
        if found is None:
            break
        position = rule_position(lines, found)
        # A jump that (with the faillock rule present) lands on pam_deny was never widened for
        # it (an earlier version of this conversion skipped that); leave such jumps alone.
        adjust(lines, position, -1, keep_if_landing_on='pam_deny.so')
        del lines[found]
    unix = next((i for i, line in enumerate(lines) if is_rule(line) and 'pam_unix.so' in line), None)
    first = next((i for i, line in enumerate(lines) if is_rule(line)), None)
    if unix is None or first is None:
        raise SystemExit('no auth/pam_unix rule found')
    # authfail goes after the whole primary block (pam_unix, pam_sss, ...), right before the
    # pam_deny fallback: placing it directly after pam_unix would deny SSSD users whose
    # password pam_unix cannot check before pam_sss runs.
    deny = next((i for i, line in enumerate(lines)
                 if i > unix and is_rule(line) and 'pam_deny.so' in line), unix + 1)
    lines.insert(deny, AUTHFAIL)
    adjust(lines, rule_position(lines, deny), +1)
    lines.insert(first, PREAUTH)  # nothing precedes the first rule, so no jump to widen
    return '\n'.join(lines)


def main(src, dst):
    text = open(src).read()
    result = convert(text)
    with open(dst, 'w') as handle:
        handle.write(result)
    if result != text:
        print('changed')


if __name__ == '__main__':
    main(*sys.argv[1:3])
