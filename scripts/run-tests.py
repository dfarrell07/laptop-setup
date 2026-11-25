#!/usr/bin/env python3
"""Run test commands in parallel and report each one; exit 1 if any failed.

Usage: run-tests.py [--jobs N] -- 'cmd one' 'cmd two' ...
Each command runs in its own shell with its output captured; a failure prints that command's full output. JOBS
(or --jobs) sets the parallelism, default the CPU count; JOBS=1 runs them one at a time. The test
suites use private temporary directories, so they are safe to run side by side.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import subprocess
import sys
import time


def split(command):
    """`python3 scripts/test-x.py` -> one command per test method, so a long suite spreads across CPUs."""
    parts = command.split()
    script = next((part for part in parts if part.endswith('.py')), None)
    if script is None:
        return [command]
    tree = ast.parse(open(script, encoding='utf-8').read())
    tests = [f'{node.name}.{item.name}' for node in tree.body if isinstance(node, ast.ClassDef)
             for item in node.body if isinstance(item, ast.FunctionDef) and item.name.startswith('test')]
    # Cross-check with unittest's own loader: tests inherited from another module are invisible to ast, and a
    # split that misses them would silently skip them, so such a suite runs whole.
    loader = ('import importlib.util, sys, unittest\n'
              'spec = importlib.util.spec_from_file_location("suite", sys.argv[1])\n'
              'module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n'
              'print(unittest.defaultTestLoader.loadTestsFromModule(module).countTestCases())')
    counted = subprocess.run([parts[0], '-c', loader, script], capture_output=True, text=True, check=False)
    if counted.returncode or counted.stdout.strip() != str(len(tests)):
        return [command]
    return [f'{command} {test}' for test in tests] or [command]


def run(command):
    start = time.monotonic()
    result = subprocess.run(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            check=False)
    return command, result.returncode, time.monotonic() - start, result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--jobs', type=int, default=int(os.environ.get('JOBS') or os.cpu_count() or 1))
    parser.add_argument('--split', action='append', default=[],
                        help='a test file whose test methods run as separate processes (repeatable)')
    parser.add_argument('commands', nargs='+')
    args = parser.parse_args()
    commands = []
    for command in args.commands:
        commands += split(command) if any(name in command for name in args.split) else [command]
    # The split pieces (from the slowest suites) start first; the rest keep their Makefile order.
    commands.sort(key=lambda command: not any(name in command for name in args.split))
    args.commands = commands
    # Under parallel load the Ansible-driven suites run slower; give their internal deadlines room unless set.
    os.environ.setdefault('LAPTOP_SETUP_TEST_TIMEOUT', '300')
    # ansible.cfg's scripts/vault-pass.sh becomes the YubiKey script after make setup-yubikeys: no suite may call it.
    # Make targets a suite runs still go through verify-ansible-args.sh, which sets its own value.
    os.environ['ANSIBLE_VAULT_PASSWORD_FILE'] = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                             'scripts', 'vault-pass-ci.sh')
    start = time.monotonic()
    failed = []
    total = 0.0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run, command) for command in args.commands]
        for future in as_completed(futures):
            command, code, seconds, output = future.result()
            total += seconds
            print(f'{"PASS" if code == 0 else "FAIL"} {seconds:6.1f}s  {command}', flush=True)
            if code:
                failed.append((command, output))
    for command, output in failed:
        print(f'\n===== FAILED: {command} =====\n{output}', flush=True)
    wall = time.monotonic() - start
    print(f'\n{len(args.commands) - len(failed)}/{len(args.commands)} passed in {wall:.0f}s wall '
          f'({total:.0f}s of test time, {args.jobs} jobs)')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
