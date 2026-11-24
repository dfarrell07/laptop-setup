#!/usr/bin/env python3
"""The Claude Code hook that sends file-writing heredocs to the Write tool (roles/claude/files)."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / 'roles/claude/files/claude-file-heredoc-guard.py'

# The background subagent's command that raised a removal-check prompt in bypass mode (2026-11-22): the quoted
# heredoc's make syntax ($$( ...) made Claude Code unsure how the shell would read it.
INCIDENT = r"""B=/private/tmp/claude-501/ega216rev; cat > $B/test.mk <<'EOF'
SHELL := /bin/bash
t:
	rm -rf $(ANSIBLE_216_DIR) && mkdir -p $(ANSIBLE_216_DIR)
	expected=$$(ls -d $(ANSIBLE)/roles/*/ | wc -l) && \
	echo SANITY-WOULD-RUN $$PWD
EOF
cd /Users/dfarrell07/src/evpn/mac2-clones/ega-216 && ANSIBLE_HOME=$B/home make -f $B/test.mk t; echo rc=$?; git status --short"""


def decide(command, tool='Bash'):
    result = subprocess.run([sys.executable, '-I', str(HOOK)], input=json.dumps(
        {'hook_event_name': 'PreToolUse', 'tool_name': tool, 'tool_input': {'command': command}}),
        capture_output=True, text=True, check=True)
    if not result.stdout:
        return 'pass'
    output = json.loads(result.stdout)['hookSpecificOutput']
    assert output['hookEventName'] == 'PreToolUse', output
    assert 'Write tool' in output['permissionDecisionReason'], output
    return output['permissionDecision']


class FileHeredocGuardTests(unittest.TestCase):
    def test_file_writing_heredocs_are_denied(self):
        for command in [INCIDENT,
                        "cat > notes.md <<'EOF'\nhi\nEOF",
                        "cat >> log.txt <<EOF\nhi\nEOF",
                        "cat <<'EOF' > /tmp/x.mk\nt:\nEOF",
                        "cat <<-EOF >| out\n\thi\nEOF",
                        'mkdir -p d && cat > d/f <<"EOF"\nx\nEOF',
                        "tee out.txt <<'EOF'\nhi\nEOF",
                        "tee -a out.txt <<'EOF' >/dev/null\nhi\nEOF",
                        "cat <<'EOF' | tee out.txt >/dev/null\nhi\nEOF",
                        "LC_ALL=C cat > f <<'EOF'\nx\nEOF",
                        "cat 1> f <<'EOF'\nx\nEOF",
                        "cat &> f <<'EOF'\nx\nEOF",
                        "cat <<\\EOF >f\nx\nEOF",
                        "cat <<EOF |& tee f\nx\nEOF",
                        "/bin/cat > f <<'EOF'\nx\nEOF",
                        "if true; then cat > f <<'EOF'\nx\nEOF\nfi",
                        "{ cat > f <<'EOF'\nx\nEOF\n}",
                        "tee -- f <<'EOF'\nx\nEOF",
                        "cat >\"$f\" <<'EOF'\nx\nEOF",
                        "git add -A; cat > a <<'A'\nx\nA\necho ok",
                        "> f cat <<'EOF'\nx\nEOF",
                        ">f cat <<'EOF'\nx\nEOF",
                        "(cat > f <<'EOF'\nx\nEOF\n)",
                        "cat > f <<'END-PY'\nx\nEND-PY",
                        "cat >f<<EOF\nx\nEOF",
                        "cat <<EOF >& out.txt\nx\nEOF",
                        "env cat > f <<'EOF'\nx\nEOF",
                        "cat <<'EOF' > \"$B/test.mk\"\nx\nEOF",
                        "f() { cat > out <<'EOF'\nx\nEOF\n}",
                        "x=$((1<<2))\ncat > f <<'EOF'\nx\nEOF",  # a shift is not a heredoc
                        'echo "((" ; cat > f <<EOF\nx\nEOF',  # quoted parentheses are not arithmetic
                        "grep -c '((' x.sh; cat > f <<'EOF'\nx\nEOF",
                        "v=$((((a)+1))); cat > f <<'EOF'\nx\nEOF",
                        "cat <<EOF\r\nhi\r\nEOF\r\ncat > f <<X\r\ny\r\nX\r\n",  # CRLF: the delimiter is EOF\r
                        "python3 - <<'EOF'\fx\nprint(1)\nEOF\fx\ncat > f <<'Z'\nz\nZ",  # the whole word ends it
                        "echo ${x//((/}; cat > f <<EOF\nx\nEOF",  # only a (( closed by )) is arithmetic
                        "out=$((cd . && cat > f <<EOF\nx\nEOF\n) )",  # $((cmd) ) is a command substitution
                        "for((i=0;i<1<<2;i++)); do :; done\ncat > f <<EOF\nx\nEOF",
                        "diff <((sort a) ) b; cat > f <<EOF\nx\nEOF",
                        'echo "$((1<<2))"\ncat > f <<\'EOF\'\nx\nEOF',  # arithmetic still expands in "..."
                        "n=$(basename $(pwd)); echo $((1<<2))\ncat > f <<'EOF'\nx\nEOF"]:
            with self.subTest(command=command[:40]):
                self.assertEqual(decide(command), 'deny')

    def test_heredocs_that_feed_a_program_or_need_root_pass(self):
        for command in ["git commit -s -F - <<'EOF'\nSubject\nEOF",
                        "python3 - <<'EOF'\nprint(1)\nEOF",
                        "cat <<'EOF' | kubectl apply -f -\nkind: x\nEOF",
                        "cat <<'EOF'\nprinted\nEOF",
                        "cat <<'EOF' >/dev/null\nx\nEOF",
                        "cat <<'EOF' >&2\nx\nEOF",
                        "cat <<'EOF' | sudo tee /etc/x >/dev/null\nx\nEOF",
                        "sudo tee /etc/x <<'EOF'\nx\nEOF",
                        "gh pr create --body-file - <<'EOF'\nbody\nEOF",
                        "python3 script.py > out.txt",
                        "cat a > b",
                        "echo '<<EOF' > f",
                        # a body or quoted text that only mentions a file-writing heredoc
                        "git commit -F - <<'EOF'\nUse cat > f <<EOF no more\nEOF",
                        "ssh host bash -s <<'O'\ncat > /etc/x <<'I'\nx\nI\nO",
                        'echo "a; cat > f <<EOF"',
                        "git commit -m 'x; cat > f <<EOF'",
                        "tee out <<<x",
                        'cat <<<"hello" > out',
                        "echo $((1<<3)) > n",
                        "cat <<'EOF' 2>err.log\nx\nEOF",
                        "cat <<'EOF' || echo failed > f\nx\nEOF",
                        "cat <<'EOF' ; tee log.txt | grep foo\nhi\nEOF",
                        "git apply <<'EOF'\n+cat > /tmp/gen.mk <<'GEN'\nEOF",
                        'git commit -m "Hook\n\ncat > f <<EOF is denied now"',
                        "gh pr create --body 'Agents wrote\ncat > test.mk <<EOF\nbefore'",
                        "python3 - <<'END-PY'\ncat > f <<X\nEND-PY",
                        "cat <<EOF > >(wc -l)\nx\nEOF",
                        "cat <<EOF >(wc -l)\nx\nEOF",
                        "ls  # e.g.; cat > f <<EOF",
                        "echo it\\'s; cat <<'EOF'\nx\nEOF",
                        "cat <<EOF | tee >(sha256sum) >/dev/null\nx\nEOF",
                        "tee >(wc -l) <<EOF\nx\nEOF",
                        "cat <<EOF | tee 2>/dev/null\nx\nEOF",
                        "cat <<EOF | tee 1>&2\nx\nEOF",
                        "cat <<EOF > \"/dev/null\"\nx\nEOF",
                        "tee '/dev/null' <<EOF\nx\nEOF",
                        "cat <<EOF >&2\nx\nEOF",
                        'git commit -m "$(cat <<\'EOF\'\nSay "hi\ncat > f <<X\nEOF\n)"',
                        "echo \"((\" ; git commit -F - <<'EOF'\nmsg\ncat > f <<X\nEOF",
                        "(cd /x && (ls)); tee out <<< $((1<<2))",
                        # bash ends lines only at \n: a \r or \f does not end a body
                        "git commit -F - <<'EOF'\nline\r\nEOF\r\ncat > f <<X\nEOF",
                        "python3 - <<'EOF'\nx = 1\x0cEOF\ncat > f <<X\ny\nX\nEOF"]:
            with self.subTest(command=command[:40]):
                self.assertEqual(decide(command), 'pass')

    def test_long_arguments_stay_fast(self):
        start = time.monotonic()
        self.assertEqual(decide("python3 - --arg '" + 'x' * 60000 + "' <<EOF\nx\nEOF"), 'pass')
        self.assertLess(time.monotonic() - start, 3)  # was 15 s+: the function-header regex retried every offset

    def test_other_tools_and_bad_input_pass(self):
        self.assertEqual(decide("cat > f <<'EOF'\nx\nEOF", tool='Write'), 'pass')
        for raw in ['not json', '[]', '{"tool_name": "Bash", "tool_input": "cat > f <<EOF"}']:
            result = subprocess.run([sys.executable, '-I', str(HOOK)], input=raw, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (0, ''), raw)


if __name__ == '__main__':
    unittest.main()
