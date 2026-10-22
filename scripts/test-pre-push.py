#!/usr/bin/env python3
"""Exercise pre-push enforcement without network access or private signing keys."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HOOK = Path(__file__).resolve().parent.parent / ".githooks/pre-push"
GIT = shutil.which("git")
ZERO = "0" * 40


class PrePushTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ)
        self.env.update({
            "PATH": str(self.tools) + os.pathsep + os.environ["PATH"],
            "TEST_GITLEAKS_LOG": str(self.root / "scans.log"),
            "TEST_VERIFY_RESULT": "0",
            "TEST_SCAN_RESULT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        })
        # Stub only the signature result; routing and commit inspection use real Git.
        (self.tools / "git").write_text(
            '#!/bin/bash\nif [ "$1" = verify-commit ]; then exit "$TEST_VERIFY_RESULT"; fi\n'
            'exec "' + GIT + '" "$@"\n'
        )
        (self.tools / "gitleaks").write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_GITLEAKS_LOG"\nexit "$TEST_SCAN_RESULT"\n'
        )
        for path in self.tools.iterdir():
            path.chmod(0o755)
        self.git("init", "-q")
        self.git("config", "user.name", "Test Author")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.hooksPath", ".githooks")
        (self.repo / "base.txt").write_text("baseline\n")
        self.git("add", "base.txt")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Baseline")
        helper = self.repo / "scripts/gpg-policy.sh"
        helper.parent.mkdir()
        shutil.copy2(HOOK.parent.parent / "scripts/gpg-policy.sh", helper)
        shutil.copy2(HOOK.parent.parent / "scripts/verify-vault-stub.py", helper.with_name("verify-vault-stub.py"))
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/main", self.base)

    def git(self, *args):
        return subprocess.check_output([GIT, *args], cwd=self.repo, env=self.env, text=True)

    def commit(self, signoff=True, critical=False, filename=None):
        path = self.repo / (filename or ("scripts/change.sh" if critical else "change.txt"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a change\n")
        self.git("add", str(path.relative_to(self.repo)))
        args = ["-c", "commit.gpgsign=false", "commit", "-qm", "Test change"]
        if signoff:
            args.append("--signoff")
        self.git(*args)
        return self.git("rev-parse", "HEAD").strip()

    def run_hook(self, updates):
        return subprocess.run(["bash", str(HOOK)], input="\n".join(updates) + "\n",
                              cwd=self.repo, env=self.env, capture_output=True, text=True)

    def update(self, tip, name="topic", remote=ZERO):
        return f"refs/heads/{name} {tip} refs/heads/{name} {remote}"

    def test_new_branch_requires_signoff(self):
        result = self.run_hook([self.update(self.commit(signoff=False))])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Signed-off-by", result.stderr)

    def test_new_branch_checks_critical_commit_signature(self):
        self.env["TEST_VERIFY_RESULT"] = "1"
        result = self.run_hook([self.update(self.commit(critical=True))])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("valid signature", result.stderr)

    def test_signature_covers_bootstrap_requirements_and_ci_configuration(self):
        self.env["TEST_VERIFY_RESULT"] = "1"
        for filename in (
            ".github/actions/ci-setup/action.yml",
            "roles/repos_dnf/defaults/main.yml",
            "requirements-bootstrap.txt",
            ".gitleaks.toml",
            "Makefile.sha256",
            "package-lock.json",
        ):
            with self.subTest(filename=filename):
                self.git("reset", "--hard", self.base)
                result = self.run_hook([self.update(self.commit(filename=filename))])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("valid signature", result.stderr)

    def test_disabled_policy_allows_unsigned_but_preserves_signoff_and_scans(self):
        (self.repo / ".gpg-enforcement").write_text("disabled\n")
        self.env["TEST_VERIFY_RESULT"] = "1"
        tip = self.commit(critical=True)
        result = self.run_hook([self.update(tip)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.env["TEST_SCAN_RESULT"] = "1"
        result = self.run_hook([self.update(tip)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Secret scanning failed", result.stderr)
        self.env["TEST_SCAN_RESULT"] = "0"
        tip = self.commit(signoff=False)
        result = self.run_hook([self.update(tip)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Signed-off-by", result.stderr)

    def test_invalid_policy_fails(self):
        (self.repo / ".gpg-enforcement").write_text("not-a-policy\n")
        result = self.run_hook([self.update(self.commit())])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("enabled or disabled", result.stderr)

    def test_vault_template_is_allowed_but_plaintext_values_are_blocked(self):
        # Inline stub: the tracked vault.yml may be encrypted once real secrets are added.
        template = "---\n# placeholder template\nvault_placeholder: encrypt-me\nvault_ssh_auth_key: ''\nvault_registry_tokens: []\n"
        vault = self.repo / "vault.yml"
        for content, accepted in [(template, True),
                                  (template + "other_key: plaintext-value\n", False),
                                  (template.replace("vault_ssh_auth_key: ''", "vault_ssh_auth_key: plaintext-value"), False)]:
            with self.subTest(accepted=accepted):
                vault.write_text(content)
                self.git("add", "vault.yml")
                tip = self.commit()
                result = self.run_hook([self.update(tip)])
                self.assertEqual(result.returncode, 0 if accepted else 1, result.stderr)
                if not accepted:
                    self.assertIn("not encrypted", result.stderr)

    def test_vault_check_reads_pushed_commit_not_working_tree(self):
        template = "---\nvault_placeholder: encrypt-me\nvault_key: []\n"
        vault = self.repo / "vault.yml"
        vault.write_text(template + "vault_key2: plaintext-value\n")
        self.git("add", "vault.yml")
        tip = self.commit()
        # The working tree is clean (stub) but the pushed commit holds plaintext.
        vault.write_text(template)
        result = self.run_hook([self.update(tip)])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not encrypted", result.stderr)
        # An earlier pushed commit with plaintext is also caught.
        self.git("add", "vault.yml")
        tip2 = self.commit(filename="more.txt")
        result = self.run_hook([self.update(tip2)])
        self.assertEqual(result.returncode, 1, result.stderr)
        # Deleting the file is fine.
        self.git("reset", "--hard", self.base)
        vault.write_text(template)
        self.git("add", "vault.yml")
        self.commit()
        self.git("rm", "-q", "vault.yml")
        self.git("-c", "commit.gpgsign=false", "commit", "-qsm", "Remove vault")
        result = self.run_hook([self.update(self.git("rev-parse", "HEAD").strip())])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_secret_scanner_failure_blocks_push(self):
        self.env["TEST_SCAN_RESULT"] = "1"
        result = self.run_hook([self.update(self.commit())])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Secret scanning failed", result.stderr)

    def test_checks_every_ref_and_uses_correct_base(self):
        pushed = self.commit(filename="already-pushed.txt")
        tip = self.commit(critical=True)
        result = self.run_hook([self.update(tip), self.update(tip, name="second", remote=pushed)])
        self.assertEqual(result.returncode, 0, result.stderr)
        scans = Path(self.env["TEST_GITLEAKS_LOG"]).read_text().splitlines()
        self.assertEqual(len(scans), 2)
        # A new branch scans from the merge-base; an existing one from its remote tip.
        self.assertIn(self.base + ".." + tip, scans[0])
        self.assertIn(pushed + ".." + tip, scans[1])

    def test_branch_deletion_does_not_inspect_commits(self):
        result = self.run_hook([self.update(ZERO, remote=self.base)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(Path(self.env["TEST_GITLEAKS_LOG"]).exists())


if __name__ == "__main__":
    unittest.main()
