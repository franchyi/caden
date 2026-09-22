import re

ACTION_RE = re.compile(r"```bash\s*\n(.*?)```", re.DOTALL)
# A heredoc opener anchored at END OF LINE: << or <<-, a >=2-char tag in matched (or no) quotes,
# then only trailing redirections/pipes before the newline. >=2 chars cuts false hits on shifts
# (`1 << n`); (?<!<)/(?!<) excludes <<< herestrings; the end-of-line anchor is what stops us from
# flagging `<< WORD` written inside a string or a # comment, where prose follows the tag on the
# same line (a real opener has the heredoc body starting on the NEXT line).
_HEREDOC_RE = re.compile(
    r"(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_]\w+)\1[ \t]*(?:[<>|&\d][^\n]*)?$", re.M)


class FormatError(Exception):
    """Model response contained no bash command block (or a malformed one)."""


def parse_action(text: str) -> str:
    """Return the bash command(s) from the response. The model may emit one or more ```bash blocks;
    they are concatenated and run in order in a single shell. Raise FormatError if none found, or
    if a heredoc has no terminator — the usual cause is a literal ``` inside the block ending the
    fence early, and executing the truncated script would silently swallow every following command
    as heredoc content."""
    blocks = [m.strip() for m in ACTION_RE.findall(text)]
    if not blocks:
        raise FormatError("No ```bash code block found in the response.")
    command = "\n".join(blocks)
    for m in _HEREDOC_RE.finditer(command):
        tag = m.group(2)
        if not re.search(rf"^\s*{tag}\s*$", command[m.end():], re.M):
            raise FormatError(
                f"heredoc <<{tag} has no terminator line. If the file you are writing "
                "contains ``` the fence ended your bash block early — write that file with "
                "python instead of a heredoc."
            )
    return command
