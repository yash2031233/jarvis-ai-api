import pytest

from jarvis.brain.repair import extract_text_tool_calls, loads_lenient, match_tool_name, normalize_args

KNOWN = ["open_app", "set_volume", "web_search"]


@pytest.mark.parametrize("raw,expected", [
    ('{"name": "spotify"}', {"name": "spotify"}),
    ('```json\n{"name": "spotify"}\n```', {"name": "spotify"}),
    ('{"name": "spotify",}', {"name": "spotify"}),
    ("{'name': 'spotify'}", {"name": "spotify"}),
    ('{"level": 40, "mute": false', {"level": 40, "mute": False}),  # truncated
    ('Sure! {"level": 40}', {"level": 40}),
    ('{"flag": true, "x": null}', {"flag": True, "x": None}),
])
def test_loads_lenient(raw, expected):
    assert loads_lenient(raw) == expected


def test_double_encoded_args():
    assert normalize_args('"{\\"name\\": \\"chrome\\"}"') == {"name": "chrome"}


@pytest.mark.parametrize("name,expected", [
    ("open_app", "open_app"), ("functions.open_app", "open_app"), ("OpenApp", "open_app"),
    ("open_ap", "open_app"), ("set-volume", "set_volume"), ("delete_everything", None),
])
def test_match_tool_name(name, expected):
    assert match_tool_name(name, KNOWN) == expected


def test_extract_tagged_calls():
    text = 'On it.\n<tool_call>{"name": "open_app", "arguments": {"name": "spotify"}}</tool_call>'
    calls, rest = extract_text_tool_calls(text, KNOWN)
    assert [(c.name, c.arguments) for c in calls] == [("open_app", {"name": "spotify"})]
    assert rest == "On it."


def test_extract_fenced_and_bare():
    calls, _ = extract_text_tool_calls('```json\n{"name": "set_volume", "arguments": {"level": 30}}\n```', KNOWN)
    assert calls[0].arguments == {"level": 30}
    calls, _ = extract_text_tool_calls('{"name": "web_search", "parameters": {"query": "x"}}', KNOWN)
    assert calls[0].name == "web_search" and calls[0].arguments == {"query": "x"}


def test_plain_text_is_not_a_tool_call():
    calls, rest = extract_text_tool_calls("The capital of France is Paris.", KNOWN)
    assert calls == [] and rest == "The capital of France is Paris."
