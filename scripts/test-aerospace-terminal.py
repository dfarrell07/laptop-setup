#!/usr/bin/env python3
"""Exercise real AeroSpace seed/migration tasks without touching the real home."""
import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / '.venv/bin/ansible-playbook'
if not ANSIBLE.exists():
    ANSIBLE = Path(shutil.which('ansible-playbook'))
TEST_TIMEOUT = int(os.environ.get('LAPTOP_SETUP_TEST_TIMEOUT', '0'))


class AeroSpaceTests(unittest.TestCase):
    def test_fresh_repeat_and_replaced_hand_edits(self):
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        tasks = copy.deepcopy(next(task['block'] for task in production if task['name'] == 'Deploy AeroSpace configuration (macOS)'))
        for task in tasks:
            if 'ansible.builtin.template' in task:
                task['ansible.builtin.template']['src'] = str(ROOT / 'roles/desktop/templates/aerospace.toml.j2')
        for terminal, app in [('ghostty', 'Ghostty'), ('terminal', 'Terminal')]:
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                config = home / '.config/aerospace/aerospace.toml'
                play = home / 'test.yml'
                play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                    'vars_files': [str(ROOT / 'roles/desktop/defaults/main.yml')],
                    'vars': {'ansible_facts': {'user_dir': str(home), 'system': 'Darwin'}},
                    'tasks': [{'ansible.builtin.set_fact': {'desktop_terminal': terminal}}] + tasks}]))
                def run(expected_rc=0):
                    result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                        env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, expected_rc, result.stdout + result.stderr)
                    return result.stdout
                run()
                expected = f'exec-and-forget open -na {app}'
                bindings = tomllib.loads(config.read_text())['mode']['main']['binding']
                self.assertEqual(bindings['alt-enter'], expected)
                for key in ('alt-d', 'alt-ctrl-l'):  # AeroSpace runs exec-and-forget through bash -c
                    command = bindings[key].removeprefix('exec-and-forget ')
                    self.assertEqual(subprocess.run(['bash', '-n', '-c', command]).returncode, 0, command)
                self.assertIn('/opt/homebrew/bin/choose |', bindings['alt-d'])
                # Run the launcher with stubs: choose prints the pick without a newline (as choose-gui does),
                # names keep spaces and quotes, and a cancelled pick (no output) opens nothing.
                for pick, opened in [('Say "It\'s" On', 'Say "It\'s" On'), ('', None)]:
                    stubs = home / 'stubs'
                    stubs.mkdir(exist_ok=True)
                    (stubs / 'ls').write_text('#!/bin/sh\nprintf "%s\\n" "/Applications:" "Say \\"It\'s\\" On.app" "x.txt"\n')
                    (stubs / 'open').write_text('#!/bin/sh\nprintf "%s" "$2" > "$OPENED"\n')
                    choose = stubs / 'choose'
                    choose.write_text('#!/bin/sh\ncat > "$LISTED"; printf "%s" "$PICK"\n')
                    for stub in stubs.iterdir():
                        stub.chmod(0o755)
                    record = home / 'opened'
                    record.unlink(missing_ok=True)
                    command = bindings['alt-d'].removeprefix('exec-and-forget ').replace('/opt/homebrew/bin/choose', str(choose))
                    listed = home / 'listed'
                    subprocess.run(['bash', '-c', command], env=dict(os.environ, PATH=f'{stubs}:/usr/bin:/bin', PICK=pick,
                                   OPENED=str(record), LISTED=str(listed)), check=False, timeout=30)
                    self.assertEqual(listed.read_text(), 'Say "It\'s" On\n')  # .app stripped, headings and files dropped
                    self.assertEqual(record.read_text() if record.exists() else None, opened)
                self.assertIn('using {control down, command down}', bindings['alt-ctrl-l'])  # macOS lock-screen shortcut
                self.assertEqual(re.findall(r'changed=(\d+)', run())[-1], '0')
                # Managed like the Ghostty config: a re-run replaces hand edits and an old seeded copy (template
                # updates land), keeping the replaced file beside it
                managed = config.read_text()
                self.assertTrue(managed.startswith('# Ansible managed\n'), managed[:80])
                for edited in [managed.replace(expected, 'exec-and-forget open -na Alacritty'),
                               '# Seeded once by Ansible; edit freely\n' + managed.replace(expected, 'exec-and-forget my-term')]:
                    for old in config.parent.glob('aerospace.toml.*~'):
                        old.unlink()
                    config.write_text(edited)
                    self.assertEqual(re.findall(r'changed=(\d+)', run())[-1], '1')
                    self.assertEqual(config.read_text(), managed)
                    self.assertEqual([b.read_text() for b in config.parent.glob('aerospace.toml.*~')], [edited])
                    self.assertEqual(re.findall(r'changed=(\d+)', run())[-1], '0')
                before = config.read_text()
                for key, value in [('desktop_macos_terminal_app', 'Ghostty; touch /tmp/unsafe-terminal'),
                                   ('desktop_macos_terminal_path', '/tmp/untrusted.app')]:
                    fixture = yaml.safe_load(play.read_text())
                    fixture[0]['tasks'][0]['ansible.builtin.set_fact'] = {'desktop_terminal': terminal, key: value}
                    play.write_text(yaml.safe_dump(fixture))
                    run(2)
                    self.assertEqual(config.read_text(), before)

    def test_aerospace_starts_when_it_is_the_wm_and_not_running(self):
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        block = next(task['block'] for task in production if task['name'] == 'Deploy AeroSpace configuration (macOS)')
        tasks = [task for task in block if 'AeroSpace' in task['name'] and ('running' in task['name']
                 or task['name'].startswith(('Start', 'Warn')))]
        self.assertEqual(len(tasks), 3)
        start = next(task for task in tasks if task['name'].startswith('Start'))
        self.assertNotIn('become', start)  # a user app: no sudo
        self.assertEqual(start['ansible.builtin.command']['argv'], ['/usr/bin/open', '-ga', 'AeroSpace'])
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            for task in tasks:  # Apple's tools by full path; run the stubs instead
                argv = task.get('ansible.builtin.command', {}).get('argv')
                if argv:
                    argv[0] = str(d / Path(argv[0]).name)
            log = d / 'calls'
            for name, body in [('pgrep', f'echo "pgrep $*" >> {log}; exit "$RUNNING"'),
                               ('open', f'echo "open $*" >> {log}; exit "$OPEN_RC"')]:
                (d / name).write_text('#!/bin/sh\n' + body + '\n')
                (d / name).chmod(0o755)
            play = d / 'test.yml'

            def run(wm, running, open_rc=0):
                log.unlink(missing_ok=True)
                play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                    'vars': {'desktop_macos_tiling_wm': wm}, 'tasks': tasks}]))
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                    env=dict(os.environ, RUNNING=str(running), OPEN_RC=str(open_rc),
                             ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                    capture_output=True, text=True, timeout=max(60, TEST_TIMEOUT))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return (log.read_text() if log.exists() else ''), result.stdout
            calls, out = run(True, 1)
            self.assertEqual(calls, 'pgrep -x AeroSpace\nopen -ga AeroSpace\n')
            self.assertEqual(re.findall(r'changed=(\d+)', out)[-1], '1')
            calls, out = run(True, 0)
            self.assertEqual(calls, 'pgrep -x AeroSpace\n')  # already running: left alone
            self.assertEqual(re.findall(r'changed=(\d+)', out)[-1], '0')
            self.assertEqual(run(False, 1)[0], '')  # another window manager: not probed, not started
            calls, out = run(True, 1, open_rc=1)  # not installed: warn, do not fail the chunk
            self.assertIn('open -ga AeroSpace', calls)
            self.assertIn('make apply CHUNK=packages', out)

    def test_aerospace_reloads_a_changed_config_by_full_path(self):
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        block = next(task['block'] for task in production if task['name'] == 'Deploy AeroSpace configuration (macOS)')
        names = [task['name'] for task in block]
        deploy = copy.deepcopy(block[names.index('Deploy AeroSpace config (macOS)')])
        reload_task = block[names.index('Reload the changed AeroSpace config (macOS)')]
        self.assertEqual(names.index(reload_task['name']), names.index(deploy['name']) + 1)
        self.assertNotIn('become', reload_task)  # the user's AeroSpace: no sudo
        self.assertEqual(reload_task['ansible.builtin.command']['argv'],
                         ["{{ ansible_facts['user_dir'] }}/.local/bin/aerospace", 'reload-config'])
        deploy['ansible.builtin.template']['src'] = str(ROOT / 'roles/desktop/templates/aerospace.toml.j2')
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.config/aerospace').mkdir(parents=True)
            (home / '.local/bin').mkdir(parents=True)
            log = home / 'calls'
            (home / '.local/bin/aerospace').write_text(f'#!/bin/sh\necho "aerospace $*" >> {log}; exit "$RELOAD_RC"\n')
            (home / '.local/bin/aerospace').chmod(0o755)
            play = home / 'test.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                'vars_files': [str(ROOT / 'roles/desktop/defaults/main.yml')],
                'vars': {'ansible_facts': {'user_dir': str(home), 'system': 'Darwin'}}, 'tasks': [deploy, reload_task]}]))

            def run(rc=0):
                log.unlink(missing_ok=True)
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                    env=dict(os.environ, RELOAD_RC=str(rc), ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                    capture_output=True, text=True, timeout=max(60, TEST_TIMEOUT))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return log.read_text() if log.exists() else ''
            self.assertEqual(run(), 'aerospace reload-config\n')  # new config: reloaded
            self.assertEqual(run(), '')  # unchanged: left alone
            config = home / '.config/aerospace/aerospace.toml'
            config.write_text(config.read_text() + '# edited\n')
            self.assertEqual(run(rc=1), 'aerospace reload-config\n')  # AeroSpace not running: the run still passes
            (home / '.local/bin/aerospace').unlink()
            config.write_text('')
            run()  # not installed yet: passes too

    def test_firewall_enabled_and_disabled_output_formats(self):
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        task = next(task for task in production if task['name'] == 'Enable macOS firewall settings')
        condition = task['when'][1]
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        setting = next(item for item in defaults['desktop_macos_firewall_settings'] if item['name'] == 'stealth')
        cases = [('Stealth Mode = 1', False), ('Stealth mode enabled', False),
                 ('Firewall stealth mode is on', False), ('Stealth mode disabled', True),
                 ('Stealth mode not enabled', True), ('Stealth Mode = 0', True)]
        tasks = [{'ansible.builtin.assert': {'that': f'({condition}) == {str(enable).lower()}'},
                  'vars': {'item': setting, 'ansible_loop': {'index0': 0},
                           '_fw_check_results': {'results': [{'stdout': output}]}}} for output, enable in cases]
        with tempfile.TemporaryDirectory() as directory:
            play = Path(directory) / 'test.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'tasks': tasks}]))
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                                    env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ghostty_config_matches_alacritty_and_is_idempotent(self):
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        tasks = copy.deepcopy(next(task['block'] for task in production if task['name'] == 'Deploy Ghostty configuration (macOS)'))
        for task in tasks:
            if 'ansible.builtin.template' in task:
                task['ansible.builtin.template']['src'] = str(ROOT / 'roles/desktop/templates/ghostty.config.j2')
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            play = home / 'test.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False,
                'vars_files': [str(ROOT / 'roles/desktop/defaults/main.yml')],
                'vars': {'ansible_facts': {'user_dir': str(home), 'system': 'Darwin'}}, 'tasks': tasks}]))
            def run():
                result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                    env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                    capture_output=True, text=True, timeout=max(60, TEST_TIMEOUT))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout
            run()
            lines = (home / '.config/ghostty/config').read_text().splitlines()
            settings = [line.split(' = ', 1) for line in lines if line and not line.startswith('#')]
            self.assertTrue(all(len(pair) == 2 for pair in settings), lines)
            values = dict(settings)
            self.assertEqual(values['font-family'], defaults['desktop_alacritty_font_family'])
            self.assertEqual(values['background'], defaults['desktop_alacritty_color_bg'])
            self.assertEqual(values['clipboard-read'], 'deny')  # Alacritty osc52 = OnlyCopy
            self.assertEqual(values['term'], 'xterm-256color')
            palette = [value for key, value in settings if key == 'palette']
            self.assertEqual([entry.split('=')[0] for entry in palette], [str(n) for n in range(16)])
            self.assertTrue(all(re.fullmatch(r'\d+=#[0-9a-f]{6}', entry) for entry in palette), palette)
            self.assertEqual(re.findall(r'changed=(\d+)', run())[-1], '0')

    def test_macos_defaults_are_user_space_and_restart_their_service(self):
        defaults = yaml.safe_load((ROOT / 'roles/desktop/defaults/main.yml').read_text())
        production = yaml.safe_load((ROOT / 'roles/desktop/tasks/main.yml').read_text())
        task = next(task for task in production if task['name'] == 'Set macOS defaults')
        self.assertNotIn('become', task)
        self.assertEqual(task['register'], '_desktop_macos_defaults')  # the restart below reads it
        self.assertIs(task['community.general.osx_defaults']['check_type'], False)
        self.assertIn('item.manage', task['when'])
        restart = next(task for task in production if task['name'] == 'Restart macOS UI services whose defaults changed')
        self.assertNotIn('become', restart)
        restarted = {domain for item in restart['loop'] for domain in item['domains']}
        keys = set()
        for item in defaults['desktop_macos_defaults']:
            self.assertIn(item['type'], {'bool', 'int', 'float', 'string'}, item)
            self.assertNotIn((item['domain'], item['key']), keys, 'duplicate default')
            keys.add((item['domain'], item['key']))
        self.assertIn(('NSGlobalDomain', 'ApplePressAndHoldEnabled'), keys)
        for domain in ('com.apple.dock', 'com.apple.screencapture'):
            self.assertIn(domain, {domain for domain, _ in keys})
            self.assertIn(domain, restarted)
        condition = restart['when'][2]
        results = {'results': [{'changed': True, 'item': {'domain': 'com.apple.dock'}},
                               {'changed': False, 'item': {'domain': 'com.apple.screencapture'}}]}
        cases = [({'service': 'Dock', 'domains': ['com.apple.dock']}, True),
                 ({'service': 'SystemUIServer', 'domains': ['com.apple.screencapture']}, False)]
        tasks = [{'ansible.builtin.assert': {'that': f'({condition}) == {str(want).lower()}'},
                  'vars': {'item': item, '_desktop_macos_defaults': results}} for item, want in cases]
        with tempfile.TemporaryDirectory() as directory:
            play = Path(directory) / 'test.yml'
            play.write_text(yaml.safe_dump([{'hosts': 'localhost', 'gather_facts': False, 'tasks': tasks}]))
            result = subprocess.run([str(ANSIBLE), '-i', 'localhost,', '-c', 'local', str(play)], cwd=ROOT,
                                    env=dict(os.environ, ANSIBLE_VAULT_PASSWORD_FILE=str(ROOT / 'scripts/vault-pass-ci.sh')),
                                    capture_output=True, text=True, timeout=max(60, TEST_TIMEOUT))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
