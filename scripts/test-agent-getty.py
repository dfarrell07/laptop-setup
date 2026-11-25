#!/usr/bin/env python3
"""agent-getty (docs/agent-getty.md): pair validation, the rendered unit and plist, and the rendered script run against
fake harnesses on a private tmux socket (real Ansible engine, no host changes)."""
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
VARS_FILES = ['default.config.yml', 'group_vars/all/vars.yml', 'roles/dotfiles/defaults/main.yml',
              'roles/claude/defaults/main.yml']
TASKS = ROOT / 'roles/claude/tasks/agent_getty.yml'


def play(out, tasks, extra=None):
    """Run tasks on localhost with the role's vars; returns the CompletedProcess."""
    book = [{'hosts': 'localhost', 'gather_facts': True, 'gather_subset': ['min'],
             'vars_files': [str(ROOT / p) for p in VARS_FILES], 'tasks': tasks}]
    path = Path(out) / f'play-{time.monotonic_ns()}.yml'
    path.write_text(yaml.safe_dump(book))
    extra_file = path.with_suffix('.json')  # extra vars: they outrank vars_files, as config.yml does
    extra_file.write_text(json.dumps(extra or {}))
    return subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', '-e', f'@{extra_file}', str(path)], cwd=ROOT,
                          env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                          capture_output=True, text=True, timeout=180)


def enabled_pairs(result):
    """The ENABLED=... debug line, whichever stdout callback printed it."""
    return re.search(r'ENABLED=([a-z0-9,-]*)', result.stdout).group(1)


def wait_for(check, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.2)
    return False


class AgentGettyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = out = Path(cls.tmp.name)
        cls.home = out / 'home'
        cls.home.mkdir()
        renders = [('agent-getty.j2', 'agent-getty', {}), ('agent-getty@.service.j2', 'unit', {}),
                   ('agent-getty.plist.j2', 'plist', {'_launchd_label': 'getty-codex-personal'})]
        tasks = [{'name': f'render {dest}', 'ansible.builtin.template': {
            'src': str(ROOT / 'roles/claude/templates' / src), 'dest': str(out / dest), 'mode': '0700'},
            'vars': extra} for src, dest, extra in renders]
        result = play(out, tasks, {
            'agent_getty_pairs': ['claude/personal', 'codex/personal', 'agy/personal'],
            'agent_getty_dir': str(cls.home), 'agent_getty_host': 'testhost',
            '_getty_enabled': ['claude-personal', 'codex-personal'],
            'codex_personal_config_dir': str(out / 'codex personal'), 'dotfiles_github_user': 'someone'})
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # --- pair validation: the first three tasks of agent_getty.yml (assert, warn, pair lists), run as is ---

    def validate(self, pairs, **extra):
        tasks = yaml.safe_load(TASKS.read_text())[:3]
        tasks.append({'ansible.builtin.debug': {'msg': 'ENABLED={{ _getty_enabled | join(",") }}'}})
        return play(self.out, tasks, dict({'agent_getty_pairs': pairs, 'profile': 'personal'}, **extra))

    def test_supported_pairs_pass(self):
        for pairs, profile, enabled in [
                ([], 'work', ''),
                (['claude/personal', 'codex/work', 'codex/personal', 'agy/personal'], 'work',
                 'claude-personal,codex-work,codex-personal'),
                (['claude/personal', 'claude/personal2', 'codex/personal', 'agy/personal'], 'personal',
                 'claude-personal,claude-personal2,codex-personal')]:
            result = self.validate(pairs, profile=profile)
            self.assertEqual(result.returncode, 0, (pairs, result.stdout[-2000:]))
            self.assertEqual(enabled_pairs(result), enabled)
            self.assertNotIn('agent-getty skips', result.stdout)

    def test_pairs_not_possible_yet_are_skipped_with_a_warning(self):
        result = self.validate(['claude/work', 'agy/work', 'codex/work', 'codex/personal2', 'agy/personal2'])
        self.assertEqual(result.returncode, 0, result.stdout[-2000:])
        self.assertIn('agent-getty skips claude/work, agy/work, codex/personal2, agy/personal2:', ' '.join(result.stdout.split()))
        self.assertEqual(enabled_pairs(result), 'codex-work')

    def test_unknown_pairs_and_personal2_on_work_fail_closed(self):
        for pairs, profile in [(['claude/personal2'], 'work'), (['codex/personal2'], 'work'),
                               (['claude-personal'], 'personal'), ('claude/personal', 'personal'),
                               (['claude/personal; rm -rf ~'], 'personal')]:
            result = self.validate(pairs, profile=profile)
            self.assertNotEqual(result.returncode, 0, pairs)
            self.assertIn('agent_getty_pairs must list pairs from', result.stdout, pairs)

    def test_unsafe_dir_and_host_fail(self):
        for extra in [{'agent_getty_dir': '/home/a b'}, {'agent_getty_dir': 'relative'},
                      {'agent_getty_host': 'host;x'}]:
            self.assertNotEqual(self.validate(['claude/personal'], **extra).returncode, 0, extra)

    def test_a_planted_drop_in_stops_the_run(self):
        linux = next(t for t in yaml.safe_load(TASKS.read_text()) if t.get('name') == 'Agent-getty units (Linux)')
        tasks = [t for t in linux['block'] if 'drop-ins' in t.get('name', '')]
        self.assertEqual(len(tasks), 2)
        home = self.out / 'dropin-home'
        units = home / '.config/systemd/user'
        units.mkdir(parents=True)
        self.assertEqual(play(self.out, tasks, {'ansible_facts': {'user_dir': str(home)}}).returncode, 0)
        (units / 'agent-getty@claude-personal.service.d').mkdir()
        result = play(self.out, tasks, {'ansible_facts': {'user_dir': str(home)}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('agent-getty@claude-personal.service.d', result.stdout)

    # --- rendered unit and plist ---

    def test_unit_restarts_forever_and_runs_the_instance(self):
        unit = (self.out / 'unit').read_text()
        for line in ['ExecStart=/bin/bash %h/.local/bin/agent-getty run %i', 'Restart=always',
                     'StartLimitIntervalSec=0', 'WantedBy=default.target',
                     'Environment=TMUX_TMPDIR=%t/tmux']:  # the shells' socket directory, so attach and ls find it
            self.assertIn(line + '\n', unit)
        self.assertNotIn('KillMode', unit.replace('# The default KillMode', ''))
        verify = shutil.which('systemd-analyze')
        if verify:
            path = self.out / 'agent-getty@.service'
            path.write_text(unit)
            result = subprocess.run([verify, '--user', 'verify', f'{path}'], capture_output=True, text=True)
            self.assertNotIn('Unknown key', result.stderr)

    def test_plist_keeps_the_pair_alive(self):
        plist = plistlib.loads((self.out / 'plist').read_bytes())
        self.assertEqual(plist['Label'], 'com.someone.claude-getty-codex-personal')
        self.assertEqual(plist['ProgramArguments'][1:], [f'{Path.home()}/.local/bin/agent-getty', 'run', 'codex-personal'])
        self.assertIs(plist['KeepAlive'], True)
        self.assertIs(plist['RunAtLoad'], True)
        self.assertIn('/opt/homebrew/bin', plist['EnvironmentVariables']['PATH'])

    # --- the script, against fake harnesses on a private tmux socket ---

    @unittest.skipUnless(shutil.which('shellcheck'), 'shellcheck not installed')
    def test_rendered_script_passes_shellcheck(self):
        result = subprocess.run(['shellcheck', '-S', 'warning', str(self.out / 'agent-getty')], capture_output=True,
                                text=True)
        self.assertEqual(result.returncode, 0, result.stdout)

    def script_env(self, fake, claude_prompt=None):
        """A bin dir whose claude and codex log their arguments and environment, then wait."""
        bindir = self.out / f'bin-{fake}'
        bindir.mkdir(exist_ok=True)
        log = self.out / f'log-{fake}'
        for name in ('claude', 'codex'):
            body = (f'#!/bin/bash\necho "start {name} $* | CLAUDE_CONFIG_DIR=${{CLAUDE_CONFIG_DIR-unset}} '
                    f'ANTHROPIC_API_KEY=${{ANTHROPIC_API_KEY-unset}} CODEX_HOME=${{CODEX_HOME-unset}}" >> {log}\n')
            if name == 'claude':
                # like Claude: the screen clears once the question is answered
                body += claude_prompt or (f'echo "Trust {self.home}? [y/N]"; read -r a; echo "answer $a" >> {log}; '
                                          "printf '\\033[2J\\033[H'\n")
            body += f'echo $$ > {log}.pid; exec sleep 300\n'
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        # short: a macOS socket path must stay under 104 characters, and $TMPDIR there is long
        tmux_dir = Path(tempfile.mkdtemp(dir='/tmp'))
        self.addCleanup(shutil.rmtree, tmux_dir, True)
        env = dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}', TMUX_TMPDIR=str(tmux_dir), AGENT_GETTY_POLL='1',
                   HOME=str(self.home), XDG_STATE_HOME=str(self.out / f'state-{fake}'),
                   CLAUDE_CONFIG_DIR='/leaked', ANTHROPIC_API_KEY='leaked')
        env.pop('TMUX', None)
        return env, log

    def getty(self, env, *args, **kw):
        return subprocess.Popen(['/bin/bash', str(self.out / 'agent-getty'), *args], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)

    def reap(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.communicate()

    def stop_server(self, env, pair):
        subprocess.run(['tmux', '-L', f'agent-getty-{pair}', 'kill-server'], env=env, capture_output=True)

    def test_run_starts_once_adopts_and_exits_when_the_daemon_ends(self):
        env, log = self.script_env('claude')
        first = self.getty(env, 'run', 'claude-personal')
        self.addCleanup(self.reap, first)
        self.addCleanup(self.stop_server, env, 'claude-personal')
        self.assertTrue(wait_for(lambda: log.exists() and 'answer y' in log.read_text()), log.read_text() if log.exists() else '')
        line = log.read_text().splitlines()[0]
        self.assertIn('start claude remote-control --name testhost-claude-personal --permission-mode bypassPermissions', line)
        # cper's home, never the caller's account
        self.assertIn(f'CLAUDE_CONFIG_DIR={Path.home()}/.claude ANTHROPIC_API_KEY=unset', line)
        second = self.getty(env, 'run', 'claude-personal')  # a duplicate start adopts the running daemon
        self.addCleanup(self.reap, second)
        time.sleep(3)
        self.assertEqual(sum(1 for x in log.read_text().splitlines() if x.startswith('start')), 1)
        self.assertIsNone(second.poll())
        listing = subprocess.run(['/bin/bash', str(self.out / 'agent-getty'), 'ls'], env=env, capture_output=True,
                                 text=True)
        self.assertEqual(listing.stdout, 'claude-personal running\ncodex-personal stopped\n')
        os.kill(int(Path(f'{log}.pid').read_text()), 15)  # the daemon ends
        for proc in (first, second):
            _, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 1)
            self.assertIn('getty-claude-personal ended', err)

    def test_an_answer_claude_finds_too_quick_is_given_again(self):
        # Claude ignores an answer within its grace window after the question and asks again, redrawing the screen
        # (clear and clear-history, as Ink does), so the question never piles up in history
        question = f'echo "Trust {self.home}? [y/N]"'
        prompt = ('ask() { printf \'\\033[2J\\033[3J\\033[H\'; ' + question + '; t=$(date +%s%N); read -r a; '
                  'if [ $(( ($(date +%s%N) - t) / 1000000 )) -lt 2500 ]; then ask; '
                  'else echo "answer $a" >> LOG; printf \'\\033[2J\\033[H\'; fi; }; ask\n')
        env, log = self.script_env('reask', prompt.replace('LOG', str(self.out / 'log-reask')))
        proc = self.getty(env, 'run', 'claude-personal')
        self.addCleanup(self.reap, proc)
        self.addCleanup(self.stop_server, env, 'claude-personal')
        self.assertTrue(wait_for(lambda: log.exists() and 'answer y' in log.read_text(), timeout=30),
                        log.read_text() if log.exists() else 'no log')

    def test_a_stop_during_start_up_stops_the_daemon(self):
        # a claude whose trust question never appears keeps run() in its start-up wait
        env, log = self.script_env('early', 'echo starting; read -r a\n')
        proc = self.getty(env, 'run', 'claude-personal')
        self.addCleanup(self.reap, proc)
        self.addCleanup(self.stop_server, env, 'claude-personal')
        self.assertTrue(wait_for(lambda: Path(f'{log}.pid').exists() or log.exists()))
        time.sleep(2)
        proc.terminate()
        proc.communicate(timeout=15)
        listing = subprocess.run(['tmux', '-L', 'agent-getty-claude-personal', 'ls'], env=env, capture_output=True)
        self.assertNotEqual(listing.returncode, 0)  # nothing left running

    def test_the_daemons_output_is_kept_in_a_log(self):
        env, _ = self.script_env('logged', 'echo "not logged in"; exec sleep 300\n')
        logs = self.home / 'Library/Logs' if sys.platform == 'darwin' else self.out / 'state-logged/agent-getty'
        daemon_log = logs / 'agent-getty-claude-personal-daemon.log'
        claude = self.getty(env, 'run', 'claude-personal')
        self.addCleanup(self.reap, claude)
        self.addCleanup(self.stop_server, env, 'claude-personal')
        self.assertTrue(wait_for(lambda: daemon_log.exists() and 'not logged in' in daemon_log.read_text()),
                        daemon_log.read_text() if daemon_log.exists() else 'no log')

    def test_stopping_the_run_stops_the_daemon(self):
        env, log = self.script_env('stop')
        proc = self.getty(env, 'run', 'codex-work')
        self.addCleanup(self.reap, proc)
        self.addCleanup(self.stop_server, env, 'codex-work')
        self.assertTrue(wait_for(lambda: Path(f'{log}.pid').exists()))
        daemon = int(Path(f'{log}.pid').read_text())
        proc.terminate()  # what systemd and launchd send
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(wait_for(lambda: not Path(f'/proc/{daemon}').exists() if Path('/proc').is_dir()
                                 else subprocess.run(['kill', '-0', str(daemon)]).returncode != 0, timeout=10))
        listing = subprocess.run(['tmux', '-L', 'agent-getty-codex-work', 'ls'], env=env, capture_output=True)
        self.assertNotEqual(listing.returncode, 0)  # the pair's tmux server is gone

    def test_codex_runs_in_the_foreground_with_its_profile_home(self):
        env, log = self.script_env('codex')
        proc = self.getty(env, 'run', 'codex-personal')
        self.addCleanup(self.reap, proc)
        self.addCleanup(self.stop_server, env, 'codex-personal')
        self.assertTrue(wait_for(lambda: log.exists() and log.read_text()))
        line = log.read_text().splitlines()[0]
        self.assertTrue(line.startswith('start codex remote-control --no-daemon |'), line)
        self.assertTrue(line.endswith(f'CODEX_HOME={self.out}/codex personal'), line)
        self.assertEqual((self.out / 'codex personal').stat().st_mode & 0o777, 0o700)

    def test_unknown_pair_is_refused(self):
        env, _ = self.script_env('none')
        for pair in ('claude-work', 'agy-personal', '../x'):
            proc = self.getty(env, 'run', pair)
            _, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 2, pair)
            self.assertIn('unknown pair', err)


if __name__ == '__main__':
    unittest.main()
