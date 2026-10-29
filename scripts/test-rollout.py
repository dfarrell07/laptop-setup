#!/usr/bin/env python3
"""Verify the chunk registry and scripts/rollout.py: shape, order, enabled-list resolution, CLI."""
import atexit
import importlib.util
import json
import os
import pty
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('rollout', ROOT / 'scripts/rollout.py')
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)

CATALOG, DEFAULT = rollout.load_catalog()


def closure_of(name):
    return rollout.closure(CATALOG, name)


def config(text):
    handle = tempfile.NamedTemporaryFile('w', suffix='.yml', delete=False)
    handle.write(text)
    handle.close()
    atexit.register(os.unlink, handle.name)
    return handle.name


ANSIBLE_TMP = tempfile.mkdtemp(prefix='rollout-test-ansible-')  # keep ansible out of the real ~/.ansible
ANSIBLE_ENV = {'ANSIBLE_LOCAL_TEMP': ANSIBLE_TMP, 'ANSIBLE_REMOTE_TEMP': ANSIBLE_TMP}


class RegistryTests(unittest.TestCase):
    def test_default_enables_everything(self):
        self.assertEqual(DEFAULT, 'all')

    def test_requires_are_listed_earlier(self):
        # load_catalog enforces this; a registry that violates it must not load
        broken = config('rollout_catalog:\n  a: {desc: x, risk: low, requires: [b]}\n  b: {desc: y, risk: low}\n')
        with self.assertRaises(rollout.RolloutError):
            rollout.load_catalog(broken)

    def test_higher_risk_never_precedes_lower_risk_it_does_not_depend_on(self):
        order = ['low', 'medium', 'high']
        names = list(CATALOG)
        for i, name in enumerate(names):
            for later in names[i + 1:]:
                if order.index(CATALOG[later]['risk']) < order.index(CATALOG[name]['risk']):
                    self.assertIn(name, closure_of(later), f'{later} ({CATALOG[later]["risk"]}) is listed '
                                  f'after {name} ({CATALOG[name]["risk"]}); order is the recommended order')

    def test_verify_names_exist_in_smoke_test_and_can_pass(self):
        script = (ROOT / 'scripts/smoke-test.sh').read_text()
        for name, entry in CATALOG.items():
            for check in entry.get('verify', []):
                with self.subTest(chunk=name, check=check):
                    if re.search(r'^\s*_ug_chk ' + re.escape(check) + ' ', script, re.M):
                        continue  # recorded by the _ug_chk helper, which records PASS generically
                    self.assertRegex(script, r'record "' + re.escape(check) + r'"')
                    # --only treats anything but PASS as unverified: a check that can never PASS is useless here
                    self.assertRegex(script, r'record [\'"]' + re.escape(check) + r'[\'"] [\'"]PASS[\'"]')

    def test_ssh_client_verify_checks_what_the_ssh_role_writes(self):
        # ~/.ssh/config belongs to the dotfiles role; an ssh_client check on it passes or skips whatever ssh_client did
        checks = CATALOG['ssh_client']['verify']
        self.assertIn('known-hosts-github', checks)
        self.assertFalse([c for c in checks if c.startswith('ssh-config')], checks)


