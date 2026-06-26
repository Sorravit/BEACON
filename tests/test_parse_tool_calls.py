"""Tests for AIAgent._parse_tool_calls separator tolerance and core formats.

The parser must tolerate `_` vs `-` separator mismatches between an opening and
closing tag for the same word (e.g. `<tool_use>` ... `</tool-use>`), since models
occasionally emit inconsistent separators.
"""
import types

from main import AIAgent

# _parse_tool_calls only relies on module-level `re`, `json` and `logger`, so we can
# invoke it against a bare dummy `self` without building a full AIAgent instance.
_parse = AIAgent._parse_tool_calls.__get__(types.SimpleNamespace())


def test_well_formed_underscore():
    text = (
        "<tool_use>\n"
        "<tool_name>read_file</tool_name>\n"
        "<parameters>{\"path\": \"a.txt\"}</parameters>\n"
        "</tool_use>"
    )
    calls = _parse(text)
    assert calls == [{"tool_name": "read_file", "parameters": {"path": "a.txt"}}]


def test_mismatched_close_separator_on_tool_use():
    # opens <tool_use> but closes </tool-use>
    text = (
        "<tool_use>\n"
        "<tool_name>read_file</tool_name>\n"
        "<parameters>{\"path\": \"a.txt\"}</parameters>\n"
        "</tool-use>"
    )
    calls = _parse(text)
    assert calls == [{"tool_name": "read_file", "parameters": {"path": "a.txt"}}]


def test_hyphen_open_and_close_tool_use():
    text = (
        "<tool-use>\n"
        "<tool-name>read_file</tool-name>\n"
        "<parameters>{\"path\": \"a.txt\"}</parameters>\n"
        "</tool-use>"
    )
    calls = _parse(text)
    assert calls == [{"tool_name": "read_file", "parameters": {"path": "a.txt"}}]


def test_mismatched_tool_name_separator():
    text = (
        "<tool_use>\n"
        "<tool_name>read_file</tool-name>\n"
        "<parameters>{\"path\": \"a.txt\"}</parameters>\n"
        "</tool_use>"
    )
    calls = _parse(text)
    assert calls == [{"tool_name": "read_file", "parameters": {"path": "a.txt"}}]


def test_invoke_format_still_parses():
    text = (
        "<invoke name=\"search\">\n"
        "<parameter name=\"query\">hello</parameter>\n"
        "</invoke>"
    )
    calls = _parse(text)
    assert {"tool_name": "search", "parameters": {"query": "hello"}} in calls
