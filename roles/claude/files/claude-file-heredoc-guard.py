#!/usr/bin/env python3
"""Claude Code PreToolUse hook: send file-writing heredocs to the Write tool.

Claude Code's built-in removal check parses every Bash command, heredoc bodies included, and asks a person when it
cannot be sure it read the command the way the shell will. Bypass mode and permission rules cannot answer that ask.
Literal data in a quoted heredoc trips it (make's `$$(`, for one), so writing a Makefile or script with
`cat > file <<'EOF'` can stall an unattended agent on a prompt. The Write tool takes the same content with no shell
parsing, so this hook denies `cat`/`tee` heredocs that write a local file and says to use Write instead. It only ever
denies: heredocs that feed a program (git commit -F -, python3 -, ssh host sh -s, kubectl apply -f -), `sudo tee`,
and heredoc bodies, quoted text or comments that merely mention such a command pass untouched.
"""
import json
import os
import re
import sys

# <<EOF, <<-'EOF', <<"END-PY", <<\EOF; not the <<< of a here-string. The rest of the word, such as the \r of a
# CRLF line, is part of the delimiter for bash, so a CRLF command's body ends at EOF\r.
HEREDOC = re.compile(r'(?<!<)<<(?!<)(-?)[ \t]*(?:\'([^\']+)\'|"([^"]+)"|\\?([^ \t\n;&|<>()\'"]+))([^ \t\n;&|<>()\'"]*)')
SEGMENT = re.compile(r'&&|\|\||;|\|&|(?<!>)\|')  # not the | of >|
ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=\S*$')
PREFIX = {'!', 'then', 'do', 'else', 'command', 'time', 'exec', 'env'}
REDIRECT_OP = re.compile(r'(?:1|&)?>>?\|?$')
# stdout (bare, 1>, &> or >&) to a target; 2>, >&N and >(process substitution) are not file writes of the heredoc text
REDIRECT = re.compile(r'(?:^|(?<=[\s;&|(]))(?:1|&)?>>?\|?(&?)\s*([^\s&|;<>()][^\s&|;<>]*)')
FD_REDIRECT = re.compile(r'^\d*[<>]')  # 2>/dev/null, 2>&1 and the like, as tee arguments
NOT_A_FILE = re.compile(r'^/dev/(?:null|stdout|stderr|fd/\d+|tty)$')
REASON = ('Write this file with the Write tool, not a Bash heredoc: Claude Code\'s removal check misreads some heredoc '
          'bodies (make\'s $$( for one) and then asks a person, even in bypass mode. Run the rest of the command '
          'separately once the file exists.')


def mask(line, quote):
    """Blank quoted text and comments to '_' (same length, so positions line up); return it and the quote left open."""
    out, i = [], 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == '\\' and quote == '"' and i + 1 < len(line):
                out.append('__')
                i += 2
                continue
            if ch == quote:
                quote = ''
            out.append('_')
        elif ch == '\\' and i + 1 < len(line):
            out.append('__')
            i += 2
            continue
        elif ch in '\'"':
            quote = ch
            out.append('_')
        elif ch == '#' and (i == 0 or line[i - 1] in ' \t;&|('):
            out.append('_' * (len(line) - i))
            break
        else:
            out.append(ch)
        i += 1
    return ''.join(out), quote


def words(segment):
    segment = re.sub(r'(?<![\w-])[A-Za-z_][\w-]*\(\)', ' ', segment)  # a function header, as in `f() { cat > out <<EOF`
    w = [x for x in segment.replace('(', ' ').replace('{', ' ').split() if not ENV.match(x)]
    while w and (w[0] in PREFIX or REDIRECT_OP.match(w[0]) or w[0].startswith(('>', '1>', '&>'))):
        w = w[2:] if REDIRECT_OP.match(w[0]) else w[1:]  # `> f cat` skips the operator and its target
    return [os.path.basename(w[0])] + w[1:] if w else []


def is_file(target):
    return not NOT_A_FILE.match(target.strip('\'"'))