class EnabledTests(unittest.TestCase):
    def test_all_skips_nothing(self):
        enabled = rollout.enabled_chunks(CATALOG, 'all')
        self.assertEqual(rollout.skip_tags(CATALOG, enabled), [])

    def test_subset_skips_the_complement_in_registry_order(self):
        enabled = rollout.enabled_chunks(CATALOG, ['ssh_client', 'packages'])
        self.assertEqual(enabled, ['packages', 'ssh_client'])
        self.assertEqual(rollout.skip_tags(CATALOG, enabled), [n for n in CATALOG if n not in enabled])

    def test_empty_list_enables_nothing(self):
        self.assertEqual(rollout.enabled_chunks(CATALOG, []), [])

    def test_bad_values_fail_closed(self):
        for bad in ['All', 'none', True, 5, None, {'a': 1}, ['dotfiles', 7]]:
            with self.subTest(value=bad), self.assertRaises(rollout.RolloutError):
                rollout.enabled_chunks(CATALOG, bad)

    def test_unknown_chunk_gets_a_suggestion(self):
        with self.assertRaises(rollout.RolloutError) as ctx:
            rollout.enabled_chunks(CATALOG, ['packges'])
        self.assertIn("did you mean 'packages'", str(ctx.exception))

    def test_duplicates_rejected(self):
        with self.assertRaises(rollout.RolloutError):
            rollout.enabled_chunks(CATALOG, ['packages', 'packages'])

    def test_prerequisites_are_never_implied(self):
        with self.assertRaises(rollout.RolloutError) as ctx:
            rollout.enabled_chunks(CATALOG, ['agents'])
        self.assertIn('agents needs packages', str(ctx.exception))


class CliTests(unittest.TestCase):
    def run_cli(self, command, text=None):
        args = [sys.executable, str(ROOT / 'scripts/rollout.py'), command]
        if text is not None:
            args += ['--config', config(text)]
        else:
            args += ['--config', '/nonexistent/config.yml']
        return subprocess.run(args, capture_output=True, text=True)

    def test_missing_config_means_all(self):
        result = self.run_cli('skip-tags')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, ''))

    def test_skip_tags_for_a_list(self):
        result = self.run_cli('skip-tags', 'rollout_enable: [packages, ssh_client]\n')
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('packages', result.stdout.strip().split(','))
        self.assertIn('login_shell', result.stdout.strip().split(','))

    def test_invalid_config_exits_2_with_one_message(self):
        result = self.run_cli('check', 'rollout_enable: [packges]\n')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.count('ERROR'), 1)
        self.assertIn("did you mean 'packages'", result.stderr)

    def test_status_lists_every_chunk(self):
        result = self.run_cli('status', 'rollout_enable: [packages]\n')
        self.assertEqual(result.returncode, 0)
        for name in CATALOG:
            self.assertIn(name, result.stdout)
        self.assertIn('make all applies only the ON chunks (1 of', result.stdout)


class PlayZeroAssertTests(unittest.TestCase):
    """Run the real chunk-selection tasks of pre-flight.yml with and without --skip-tags."""

    TASKS = ['Resolve disabled chunks', 'Chunk selection is valid and every disabled chunk is skipped']

    def run_play(self, enable, skip):
        pre = yaml.safe_load((ROOT / 'pre-flight.yml').read_text())[0]['tasks']
        tasks = [t for t in pre if t.get('name') in self.TASKS]
        self.assertEqual(len(tasks), 2)
        with tempfile.TemporaryDirectory() as directory:
            play = Path(directory) / 'play.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                                             'vars_files': [str(ROOT / 'group_vars/all/rollout.yml')],
                                             'tasks': tasks}]))
            ansible = ROOT / '.venv/bin/ansible-playbook'
            command = [str(ansible) if ansible.exists() else shutil.which('ansible-playbook'),
                       '-i', 'localhost,', '-c', 'local', str(play),
                       '-e', json.dumps({'rollout_enable': enable})]  # -e outranks vars_files, like config.yml
            if skip:
                command += ['--skip-tags', ','.join(skip)]
            return subprocess.run(command, cwd=directory, capture_output=True, text=True,
                                  env=dict(os.environ, **ANSIBLE_ENV)).returncode

    def test_default_passes_without_skip_tags(self):
        self.assertEqual(self.run_play('all', []), 0)

    def test_list_passes_with_the_complement_skipped(self):
        enabled = rollout.enabled_chunks(CATALOG, ['packages', 'ssh_client'])
        self.assertEqual(self.run_play(['packages', 'ssh_client'], rollout.skip_tags(CATALOG, enabled)), 0)

    def test_disabled_chunk_without_skip_tags_fails_closed(self):
        self.assertNotEqual(self.run_play(['packages'], []), 0)

    def test_partial_skip_fails_closed(self):
        enabled = rollout.enabled_chunks(CATALOG, ['packages'])
        self.assertNotEqual(self.run_play(['packages'], rollout.skip_tags(CATALOG, enabled)[:-1]), 0)

    def test_bad_values_fail_closed(self):
        for bad in ['All', 'none', ['nosuchchunk'], {'packages': True}, 5]:
            with self.subTest(value=bad):
                self.assertNotEqual(self.run_play(bad, []), 0)


