"""Shared by the adapters whose screens read shell commands: which kaiusap verbs a command runs."""
import re


def bash_verbs(command):
    """Find direct kaiusap calls, including command substitutions.

    shlex keeps quoted separators intact; punctuation runs are then split at
    shell operators. A substitution begins a new command even inside quotes.
    This is a recognizer, never a shell evaluator.
    """
    # Extract substitutions before shlex removes quoting; nested substitutions
    # are visited recursively. Their surrounding command is still inspected.
    substitutions = []
    spans = []
    stack = []
    quote, escaped = None, False
    i = 0
    while i < len(command):
        c = command[i]
        if escaped:
            escaped = False
        elif c == "#" and quote is None and (i == 0 or command[i - 1] in " \t\r\n;&|()"):
            # shlex's comment reader consumes the newline. Mask comments here
            # instead so a real command on the next line starts a new part.
            end = command.find("\n", i)
            end = len(command) if end < 0 else end
            command = command[:i] + " " * (end - i) + command[end:]
            i = end
            continue
        elif c == "\\" and quote != "'":
            escaped = True
        elif c == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif c == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif command[i:i + 2] == "$(" and quote != "'":
            stack.append((i, quote))
            quote = None
            i += 1
        elif c == ")" and stack and quote is None:
            start, outer_quote = stack.pop()
            if not stack:
                substitutions.extend(bash_verbs(command[start + 2:i]))
                spans.append((start, i + 1))
            quote = outer_quote
        i += 1
    for start, end in reversed(spans):
        command = command[:start] + " substitution " + command[end:]
    import shlex
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return substitutions
    parts, part = [], []
    for token in tokens + [";"]:
        if token and all(c in ";&|()\n" for c in token):
            parts.append(part)
            part = []
        else:
            part.append(token)
    for words in parts:
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if len(words) >= 2 and words[0].rsplit("/", 1)[-1] == "kaiusap":
            # The CLI permits --json anywhere.
            rest = [word for word in words[1:] if word != "--json"]
            if rest:
                substitutions.append(rest[0])
    return substitutions