def writes_file(segment, original):
    """A cat with a stdout redirect to a file, or a tee with a file argument. `segment` is the masked text, `original`
    the same span unmasked, where a quoted target such as "/dev/null" can still be read."""
    blank = re.compile(r'[<>]\([^)]*\)')  # process substitution is not a file
    segment = blank.sub(lambda m: ' ' * len(m.group(0)), segment)
    original = original[:len(segment)]
    w = words(segment)
    if w[:1] == ['cat']:
        return any(is_file(original[m.start(2):m.end(2)]) for m in REDIRECT.finditer(segment)
                   if not (m.group(1) and re.fullmatch(r'\d+|-', m.group(2))))  # >&2 is not a file
    if w[:1] == ['tee']:
        tokens = [(m.group(0), original[m.start():m.end()]) for m in re.finditer(r'\S+', segment)]
        start = next((i for i, (t, _) in enumerate(tokens) if os.path.basename(t.lstrip('({')) == 'tee'), -1) + 1
        return any(is_file(o) for t, o in tokens[start:]
                   if t != '--' and not t.startswith(('-', '<', '&')) and not FD_REDIRECT.match(t))
    return False


def end_word(m):
    """The word that ends this heredoc's body."""
    return (m.group(2) or m.group(3) or m.group(4)) + m.group(5)


def in_arithmetic(text):
    """For each position of text: whether it sits inside (( ... )) or $(( ... )), where << is a shift. Only a (( closed
    by )) is arithmetic: bash reads ((cmd) ) and an unclosed (( as subshells, and ${x//((/} is no arithmetic either."""
    close, opened = {}, []
    for i, ch in enumerate(text):
        if ch == '(':
            opened.append(i)
        elif ch == ')' and opened:
            close[opened.pop()] = i
    depth = [0] * (len(text) + 1)
    for i, j in close.items():
        if close.get(i + 1) == j - 1:  # (( ... )) with no gap between the closing parentheses
            depth[i + 2] += 1
            depth[j] -= 1
    inside, level = [], 0
    for d in depth:
        level += d
        inside.append(level > 0)
    return inside


def heredocs(line, shown):
    """The << operators in a line, not the shift in $((1<<2)). Parentheses count outside quotes and comments; for a <<
    inside double quotes, where $((...)) still expands, from the opening quote."""
    unquoted, quoted = in_arithmetic(shown), {}
    found = []
    for m in HEREDOC.finditer(line):
        if shown[m.start()] == '<':
            shift = unquoted[m.start()]
        else:
            q = line.rfind('"', 0, m.start()) + 1
            if q not in quoted:
                quoted[q] = in_arithmetic(line[q:])
            shift = quoted[q][m.start() - q]
        if not shift:
            found.append(m)
    return found


def file_heredoc(command):
    pending, quote = [], ''  # heredocs whose bodies follow (delimiter, strip tabs); a quote left open by a line
    for line in command.split('\n'):  # bash ends lines at \n only, not at \r or \f
        if pending:
            delimiter, strip = pending[0]
            if (line.lstrip('\t') if strip else line) == delimiter:
                pending.pop(0)
            continue
        shown, quote = mask(line, quote)
        ops = [m for m in heredocs(line, shown) if shown[m.start()] == '<']  # outside quotes and comments
        # "$(cat <<'EOF' ...)" (a commit message): skip that body too, though the heredoc only feeds the substitution
        pending.extend((end_word(m), m.group(1) == '-') for m in heredocs(line, shown)
                       if shown[m.start()] == '_' and '$(' in line[line.rfind('"', 0, m.start()):m.start()])
        if not ops:
            continue
        plain = line
        for m in ops:
            pending.append((end_word(m), m.group(1) == '-'))
            gap = ' ' * (m.end() - m.start())
            shown = shown[:m.start()] + gap + shown[m.end():]
            plain = plain[:m.start()] + gap + plain[m.end():]
        cuts = list(SEGMENT.finditer(shown))
        starts = [0] + [c.end() for c in cuts]
        ends = [c.start() for c in cuts] + [len(shown)]
        for m in ops:
            at = max(i for i, s in enumerate(starts) if s <= m.start())
            if writes_file(shown[starts[at]:ends[at]], plain[starts[at]:ends[at]]):
                return True
            piped = at < len(cuts) and cuts[at].group(0) in ('|', '|&')
            if (piped and words(shown[starts[at]:ends[at]])[:1] == ['cat']
                    and writes_file(shown[starts[at + 1]:ends[at + 1]], plain[starts[at + 1]:ends[at + 1]])):
                return True  # cat <<EOF | tee file
    return False


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get('tool_name') != 'Bash':
        return 0
    tool_input = event.get('tool_input')
    command = tool_input.get('command') if isinstance(tool_input, dict) else None
    if isinstance(command, str) and file_heredoc(command):
        json.dump({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                          'permissionDecisionReason': REASON}}, sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