class TagTests(unittest.TestCase):
    """Chunk names are real tags on the real playbook (ansible-playbook --list-tasks)."""

    @classmethod
    def setUpClass(cls):
        ansible = ROOT / '.venv/bin/ansible-playbook'
        env = dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh'),
                   ANSIBLE_COLLECTIONS_PATH=str(ROOT / 'collections'), **ANSIBLE_ENV)
        result = subprocess.run([str(ansible) if ansible.exists() else shutil.which('ansible-playbook'),
                                 '-i', 'inventory/localhost.yml', 'site.yml', '--list-tasks'],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr
        cls.tasks = []
        for line in result.stdout.splitlines():
            match = re.match(r'^\s{6}(.*)\tTAGS: \[(.*)\]$', line)
            if match:
                cls.tasks.append((match.group(1), {t.strip() for t in match.group(2).split(',') if t.strip()}))

    def tagged(self, chunk):
        return [name for name, tags in self.tasks if chunk in tags]

    def test_every_chunk_tags_at_least_one_task(self):
        for chunk in CATALOG:
            with self.subTest(chunk=chunk):
                self.assertTrue(self.tagged(chunk), f'no task carries the tag {chunk}; skipping it would do nothing')

    def test_intra_role_features_stay_inside_their_role(self):
        for chunk, role in {'login_shell': 'desktop', 'ssh_authorized_keys': 'ssh'}.items():
            with self.subTest(chunk=chunk):
                names = self.tagged(chunk)
                self.assertTrue(names)
                self.assertTrue(all(name.startswith(f'{role} : ') for name in names), names)

    UNTAGGED_SYSTEM_TASKS = {
        'system : Gather service facts',
        'system : Ensure policycoreutils-python-utils is installed',
        'system : Apply pending handlers (hardening core)',
        'system : Apply pending handlers (GRUB)',
        'system : Apply pending handlers (auditd)',
        'system : Apply pending handlers (chrony)',
    }

    def test_system_tasks_belong_to_a_chunk_or_are_allowed_prerequisites(self):
        # A new untagged system task would run for every chunk selection; make that a conscious decision.
        for name, tags in self.tasks:
            if name.startswith('system : ') and 'always' not in tags and not any(t.startswith('sys_') for t in tags):
                self.assertIn(name, self.UNTAGGED_SYSTEM_TASKS, name)

    def test_chunk_selection_keeps_the_chunks_own_tasks_and_no_other_chunks(self):
        # Emulates ansible's rule (skip wins; --tags selects tagged and always tasks) on the real tag sets.
        for chunk in CATALOG:
            with self.subTest(chunk=chunk):
                args = rollout.chunk_args(CATALOG, list(CATALOG), chunk)
                positive = set(args[args.index('--tags') + 1].split(','))
                skip = set(args[args.index('--skip-tags') + 1].split(',')) if '--skip-tags' in args else set()
                selected = [(n, t) for n, t in self.tasks if (t & positive or 'always' in t) and not t & skip]
                own = [n for n, t in selected if chunk in t]
                self.assertTrue(own, f'selecting {chunk} would run none of its tasks')
                others = {c for n, t in selected for c in t if c in CATALOG and c != chunk}
                self.assertTrue(others <= closure_of(chunk), f'{chunk} selection also runs {others - closure_of(chunk)}')

    def test_selection_tasks_are_not_themselves_skippable(self):
        for name, tags in self.tasks:
            if name.startswith(('Resolve disabled chunks', 'Chunk selection')):
                self.assertEqual(tags, {'always'}, name)


class ChunkArgsTests(unittest.TestCase):
    ENABLED = list(CATALOG)

    def args(self, name):
        return rollout.chunk_args(CATALOG, self.ENABLED, name)

    def skipped(self, name):
        args = self.args(name)
        return args[args.index('--skip-tags') + 1].split(',')

    def test_no_sudo_chunk_selects_its_tag_and_skips_become(self):
        args = self.args('dotfiles')
        self.assertEqual(args[:2], ['--tags', 'common,dotfiles'])
        self.assertIn('become', self.skipped('dotfiles'))
        self.assertNotIn('--ask-become-pass', args)

    def test_sudo_chunk_asks_for_the_password(self):
        args = self.args('packages')
        self.assertIn('--ask-become-pass', args)
        self.assertNotIn('become', self.skipped('packages'))

    def test_every_other_chunk_is_skipped(self):
        skipped = self.skipped('agents')
        self.assertNotIn('agents', skipped)
        self.assertIn('dotfiles', skipped)

    def test_required_chunks_are_not_skipped_because_features_inherit_their_tag(self):
        self.assertNotIn('desktop', self.skipped('login_shell'))
        self.assertNotIn('ssh_client', self.skipped('ssh_authorized_keys'))

    def test_system_chunks_use_the_system_tag_and_skip_their_siblings(self):
        name = 'sys_signin'
        args = self.args(name)
        self.assertEqual(args[:2], ['--tags', 'common,system'])
        skipped = self.skipped(name)
        self.assertNotIn(name, skipped)
        for other in CATALOG:
            if other != name:
                self.assertIn(other, skipped)

    def test_disabled_and_unknown_chunks_are_refused(self):
        with self.assertRaises(rollout.RolloutError):
            rollout.chunk_args(CATALOG, ['packages'], 'dotfiles')
        with self.assertRaises(rollout.RolloutError) as ctx:
            self.args('dotfils')
        self.assertIn("did you mean 'dotfiles'", str(ctx.exception))

    def test_high_risk_chunk_needs_a_terminal_unless_previewing(self):
        base = [sys.executable, str(ROOT / 'scripts/rollout.py'), 'chunk-args', '--config', config('')]
        refused = subprocess.run(base + ['sys_signin'], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('run it from a terminal', refused.stderr)
        preview = subprocess.run(base + ['--preview', 'sys_signin'], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(preview.returncode, 0)
        low = subprocess.run(base + ['dotfiles'], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(low.returncode, 0)


class VerifyAndDocsTests(unittest.TestCase):
    def test_verify_command_scopes_by_chunk_kind(self):
        user = rollout.verify_command(CATALOG, list(CATALOG), 'dotfiles')
        self.assertIn('--user-only', user)
        self.assertIn('dotfile-gitconfig', user[-1])
        system = rollout.verify_command(CATALOG, list(CATALOG), 'sys_mounts')
        self.assertIn('--system-only', system)

    def test_chunk_without_checks_is_unknown_not_healthy(self):
        bare = next(n for n, e in CATALOG.items() if not e.get('verify'))
        self.assertIsNone(rollout.verify_command(CATALOG, list(CATALOG), bare))

    def test_every_chunk_has_a_recovery_section(self):
        text = (ROOT / 'docs/rollout-recovery.md').read_text()
        for name in CATALOG:
            with self.subTest(chunk=name):
                self.assertRegex(text, r'(?m)^## ' + re.escape(name) + r'$')



class SmokeOnlyTests(unittest.TestCase):
    def smoke(self, *args):
        return subprocess.run([str(ROOT / 'scripts/smoke-test.sh'), '--user-only', *args], capture_output=True, text=True)

    def test_unseen_selected_check_is_skip_and_fails(self):
        result = self.smoke('--only', 'no-such-check')
        self.assertEqual(result.returncode, 1)
        self.assertIn('SKIP  no-such-check', result.stdout)

    def test_malformed_selection_is_refused(self):
        for bad in ['', 'a b', 'a,,b', ',a']:
            with self.subTest(value=bad):
                self.assertEqual(self.smoke('--only', bad).returncode, 2)

    def test_only_records_nothing_else(self):
        result = self.smoke('--only', 'no-such-check')
        self.assertEqual(result.stdout.count('SKIP'), 2)  # the line and the summary
        self.assertNotIn('PASS  ', result.stdout)


class MakeInjectionTests(unittest.TestCase):
    """The chunk name reaches the recipe through the environment, never as shell text."""

    def run_make(self, *args):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'injected'
            self.assertNotIn(' ', str(marker))
            # A goal name is not re-expanded by make ($ stays $); a CHUNK= value is, so it needs $$ to reach the recipe as $.
            # Either would create the marker file if the recipe put the text into the shell command line.
            stem_payload, value_payload = f'$(>{marker})', f'$$(>{marker})'
            result = subprocess.run(['make', *[a.replace('STEMPAYLOAD', stem_payload).replace('PAYLOAD', value_payload) for a in args]], cwd=ROOT,
                                    capture_output=True, text=True, timeout=120)
            return result, marker.exists()  # evaluate before the temporary directory disappears

    def test_chunk_target_name_is_not_executed(self):
        result, created = self.run_make('chunk-xSTEMPAYLOAD')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(created, 'the chunk name was executed as shell text')

    def test_chunk_variable_is_not_executed(self):
        for target in ('verify', 'plan'):
            with self.subTest(target=target):
                result, created = self.run_make(target, 'CHUNK=PAYLOAD')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(created, 'CHUNK was executed as shell text')


class ConfirmationTests(unittest.TestCase):
    """The high-risk prompt must reach the terminal (stderr), never the arguments make captures (stdout)."""

    def run_with_tty(self, answer):
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen([sys.executable, str(ROOT / 'scripts/rollout.py'), 'chunk-args', 'sys_signin',
                                        '--config', config('')],
                                       stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.write(master, (answer + '\n').encode())
            out, err = process.communicate(timeout=60)
            return process.returncode, out, err
        finally:
            os.close(master)
            os.close(slave)

    def test_correct_name_prints_only_arguments_on_stdout(self):
        code, out, err = self.run_with_tty('sys_signin')
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith('--tags common,system'), out)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn('high risk', err)

    def test_wrong_name_is_refused(self):
        code, out, err = self.run_with_tty('yes')
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('not confirmed', err)

    def test_mixed_verify_set_has_no_scope_flag(self):
        command = rollout.verify_command(CATALOG, ['dotfiles', 'sys_mounts'])
        self.assertNotIn('--user-only', command)
        self.assertNotIn('--system-only', command)

    def test_malformed_yaml_is_a_clean_error(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/rollout.py'), 'check',
                                 '--config', config('rollout_enable: [unclosed\n')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('not valid YAML', result.stderr)
        self.assertNotIn('Traceback', result.stderr)



class UxTests(unittest.TestCase):
    def test_bad_type_error_names_config_and_echoes_the_value(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/rollout.py'), 'check', '--config',
                                 config("rollout_enable: 'All'\n")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("config.yml: rollout_enable is 'All' (str)", result.stderr)

    def test_config_is_validated_before_preflight_and_any_sudo_prompt(self):
        makefile = (ROOT / 'Makefile').read_text()
        for target in ('all', 'update', 'check'):
            with self.subTest(target=target):
                line = re.search(rf'(?m)^{target}: (.*)$', makefile).group(1).split()
                self.assertLess(line.index('rollout-banner'), line.index('preflight'))
        self.assertIn('rollout-banner', re.search(r'(?m)^system: (.*)$', makefile).group(1))
        self.assertIn('rollout-banner', re.search(r'(?m)^plan: (.*)$', makefile).group(1))


if __name__ == '__main__':
    unittest.main()
