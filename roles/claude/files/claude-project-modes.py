#!/usr/bin/env python3
"""Keep every Claude Code session on this machine in one approval mode (claude_default_mode).

A repository's committed .claude/settings.json `permissions.defaultMode` outranks the user default, so a session
started there without a launcher flag (an autostarted unit, plain `claude`, an IDE) keeps the repository's mode.
The git-ignored .claude/settings.local.json outranks the committed file, so --apply writes the wanted mode there,
and only that key, in each checkout (worktrees included) whose project settings pick another mode. A checkout where
settings.local.json would not be git-ignored is reported, never written.

--check reports, without writing, those checkouts and every autostarted interactive `claude` (systemd user units,
macOS LaunchAgents) that passes no mode flag and whose working directory resolves to another mode. An explicit
--permission-mode or --dangerously-skip-permissions on the command line is a deliberate choice and passes.

Checkouts listed in the exclude file (default ~/.config/claude/project-mode-exclude, written by the agents chunk
from claude_project_mode_exclude: one path per line, ~ expanded, # comments; a path covers everything below it) keep
their own mode, and units starting there are not reported.

Output: one JSON object {"changed": [...], "problems": [...]}. Exit 1 with --check when there are problems, or
with --apply when a problem remains.
"""

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {"node_modules", "vendor", "venv", "__pycache__", "Library", "Applications", "snap", "go", "Downloads"}


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mode_of(path):
    data = read_json(path)
    perms = data.get("permissions") if data else None
    mode = perms.get("defaultMode") if isinstance(perms, dict) else None
    return mode if isinstance(mode, str) else None


def checkout_of(path):
    """The checkout containing path (its top level), or None."""
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def worktrees(repo):
    try:
        out = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True,
                             text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [Path(line[9:]) for line in out.stdout.splitlines() if line.startswith("worktree ")]


def find_checkouts(roots, depth):
    """Git checkouts under roots, at most depth levels down, hidden and dependency directories pruned, plus each
    one's worktrees (agent worktrees under .claude/worktrees included)."""
    found = set()
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        base = len(root.parts)
        for cur, dirs, _files in os.walk(root):
            here = Path(cur)
            if (here / ".git").exists():
                found.add(here)
                dirs[:] = []
                continue
            if len(here.parts) - base >= depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
    for repo in sorted(found):
        found.update(w for w in worktrees(repo) if w.is_dir())
    return sorted(found)


def project_mode(checkout):
    """(effective project-level mode or None, the file that sets it)."""
    local = checkout / ".claude" / "settings.local.json"
    shared = checkout / ".claude" / "settings.json"
    for path in (local, shared):
        mode = mode_of(path)
        if mode:
            return mode, path
    return None, None


