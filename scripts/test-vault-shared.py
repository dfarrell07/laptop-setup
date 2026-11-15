#!/usr/bin/env python3
"""scripts/vault-shared.sh end to end, with software age keys in place of YubiKeys.

Two fake checkouts ("machines") share a bare git remote. Machine A moves its vault into the shared repository
(init), machine B joins, an edit on A reaches B through pull, and removing A's key rotates the password so A's key
no longer opens it. ansible-vault comes from the dev venv; the tests skip when age is not installed.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts/vault-shared.sh'
VENV_BIN = ROOT / '.venv/bin'
AGE = shutil.which('age')


def run(cmd, cwd=None, env=None, input=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, env=env, input=input, capture_output=True, text=True, timeout=120,
                            check=False)
    if check and result.returncode:
        raise AssertionError(f'{cmd} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
    return result


@unittest.skipUnless(AGE and shutil.which('age-keygen'), 'age is not installed')
@unittest.skipUnless((VENV_BIN / 'ansible-vault').exists(), 'no dev venv (make bootstrap)')
class SharedVault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = self.tmp / 'remote.git'
        run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.remote)],
            env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1'))
        self.keys = {}
        for name in ('a', 'b'):
            key = self.tmp / f'key-{name}'
            run(['age-keygen', '-o', str(key)])
            self.keys[name] = (key, run(['age-keygen', '-y', str(key)]).stdout.strip())

    def machine(self, name, vault_text):
        """A checkout with the script, a fake dispatch wrapper and a vault encrypted with this machine's password."""
        repo = self.tmp / f'machine-{name}'
        (repo / 'scripts').mkdir(parents=True)
        (repo / 'group_vars/all').mkdir(parents=True)
        shutil.copy(SCRIPT, repo / 'scripts/vault-shared.sh')
        # The real wrapper pins ANSIBLE_VAULT_PASSWORD_FILE to scripts/vault-pass.sh; so does this stand-in, and it
        # swaps in a scripted editor for ansible-vault edit (the script itself clears EDITOR).
        (repo / 'scripts/verify-ansible-args.sh').write_text(
            '#!/bin/sh\nexport ANSIBLE_VAULT_PASSWORD_FILE=scripts/vault-pass.sh\n'
            'if [ "$2" = edit ]; then export EDITOR="$TEST_EDITOR"; fi\nexec "$@"\n')
        (repo / 'scripts/verify-ansible-args.sh').chmod(0o755)
        (repo / 'scripts/vault-pass.sh').write_text(f'#!/bin/sh\necho old-password-{name}\n')
        (repo / 'scripts/vault-pass.sh').chmod(0o700)
        (repo / 'config.yml').write_text('dotfiles_github_user: tester  # comment\n')
        vault = repo / 'group_vars/all/vault.yml'
        vault.write_text('vault_placeholder: ""\n')
        env = self.env(name, repo)
        run(['git', 'init', '-q', str(repo)], env=env)
        run(['git', '-C', str(repo), 'add', '-A'], env=env)
        run(['git', '-C', str(repo), 'commit', '-q', '-m', 'x'], env=env)
        if vault_text is not None:
            vault.write_text(vault_text)
            run([str(VENV_BIN / 'ansible-vault'), 'encrypt', str(vault)], cwd=repo, env=self.env(name, repo))
        return repo

    def env(self, name, repo):
        editor = self.tmp / 'editor.sh'
        editor.write_text('#!/bin/sh\nsed -i "s/^vault_codex_api_key: .*/vault_codex_api_key: edited/" "$1"\n')
        editor.chmod(0o755)
        home = self.tmp / f'home-{name}'
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'ANSIBLE_'))}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), NO_COLOR='1',
                   PATH=f'{VENV_BIN}:{os.environ["PATH"]}', SECRETS_DIR=str(self.tmp / f'secrets-{name}'),
                   SECRETS_REMOTE=str(self.remote), VAULT_AGE_IDENTITIES=str(self.keys[name][0]),
                   ANSIBLE_VAULT_PASSWORD_FILE='scripts/vault-pass.sh', TEST_EDITOR=str(editor),
                   GIT_CONFIG_GLOBAL=str(home / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1')
        Path(env['GIT_CONFIG_GLOBAL']).write_text('[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n')
        return env

    def shared(self, name, repo, *args, check=True, **extra):
        return run(['bash', str(repo / 'scripts/vault-shared.sh'), *args], cwd=repo,
                   env=dict(self.env(name, repo), **extra), check=check)

    def view(self, name, repo, path='group_vars/all/vault.yml'):
        return run([str(VENV_BIN / 'ansible-vault'), 'view', path], cwd=repo, env=self.env(name, repo)).stdout

    def init_a(self):
        a = self.machine('a', 'vault_codex_api_key: secret-a\n')
        run(['git', 'clone', '-q', str(self.remote), str(self.tmp / 'secrets-a')], env=self.env('a', a))
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['a'][1])
        result = self.shared('a', a, 'init')
        self.assertIn('the shared vault is active', result.stdout)
        return a

    def test_init_moves_the_vault_and_a_random_password_seals_it(self):
        a = self.init_a()
        secrets = self.tmp / 'secrets-a'
        self.assertIn('secret-a', self.view('a', a))
        self.assertEqual((a / 'group_vars/all/vault.yml').read_bytes(), (secrets / 'vault.yml').read_bytes())
        helper = (a / 'scripts/vault-pass.sh').read_text()
        self.assertIn('exec age -d -i', helper)
        self.assertNotIn('old-password', helper)
        self.assertEqual((a / 'group_vars/all/vault.yml').stat().st_mode & 0o777, 0o600)
        pushed = run(['git', f'--git-dir={self.remote}', 'ls-tree', '--name-only', 'main']).stdout.split()
        self.assertEqual(sorted(pushed), ['.gitattributes', 'README.md', 'recipients', 'vault-pass.age', 'vault.yml'])
        # Nothing but ciphertext is pushed: no password file, and the vault is still a vault.
        self.assertTrue((secrets / 'vault.yml').read_text().startswith('$ANSIBLE_VAULT'))
        self.assertTrue((a / 'group_vars/all/vault.yml.pre-shared').exists())

    def test_join_with_a_plaintext_stub_then_edit_reaches_the_other_machine(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)  # never ran vault-init: the plaintext stub
        self.shared('b', b, 'join')
        self.assertIn('secret-a', self.view('b', b))
        self.assertFalse((b / 'group_vars/all/vault.yml.pre-shared').exists())  # the stub is not kept as a vault
        self.shared('a', a, 'edit')
        self.shared('b', b, 'pull')
        self.assertIn('vault_codex_api_key: edited', self.view('b', b))

    def test_an_unapproved_recipient_is_refused_until_forced(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        self.shared('b', b, 'join')
        # Someone pushes a recipient B never approved; B's next re-encryption refuses it.
        _, rogue = self.keys['a'][0], run(['age-keygen'], check=True).stdout.split('public key: ')[1].split()[0]
        secrets_a = self.tmp / 'secrets-a'
        with open(secrets_a / 'recipients', 'a') as f:
            f.write(f'{rogue}\n')
        run(['git', '-C', str(secrets_a), 'commit', '-qam', 'rogue'], env=self.env('a', a))
        run(['git', '-C', str(secrets_a), 'push', '-q'], env=self.env('a', a))
        pulled = self.shared('b', b, 'pull')
        self.assertIn('recipients list changed', pulled.stderr)
        third = run(['age-keygen'], check=True).stdout.split('public key: ')[1].split()[0]
        refused = self.shared('b', b, 'add-key', check=False, RECIPIENT=third)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('never approved', refused.stderr)

    def test_remove_key_rotates_so_the_removed_key_no_longer_opens_it(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        self.shared('b', b, 'join')
        old_pass = run(['age', '-d', '-i', str(self.keys['a'][0]), str(self.tmp / 'secrets-a/vault-pass.age')]).stdout
        self.shared('b', b, 'remove-key', self.keys['a'][1])
        secrets_b = self.tmp / 'secrets-b'
        self.assertNotIn(self.keys['a'][1], (secrets_b / 'recipients').read_text())
        denied = run(['age', '-d', '-i', str(self.keys['a'][0]), str(secrets_b / 'vault-pass.age')], check=False)
        self.assertNotEqual(denied.returncode, 0)
        new_pass = run(['age', '-d', '-i', str(self.keys['b'][0]), str(secrets_b / 'vault-pass.age')]).stdout
        self.assertNotEqual(new_pass, old_pass)
        self.assertIn('secret-a', self.view('b', b))
        stale = self.tmp / 'old-pass.sh'
        stale.write_text(f'#!/bin/sh\nprintf %s {old_pass.strip()!r}\n')
        stale.chmod(0o700)
        only_stale = {k: v for k, v in self.env('b', b).items() if k != 'ANSIBLE_VAULT_PASSWORD_FILE'}
        self.assertNotEqual(run([str(VENV_BIN / 'ansible-vault'), 'view', '--vault-password-file', str(stale),
                                 str(secrets_b / 'vault.yml')], cwd=b, env=only_stale, check=False).returncode, 0)

    def test_init_refuses_a_plaintext_vault_and_changes_nothing(self):
        a = self.machine('a', None)
        run(['git', 'clone', '-q', str(self.remote), str(self.tmp / 'secrets-a')], env=self.env('a', a))
        before = (a / 'scripts/vault-pass.sh').read_text()
        result = self.shared('a', a, 'init', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('is not encrypted here', result.stderr)
        self.assertEqual((a / 'scripts/vault-pass.sh').read_text(), before)


if __name__ == '__main__':
    unittest.main()
