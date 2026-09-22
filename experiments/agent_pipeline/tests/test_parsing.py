import pytest

from agent_pipeline.parsing import FormatError, parse_action


def test_extracts_single_command():
    text = "THOUGHT: list files\n\n```bash\nls -la\n```"
    assert parse_action(text) == "ls -la"


def test_strips_whitespace_and_keeps_multiline():
    text = "do this\n```bash\ncd src && \\\npytest -q\n```\n"
    assert parse_action(text) == "cd src && \\\npytest -q"


def test_zero_blocks_raises():
    with pytest.raises(FormatError):
        parse_action("I think we are done.")


def test_multiple_blocks_joined():
    # The model may emit several bash blocks (e.g. write a file, then run tests); they run in order.
    assert parse_action("```bash\nls\n```\nand\n```bash\npwd\n```") == "ls\npwd"


def test_heredoc_with_terminator_ok():
    cmd = parse_action("```bash\npython3 - << 'EOF'\nprint(1)\nEOF\n```")
    assert cmd.endswith("EOF")


def test_unterminated_heredoc_raises():
    # the classic cause: a literal ``` inside the heredoc body ended the fence early
    with pytest.raises(FormatError):
        parse_action("```bash\ncat > x.md << 'MD'\nsome text\n```\nMD\n")


def test_herestrings_and_shifts_not_flagged():
    parse_action('```bash\ngrep foo <<< "$x"\n```')
    parse_action("```bash\npython3 -c 'print(1 << 2)'\n```")
    parse_action("```bash\necho $((v << n))\n```")  # single-letter tag candidates ignored


def test_heredoc_mentioned_in_string_or_comment_not_flagged():
    # tightened guard: a real opener ends the line; prose after the tag means it is not one
    parse_action('```bash\necho "to start a heredoc type << END then text"\n```')
    parse_action("```bash\nls  # uses <<EOF style heredocs\n```")


def test_heredoc_with_trailing_redirection_still_detected():
    cmd = parse_action("```bash\ncat <<EOF >out.txt\nhi\nEOF\n```")
    assert "EOF" in cmd