def ignored(checkout, rel):
    try:
        return subprocess.run(["git", "-C", str(checkout), "check-ignore", "-q", "--no-index", rel],
                              capture_output=True, timeout=30, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def set_local_mode(checkout, mode):
    path = checkout / ".claude" / "settings.local.json"
    data = read_json(path) if path.exists() else {}
    if data is None:
        raise ValueError(f"{path} is not a JSON object")
    data.setdefault("permissions", {})
    if not isinstance(data["permissions"], dict):
        raise ValueError(f"{path} permissions is not an object")
    data["permissions"]["defaultMode"] = mode
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_excludes(path):
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except OSError:
        return []
    return [Path(os.path.expanduser(line.strip())).resolve() for line in lines
            if line.strip() and not line.strip().startswith("#")]


def excluded(path, excludes):
    path = Path(path).resolve()
    return any(path == ex or ex in path.parents for ex in excludes)


def check_checkouts(checkouts, mode, apply, excludes=()):
    changed, problems = [], []
    for co in checkouts:
        if excluded(co, excludes):
            continue
        got, source = project_mode(co)
        if got is None or got == mode:
            continue
        if not apply:
            problems.append(f"{co}: {source.name} sets defaultMode {got}, outranking {mode}")
            continue
        if not ignored(co, ".claude/settings.local.json"):
            problems.append(f"{co}: {source.name} sets defaultMode {got}; .claude/settings.local.json is not "
                            "git-ignored there, so it was not written (add it to the global gitignore: make dotfiles)")
            continue
        try:
            set_local_mode(co, mode)
        except (OSError, ValueError) as exc:
            problems.append(f"{co}: {exc}")
            continue
        changed.append(f"{co}: settings.local.json defaultMode {got} -> {mode}")
    return changed, problems


def user_mode(env):
    config = env.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return mode_of(Path(os.path.expanduser(config)) / "settings.json")


def claude_argv(argv):
    """The part of argv from the claude executable on, or None (wrappers such as tmux or env come first)."""
    for i, arg in enumerate(argv):
        if os.path.basename(arg) == "claude":
            return argv[i:]
    return None


def launch_problem(name, argv, workdir, env, mode):
    args = claude_argv(argv)
    if args is None or any(a in ("-p", "--print") for a in args):
        return None  # not claude, or a one-shot print run (those pin their own --permission-mode)
    if any(a == "--dangerously-skip-permissions" or a.startswith("--permission-mode") for a in args):
        return None
    got = None
    if workdir:
        top = checkout_of(workdir)
        got = project_mode(top)[0] if top else None
    got = got or user_mode(env)
    if got == mode:
        return None
    return (f"{name}: starts claude without a mode flag in {workdir or '~'}, which resolves to {got or 'default'}; "
            f"add --dangerously-skip-permissions to its command or remove the project's defaultMode")


def systemd_units(unit_dir):
    for path in sorted(Path(unit_dir).glob("*.service")):
        try:
            text = path.read_text()
        except OSError:
            continue
        workdir, env, cmds = None, {}, []
        for line in text.splitlines():
            key, _, value = line.strip().partition("=")
            if key == "WorkingDirectory":
                workdir = os.path.expanduser(value.lstrip("-").replace("%h", os.path.expanduser("~")))
            elif key == "Environment":
                for item in shlex.split(value):
                    k, _, v = item.partition("=")
                    env[k] = v
            elif key in ("ExecStart", "ExecStartPost"):
                try:
                    cmds.append(shlex.split(value.lstrip("-@+!:")))
                except ValueError:
                    continue
        for argv in cmds:
            yield path.name, argv, workdir, env


def launch_agents(agent_dir):
    for path in sorted(Path(agent_dir).glob("*.plist")):
        try:
            with path.open("rb") as fh:
                data = plistlib.load(fh)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        argv = data.get("ProgramArguments") or ([data["Program"]] if data.get("Program") else [])
        if len(argv) >= 3 and os.path.basename(argv[0]) in ("sh", "bash", "zsh") and argv[1] == "-c":
            try:
                argv = shlex.split(argv[2])
            except ValueError:
                continue
        yield path.name, argv, data.get("WorkingDirectory"), data.get("EnvironmentVariables") or {}


def check_launches(mode, unit_dir, agent_dir, excludes=()):
    problems = []
    for name, argv, workdir, env in [*systemd_units(unit_dir), *launch_agents(agent_dir)]:
        if workdir and excluded(workdir, excludes):
            continue
        msg = launch_problem(name, argv, workdir, env, mode)
        if msg:
            problems.append(msg)
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--apply", action="store_true")
    group.add_argument("--check", action="store_true")
    ap.add_argument("--mode", default="bypassPermissions")
    ap.add_argument("--root", action="append", default=[], help="directory to search for checkouts (repeatable)")
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--unit-dir", default="~/.config/systemd/user")
    ap.add_argument("--agent-dir", default="~/Library/LaunchAgents")
    ap.add_argument("--exclude-file", default="~/.config/claude/project-mode-exclude")
    args = ap.parse_args()
    roots = args.root or [os.path.expanduser("~")]
    excludes = read_excludes(args.exclude_file)
    changed, problems = check_checkouts(find_checkouts(roots, args.depth), args.mode, args.apply, excludes)
    problems += check_launches(args.mode, os.path.expanduser(args.unit_dir), os.path.expanduser(args.agent_dir),
                               excludes)
    print(json.dumps({"changed": changed, "problems": problems}, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
