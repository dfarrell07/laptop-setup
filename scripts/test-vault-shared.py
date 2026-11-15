#!/usr/bin/env python3
"""scripts/vault-shared.sh end to end, with software age keys in place of YubiKeys.

Two fake checkouts ("machines") share a bare git remote. Machine A moves its vault into the shared repository
(init), machine B joins, an edit on A reaches B through pull, and removing A's key rotates the password so A's key
no longer opens it. ansible-vault comes from the dev venv; the tests skip when age is not installed.
"""
import os
import shutil
import subprocess
import sys
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
        # The owner's SSH signing key: every commit to the shared repository is signed with it, and only it is allowed.
        self.signing = self.tmp / 'signing'
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.signing)])
        self.signers = self.tmp / 'allowed-signers'
        self.signers.write_text(f'* namespaces="git" {(self.tmp / "signing.pub").read_text()}')
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
            'if [ "$2" = edit ]; then export EDITOR="$TEST_EDITOR"; fi\n'
            'if [ "$2" = rekey ] && [ -n "$TEST_FAIL_REKEY" ]; then exit 1; fi\nexec "$@"\n')
        (repo / 'scripts/verify-ansible-args.sh').chmod(0o755)
        (repo / 'scripts/vault-pass.sh').write_text(f'#!/bin/sh\necho old-password-{name}\n')
        (repo / 'scripts/vault-pass.sh').chmod(0o700)
        (repo / 'config.yml').write_text("dotfiles_github_user: !unsafe 'tester'  # comment\n")
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
        # Portable (BSD sed's -i needs a suffix argument): a python script rewrites the file ansible-vault hands over.
        editor.write_text(f'#!{sys.executable}\nimport re, sys\npath = sys.argv[1]\n'
                          'text = re.sub(r"(?m)^vault_codex_api_key: .*", "vault_codex_api_key: edited", open(path).read())\n'
                          'open(path, "w").write(text)\n')
        editor.chmod(0o755)
        home = self.tmp / f'home-{name}'
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'ANSIBLE_'))}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), NO_COLOR='1',
                   PATH=f'{VENV_BIN}:{os.environ["PATH"]}', SECRETS_DIR=str(self.tmp / f'secrets-{name}'),
                   SECRETS_REMOTE=str(self.remote), VAULT_AGE_IDENTITIES=str(self.keys[name][0]),
                   ANSIBLE_VAULT_PASSWORD_FILE='scripts/vault-pass.sh', TEST_EDITOR=str(editor),
                   GIT_CONFIG_GLOBAL=str(home / 'gitconfig'), GIT_CONFIG_NOSYSTEM='1',
                   VAULT_ALLOWED_SIGNERS=str(self.signers), VAULT_JOIN_CONFIRMED='1')
        Path(env['GIT_CONFIG_GLOBAL']).write_text(
            f'[user]\n\tname = t\n\temail = t@t\n\tsigningkey = {self.signing}\n[gpg]\n\tformat = ssh\n'
            '[init]\n\tdefaultBranch = main\n[safe]\n\tbareRepository = all\n')
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
        run(['git', '-C', str(secrets_a), 'commit', '-S', '-qam', 'rogue'], env=self.env('a', a))
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

    def rogue_push(self, a):
        rogue = run(['age-keygen']).stdout.split('public key: ')[1].split()[0]
        secrets_a = self.tmp / 'secrets-a'
        with open(secrets_a / 'recipients', 'a') as f:
            f.write(f'{rogue}\n')
        run(['git', '-C', str(secrets_a), 'commit', '-S', '-qam', 'rogue'], env=self.env('a', a))
        run(['git', '-C', str(secrets_a), 'push', '-q'], env=self.env('a', a))
        return rogue

    def test_rejoining_does_not_approve_a_pushed_recipient(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        self.shared('b', b, 'join')
        self.rogue_push(a)
        rejoin = self.shared('b', b, 'join', check=False)
        self.assertNotEqual(rejoin.returncode, 0)
        self.assertIn('never approved', rejoin.stderr)

    def test_a_failed_remove_key_changes_nothing(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        secrets = self.tmp / 'secrets-a'
        before = {f: (secrets / f).read_bytes() for f in ('recipients', 'vault-pass.age', 'vault.yml')}
        failed = self.shared('a', a, 'remove-key', self.keys['b'][1], check=False, TEST_FAIL_REKEY='1')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual({f: (secrets / f).read_bytes() for f in before}, before)
        self.assertEqual(run(['git', '-C', str(secrets), 'status', '--porcelain'], env=self.env('a', a)).stdout, '')

    def test_a_failed_add_key_leaves_the_list_alone(self):
        a = self.init_a()
        recipients = (self.tmp / 'secrets-a/recipients').read_text()
        wrong = self.tmp / 'wrong-key'
        run(['age-keygen', '-o', str(wrong)])
        failed = self.shared('a', a, 'add-key', check=False, RECIPIENT=self.keys['b'][1], VAULT_AGE_IDENTITIES=str(wrong))
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((self.tmp / 'secrets-a/recipients').read_text(), recipients)
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])  # a retry works
        self.assertIn(self.keys['b'][1], (self.tmp / 'secrets-a/recipients').read_text())

    def test_a_failed_init_rolls_back_and_can_run_again(self):
        a = self.machine('a', 'vault_codex_api_key: secret-a\n')
        run(['git', 'clone', '-q', str(self.remote), str(self.tmp / 'secrets-a')], env=self.env('a', a))
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['a'][1])
        helper = (a / 'scripts/vault-pass.sh').read_text()
        failed = self.shared('a', a, 'init', check=False, TEST_FAIL_REKEY='1')
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((a / 'scripts/vault-pass.sh').read_text(), helper)
        self.assertFalse((self.tmp / 'secrets-a/vault-pass.age').exists())
        self.assertIn('active', self.shared('a', a, 'init').stdout)
        self.assertIn('secret-a', self.view('a', a))

    def test_the_github_user_is_read_through_an_unsafe_tag(self):
        a = self.machine('a', None)
        env = {k: v for k, v in self.env('a', a).items() if k not in ('SECRETS_DIR', 'SECRETS_REMOTE')}
        status = run(['bash', str(a / 'scripts/vault-shared.sh'), 'status'], cwd=a, env=env).stdout
        self.assertIn('/src/tester/laptop-secrets', status)

    def attacker_commit(self, a, sign_on_top=False, symlink=False):
        """An unsigned commit (someone with push access but not the signing key) replacing vault-pass.age."""
        secrets_a = self.tmp / 'secrets-a'
        env = self.env('a', a)
        (secrets_a / 'vault-pass.age').write_bytes(b'attacker')
        if symlink:
            (secrets_a / 'evil').symlink_to(Path(env['HOME']) / '.zshrc')
        run(['git', '-C', str(secrets_a), 'add', '-A'], env=env)
        run(['git', '-C', str(secrets_a), '-c', 'commit.gpgsign=false', 'commit', '-qm', 'evil'], env=env)
        if sign_on_top:
            (secrets_a / 'README.md').write_text('harmless\n')
            run(['git', '-C', str(secrets_a), 'commit', '-S', '-qam', 'looks fine'], env=env)
        run(['git', '-C', str(secrets_a), 'push', '-q'], env=env)

    def joined_pair(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        self.shared('b', b, 'join')
        return a, b

    def assert_refused_and_unchanged(self, b, result, text):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(text, result.stderr)
        self.assertNotEqual((self.tmp / 'secrets-b/vault-pass.age').read_bytes(), b'attacker')

    def test_an_unsigned_commit_is_never_merged(self):
        a, b = self.joined_pair()
        self.attacker_commit(a)
        self.assert_refused_and_unchanged(b, self.shared('b', b, 'pull', check=False), 'not signed')

    def test_a_signed_commit_on_top_does_not_hide_an_unsigned_one(self):
        a, b = self.joined_pair()
        self.attacker_commit(a, sign_on_top=True)
        self.assert_refused_and_unchanged(b, self.shared('b', b, 'pull', check=False), 'not signed')

    def test_a_fresh_join_refuses_an_unsigned_history(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        self.attacker_commit(a, sign_on_top=True)
        b = self.machine('b', None)
        result = self.shared('b', b, 'join', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not signed', result.stderr)
        self.assertIn('old-password', (b / 'scripts/vault-pass.sh').read_text())

    def test_a_committed_symlink_is_refused(self):
        a, b = self.joined_pair()
        secrets_a = self.tmp / 'secrets-a'
        (secrets_a / 'evil').symlink_to('/etc/hostname')
        run(['git', '-C', str(secrets_a), 'add', 'evil'], env=self.env('a', a))
        run(['git', '-C', str(secrets_a), 'commit', '-S', '-qm', 'link'], env=self.env('a', a))
        run(['git', '-C', str(secrets_a), 'push', '-q'], env=self.env('a', a))
        result = self.shared('b', b, 'pull', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('non-regular', result.stderr)

    def test_rejoining_keeps_the_machines_own_vault(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', 'vault_only_on_b: x\n')
        own = (b / 'group_vars/all/vault.yml').read_bytes()
        self.shared('b', b, 'join')
        self.shared('a', a, 'edit')
        self.shared('b', b, 'join')
        self.assertEqual((b / 'group_vars/all/vault.yml.pre-shared').read_bytes(), own)

    def snapshot(self, name):
        secrets = self.tmp / f'secrets-{name}'
        files = {f: (secrets / f).read_bytes() for f in ('recipients', 'vault-pass.age', 'vault.yml') if (secrets / f).exists()}
        head = run(['git', '-C', str(secrets), 'rev-parse', 'HEAD'], check=False).stdout.strip()
        return files, head

    # GIT_CONFIG_COUNT injects config for one run: a signing key that does not exist, or an unreachable push URL.
    NO_SIGNING = dict(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='user.signingkey', GIT_CONFIG_VALUE_0='/nonexistent')
    NO_PUSH = dict(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='remote.origin.pushurl', GIT_CONFIG_VALUE_0='/nonexistent')

    def test_a_hand_made_clone_with_unsigned_history_is_refused(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        self.attacker_commit(a)
        b = self.machine('b', None)
        run(['git', 'clone', '-q', str(self.remote), str(self.tmp / 'secrets-b')], env=self.env('b', b))
        result = self.shared('b', b, 'join', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not signed', result.stderr)
        self.assertIn('old-password', (b / 'scripts/vault-pass.sh').read_text())

    def test_a_rollback_to_an_older_signed_commit_is_refused(self):
        a, b = self.joined_pair()
        before = run(['git', '-C', str(self.tmp / 'secrets-a'), 'rev-parse', 'HEAD']).stdout.strip()
        self.shared('a', a, 'edit')
        self.shared('b', b, 'pull')
        run(['git', '-C', str(self.tmp / 'secrets-a'), 'push', '-qf', 'origin', f'{before}:main'], env=self.env('a', a))
        result = self.shared('b', b, 'pull', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('history was rewritten', result.stderr)

    def test_a_remove_key_whose_commit_fails_restores_everything(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        before = self.snapshot('a')
        failed = self.shared('a', a, 'remove-key', self.keys['b'][1], check=False, **self.NO_SIGNING)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.snapshot('a'), before)
        self.assertIn('secret-a', self.view('a', a))
        self.shared('a', a, 'remove-key', self.keys['b'][1])  # a retry works
        self.assertNotIn(self.keys['b'][1], (self.tmp / 'secrets-a/recipients').read_text())

    def test_an_add_key_whose_push_fails_restores_everything(self):
        a = self.init_a()
        before = self.snapshot('a')
        failed = self.shared('a', a, 'add-key', check=False, RECIPIENT=self.keys['b'][1], **self.NO_PUSH)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.snapshot('a'), before)
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        pushed = run(['git', f'--git-dir={self.remote}', 'show', 'main:recipients']).stdout
        self.assertIn(self.keys['b'][1], pushed)

    def test_an_init_whose_push_fails_rolls_back_including_the_commit(self):
        a = self.machine('a', 'vault_codex_api_key: secret-a\n')
        run(['git', 'clone', '-q', str(self.remote), str(self.tmp / 'secrets-a')], env=self.env('a', a))
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['a'][1])
        helper = (a / 'scripts/vault-pass.sh').read_text()
        failed = self.shared('a', a, 'init', check=False, **self.NO_PUSH)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual((a / 'scripts/vault-pass.sh').read_text(), helper)
        self.assertFalse((self.tmp / 'secrets-a/vault-pass.age').exists())
        self.assertEqual(self.snapshot('a')[1], 'HEAD')  # no commit left behind (rev-parse echoes the unborn name)
        self.shared('a', a, 'init')
        self.assertIn('secret-a', self.view('a', a))

    def test_an_edit_whose_push_fails_is_kept_aside(self):
        a = self.init_a()
        before = self.snapshot('a')
        failed = self.shared('a', a, 'edit', check=False, **self.NO_PUSH)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.snapshot('a'), before)
        kept = next((Path(self.env('a', a)['XDG_CONFIG_HOME']) / 'laptop-setup').glob('vault.yml.unpushed.*'))
        self.assertIn('vault_codex_api_key: edited', self.view('a', a, str(kept)))

    def test_signers_come_from_allowed_signers_and_must_match_a_pin(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        env_b = self.env('b', b)
        (Path(env_b['HOME']) / '.config/git').mkdir(parents=True)
        (Path(env_b['HOME']) / '.config/git/allowed_signers').write_text(self.signers.read_text())
        fresh = str(self.tmp / 'signers-b')  # no cache: built from allowed_signers
        wrong = self.shared('b', b, 'join', check=False, VAULT_ALLOWED_SIGNERS=fresh,
                            VAULT_SIGNING_FINGERPRINTS='SHA256:notyours')
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('match secrets_signing_fingerprints', wrong.stderr)
        fingerprint = run(['ssh-keygen', '-lf', str(self.tmp / 'signing.pub')]).stdout.split()[1]
        # The pin as config.yml holds it (the colon inside SHA256: must survive parsing).
        with open(b / 'config.yml', 'a') as f:
            f.write(f'secrets_signing_fingerprints: !unsafe "SHA256:other {fingerprint}"  # mine\n')
        self.shared('b', b, 'join', VAULT_ALLOWED_SIGNERS=fresh)
        self.assertIn('secret-a', self.view('b', b))

    def test_a_fresh_join_must_confirm_the_head_it_trusts(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        result = self.shared('b', b, 'join', check=False, VAULT_JOIN_CONFIRMED='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('confirm the newest vault change', result.stderr)
        self.assertIn('Recipients: add a key', result.stderr)  # what it would trust is shown

    def test_an_interrupt_after_the_push_landed_keeps_the_change(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        # A git that completes the push, then dies as if Ctrl-C arrived while it was finishing.
        wrapper = self.tmp / 'gitwrap'
        wrapper.mkdir()
        real = shutil.which('git')
        (wrapper / 'git').write_text(f'#!/bin/sh\nfor a in "$@"; do [ "$a" = push ] && {{ "{real}" "$@"; exit 130; }}; done\n'
                                     f'exec "{real}" "$@"\n')
        (wrapper / 'git').chmod(0o755)
        path = f'{wrapper}:{self.env("a", a)["PATH"]}'
        result = self.shared('a', a, 'remove-key', self.keys['b'][1], check=False, PATH=path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('reached GitHub', result.stderr)
        pushed = run(['git', f'--git-dir={self.remote}', 'show', 'main:recipients']).stdout
        self.assertNotIn(self.keys['b'][1], pushed)
        self.assertNotIn(self.keys['b'][1], (self.tmp / 'secrets-a/recipients').read_text())  # not rolled back
        self.shared('a', a, 'pull')
        self.assertIn('secret-a', self.view('a', a))

    def test_offline_after_the_fetch_still_rolls_back(self):
        """The push and the follow-up ls-remote both fail (the network dropped): init is undone completely."""
        a = self.machine('a', 'vault_codex_api_key: secret-a\n')
        run(['git', 'clone', '-q', str(self.remote), str(self.tmp / 'secrets-a')], env=self.env('a', a))
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['a'][1])
        helper = (a / 'scripts/vault-pass.sh').read_text()
        wrapper = self.tmp / 'offline'
        wrapper.mkdir()
        real = shutil.which('git')
        (wrapper / 'git').write_text(f'#!/bin/sh\nfor a in "$@"; do case "$a" in push|ls-remote) exit 128;; esac; done\n'
                                     f'exec "{real}" "$@"\n')
        (wrapper / 'git').chmod(0o755)
        failed = self.shared('a', a, 'init', check=False, PATH=f'{wrapper}:{self.env("a", a)["PATH"]}')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('stopped before the change was pushed', failed.stderr)
        self.assertEqual((a / 'scripts/vault-pass.sh').read_text(), helper)
        self.assertEqual(self.snapshot('a')[1], 'HEAD')
        self.shared('a', a, 'init')
        self.assertIn('secret-a', self.view('a', a))

    def forged_push(self, a, header):
        """A signed commit on GitHub whose raw object gets an extra signature header (a PGP block posing as SSH)."""
        secrets_a, env = self.tmp / 'secrets-a', self.env('a', a)
        (secrets_a / 'README.md').write_text('changed\n')
        run(['git', '-C', str(secrets_a), 'commit', '-S', '-qam', 'readme'], env=env)
        raw = run(['git', '-C', str(secrets_a), 'cat-file', 'commit', 'HEAD'], env=env).stdout
        head, _, body = raw.partition('\ngpgsig ')
        forged = f'{head}\n{header}\ngpgsig {body}'
        sha = run(['git', '-C', str(secrets_a), 'hash-object', '-t', 'commit', '-w', '--stdin'], env=env,
                  input=forged).stdout.strip()
        run(['git', '-C', str(secrets_a), 'push', '-qf', 'origin', f'{sha}:main'], env=env)

    def test_a_second_signature_header_is_refused(self):
        a, b = self.joined_pair()
        self.forged_push(a, 'gpgsig -----BEGIN PGP SIGNATURE-----\n \n -----END PGP SIGNATURE-----')
        self.assert_refused_and_unchanged(b, self.shared('b', b, 'pull', check=False), 'not signed')

    def test_a_pgp_style_commit_with_the_ssh_marker_in_its_message_is_refused(self):
        a, b = self.joined_pair()
        secrets_a, env = self.tmp / 'secrets-a', self.env('a', a)
        (secrets_a / 'vault-pass.age').write_bytes(b'attacker')
        run(['git', '-C', str(secrets_a), '-c', 'commit.gpgsign=false', 'commit', '-qam',
             'x\n\ngpgsig -----BEGIN SSH SIGNATURE-----'], env=env)
        run(['git', '-C', str(secrets_a), 'push', '-q'], env=env)
        self.assert_refused_and_unchanged(b, self.shared('b', b, 'pull', check=False), 'not signed')

    def test_the_pin_also_filters_a_cached_signer_list(self):
        a, b = self.joined_pair()
        self.shared('a', a, 'edit')
        result = self.shared('b', b, 'pull', check=False, VAULT_SIGNING_FINGERPRINTS='SHA256:notyours')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('match secrets_signing_fingerprints', result.stderr)

    def test_an_expired_signer_in_allowed_signers_is_not_trusted(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        env_b = self.env('b', b)
        (Path(env_b['HOME']) / '.config/git').mkdir(parents=True)
        pub = (self.tmp / 'signing.pub').read_text().strip()
        (Path(env_b['HOME']) / '.config/git/allowed_signers').write_text(f't@t namespaces="git",valid-before="20000101" {pub}\n')
        result = self.shared('b', b, 'join', check=False, VAULT_ALLOWED_SIGNERS=str(self.tmp / 'signers-b'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not signed', result.stderr)

    def test_a_key_limited_to_another_namespace_is_not_a_vault_signer(self):
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        env_b = self.env('b', b)
        (Path(env_b['HOME']) / '.config/git').mkdir(parents=True)
        pub = (self.tmp / 'signing.pub').read_text().strip()
        (Path(env_b['HOME']) / '.config/git/allowed_signers').write_text(f't@t namespaces="file,github" {pub}\n')
        result = self.shared('b', b, 'join', check=False, VAULT_ALLOWED_SIGNERS=str(self.tmp / 'signers-b'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not signed', result.stderr)  # ssh-keygen refuses the key for the git namespace

    def trusted_gpg_home(self):
        """A gpg home holding a key gpg trusts (it made it): what an attacker with a key in your keyring would use."""
        home = self.tmp / 'gnupg'
        home.mkdir(mode=0o700)
        env = dict(os.environ, GNUPGHOME=str(home))
        run(['gpg', '--batch', '--passphrase', '', '--quick-gen-key', 'Evil <e@e.test>', 'ed25519', 'sign', 'never'],
            env=env)
        fpr = run(['gpg', '--batch', '--with-colons', '--list-secret-keys'], env=env).stdout
        fpr = next(line.split(':')[9] for line in fpr.splitlines() if line.startswith('fpr:'))
        return str(home), fpr

    @unittest.skipUnless(shutil.which('gpg'), 'gpg is not installed')
    def test_a_commit_signed_by_a_gpg_trusted_key_is_refused(self):
        a, b = self.joined_pair()
        gnupg, fpr = self.trusted_gpg_home()
        secrets_a, env = self.tmp / 'secrets-a', dict(self.env('a', a), GNUPGHOME=gnupg)
        (secrets_a / 'vault-pass.age').write_bytes(b'attacker')
        run(['git', '-C', str(secrets_a), '-c', 'gpg.format=openpgp', '-c', f'user.signingkey={fpr}', 'commit', '-S',
             '-qam', 'pgp'], env=env)
        # The second variant: the same PGP signature with an SSH marker header added after it.
        raw = run(['git', '-C', str(secrets_a), 'cat-file', 'commit', 'HEAD'], env=env).stdout
        head, _, body = raw.partition('\n\n')
        doubled = f'{head}\ngpgsig -----BEGIN SSH SIGNATURE-----\n\n{body}'
        for variant in ('plain', 'doubled'):
            if variant == 'doubled':
                sha = run(['git', '-C', str(secrets_a), 'hash-object', '-t', 'commit', '-w', '--stdin'], env=env,
                          input=doubled).stdout.strip()
                run(['git', '-C', str(secrets_a), 'push', '-qf', 'origin', f'{sha}:main'], env=env)
            else:
                run(['git', '-C', str(secrets_a), 'push', '-q'], env=env)
            result = self.shared('b', b, 'pull', check=False, GNUPGHOME=gnupg)
            self.assert_refused_and_unchanged(b, result, 'not signed')

    def test_a_different_signer_set_verifies_the_history_again(self):
        a, b = self.joined_pair()
        self.shared('a', a, 'edit')
        self.shared('b', b, 'pull')  # verified and cached under the owner's key
        other = self.tmp / 'other'
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)])
        env_b = self.env('b', b)
        (Path(env_b['HOME']) / '.config/git').mkdir(parents=True, exist_ok=True)
        (Path(env_b['HOME']) / '.config/git/allowed_signers').write_text(
            f't@t namespaces="git" {(self.tmp / "other.pub").read_text()}')
        result = self.shared('b', b, 'pull', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not signed', result.stderr)

    def test_add_key_refuses_a_deleted_recipients_file(self):
        a, b = self.joined_pair()
        (self.tmp / 'secrets-b/recipients').unlink()
        third = run(['age-keygen']).stdout.split('public key: ')[1].split()[0]
        result = self.shared('b', b, 'add-key', check=False, RECIPIENT=third)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uncommitted changes', result.stderr)
        pushed = run(['git', f'--git-dir={self.remote}', 'show', 'main:recipients']).stdout
        self.assertIn(self.keys['a'][1], pushed)

    def test_signer_options_reach_ssh_keygen_unchanged(self):
        """Options ssh-keygen accepts, such as a namespace pattern, keep a key usable (they pass through unchanged)."""
        a = self.init_a()
        self.shared('a', a, 'add-key', RECIPIENT=self.keys['b'][1])
        b = self.machine('b', None)
        env_b = self.env('b', b)
        (Path(env_b['HOME']) / '.config/git').mkdir(parents=True)
        pub = (self.tmp / 'signing.pub').read_text().strip()
        (Path(env_b['HOME']) / '.config/git/allowed_signers').write_text(f't@t namespaces="file,g*" {pub}\n')
        self.shared('b', b, 'join', VAULT_ALLOWED_SIGNERS=str(self.tmp / 'signers-b'))
        self.assertIn('secret-a', self.view('b', b))

    def test_add_key_refuses_a_clone_whose_files_are_all_gone(self):
        a, b = self.joined_pair()
        for f in ('vault.yml', 'vault-pass.age', 'recipients', 'README.md', '.gitattributes'):
            (self.tmp / 'secrets-b' / f).unlink()
        third = run(['age-keygen']).stdout.split('public key: ')[1].split()[0]
        result = self.shared('b', b, 'add-key', check=False, RECIPIENT=third)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing its files', result.stderr)

    def test_a_list_form_pin_is_refused(self):
        a = self.init_a()
        b = self.machine('b', None)
        with open(b / 'config.yml', 'a') as f:
            f.write('secrets_signing_fingerprints:\n  - "SHA256:x"\n')
        result = self.shared('b', b, 'join', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('one quoted string', result.stderr)

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
