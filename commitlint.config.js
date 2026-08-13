module.exports = {
  rules: {
    'header-max-length': [2, 'always', 72],
    'body-leading-blank': [2, 'always'],
    'body-max-line-length': [2, 'always', 200],
    // This repo uses "Scope: message" format (scope before colon = commitlint "type" field).
    // type-enum is severity 0 (off) — slash-combined scopes (System/Nm, Common/Molecule, etc.)
    // are too open-ended to enumerate; format is enforced by the commit-msg hook regex instead.
    // scope-enum stays at 0 (off) — no parenthetical scopes are used in this repo.
    'type-enum': [0, 'always', [
      'Common', 'Repos-dnf', 'Packages', 'Dotfiles', 'Ssh', 'Git-repos',
      'Notes', 'Redhat', 'Containers', 'Desktop', 'System', 'Distrobox',
      'Claude', 'Ci', 'Docs', 'Chore', 'Smoke', 'Molecule', 'Multi',
      'Sshd', 'Nm', 'Auditd', 'Backup', 'Handlers', 'Security',
      'Vault', 'Preflight', 'Simplify',
    ]],
    'subject-empty': [2, 'never'],
    'subject-full-stop': [2, 'never', '.'],
    'scope-empty': [2, 'always'],
  },
};
