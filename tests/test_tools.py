from __future__ import annotations

import json

import pytest

from lane import tools as tools_module

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}
SHELL = {"type": "function", "function": {"name": "run_shell", "parameters": {}}}
ALLOWED = ["get_weather", "run_shell"]


def block(payload: str) -> str:
    return f"```{tools_module.FENCE}\n{payload}\n```"


def test_declared_names_keeps_declaration_order():
    assert tools_module.declared_names([WEATHER, SHELL]) == ["get_weather", "run_shell"]


def test_declared_names_ignores_malformed_entries():
    assert tools_module.declared_names([{"type": "function"}, "nonsense", WEATHER]) == ["get_weather"]


def test_render_tools_carries_the_protocol_the_schema_and_the_names():
    rendered = tools_module.render_tools([WEATHER])

    assert tools_module.FENCE in rendered
    assert "get_weather" in rendered
    assert "Current weather for a city" in rendered
    assert '"city"' in rendered, "the model cannot fill parameters it never saw"


def test_a_reply_with_no_block_is_an_ordinary_answer():
    content, calls = tools_module.parse_reply("Seoul is cold today.", allowed=ALLOWED)

    assert content == "Seoul is cold today."
    assert calls == []


def test_a_single_call_is_parsed_into_the_openai_shape():
    reply = block('{"name": "get_weather", "arguments": {"city": "Seoul"}}')

    content, calls = tools_module.parse_reply(reply, allowed=ALLOWED)

    assert content == ""
    assert len(calls) == 1
    payload = calls[0].as_openai()
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "get_weather"
    assert json.loads(payload["function"]["arguments"]) == {"city": "Seoul"}
    assert payload["id"].startswith("call_")


def test_prose_around_a_call_is_kept_as_content():
    reply = ("I need the forecast first.\n\n"
             + block('{"name": "get_weather", "arguments": {"city": "Seoul"}}')
             + "\n\nThen I will answer.")

    content, calls = tools_module.parse_reply(reply, allowed=ALLOWED)

    assert "I need the forecast first." in content
    assert "Then I will answer." in content
    assert tools_module.FENCE not in content
    assert len(calls) == 1


def test_repeated_blocks_become_parallel_calls_with_distinct_ids():
    reply = (block('{"name": "get_weather", "arguments": {"city": "Seoul"}}') + "\n"
             + block('{"name": "get_weather", "arguments": {"city": "Busan"}}'))

    _, calls = tools_module.parse_reply(reply, allowed=ALLOWED)

    assert [json.loads(call.arguments)["city"] for call in calls] == ["Seoul", "Busan"]
    assert calls[0].id != calls[1].id


def test_the_same_reply_parses_to_the_same_ids():
    reply = block('{"name": "run_shell", "arguments": {}}')

    first = tools_module.parse_reply(reply, allowed=ALLOWED)[1]
    second = tools_module.parse_reply(reply, allowed=ALLOWED)[1]

    assert [call.id for call in first] == [call.id for call in second]


def test_stringified_arguments_are_accepted_when_they_are_really_json():
    reply = block('{"name": "get_weather", "arguments": "{\\"city\\": \\"Seoul\\"}"}')

    _, calls = tools_module.parse_reply(reply, allowed=ALLOWED)

    assert json.loads(calls[0].arguments) == {"city": "Seoul"}


def test_a_truncated_block_is_refused_instead_of_ignored():
    reply = f'Let me check.\n```{tools_module.FENCE}\n{{"name": "run_shell"'

    with pytest.raises(tools_module.ToolBridgeError, match="never closes"):
        tools_module.parse_reply(reply, allowed=ALLOWED)


def test_a_block_that_is_not_json_is_refused():
    with pytest.raises(tools_module.ToolBridgeError, match="not JSON"):
        tools_module.parse_reply(block("run_shell('rm -rf /')"), allowed=ALLOWED)


def test_a_call_to_a_tool_that_was_not_offered_is_refused():
    reply = block('{"name": "delete_everything", "arguments": {}}')

    with pytest.raises(tools_module.ToolBridgeError, match="was not offered"):
        tools_module.parse_reply(reply, allowed=ALLOWED)


def test_arguments_that_are_not_an_object_are_refused():
    reply = block('{"name": "run_shell", "arguments": ["rm", "-rf", "/"]}')

    with pytest.raises(tools_module.ToolBridgeError, match="not an object"):
        tools_module.parse_reply(reply, allowed=ALLOWED)


def test_a_missing_name_is_refused():
    with pytest.raises(tools_module.ToolBridgeError, match="no tool name"):
        tools_module.parse_reply(block('{"arguments": {}}'), allowed=ALLOWED)


def test_missing_arguments_default_to_an_empty_object():
    _, calls = tools_module.parse_reply(block('{"name": "run_shell"}'), allowed=ALLOWED)

    assert calls[0].arguments == "{}"


def test_a_tool_result_renders_as_readable_prose():
    rendered = tools_module.render_tool_result(
        {"role": "tool", "name": "get_weather", "tool_call_id": "call_1", "content": "-3C"})

    assert "get_weather" in rendered
    assert "call_1" in rendered
    assert "-3C" in rendered


def test_a_non_string_tool_result_is_serialized_not_dropped():
    rendered = tools_module.render_tool_result({"name": "t", "content": {"temp": -3}})

    assert '"temp": -3' in rendered


def test_an_assistant_turn_with_calls_renders_for_the_transcript():
    rendered = tools_module.render_assistant_calls({
        "tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city": "Seoul"}'}}]})

    assert "called get_weather" in rendered
    assert "Seoul" in rendered


def test_a_truncated_call_after_a_good_one_is_refused_not_dropped():
    """Found while auditing this module 2026-08-13: the opening-fence check only
    ran when no block matched, so a stream cut mid-second-call returned the first
    call and leaked the second into content as prose."""
    reply = (block('{"name": "get_weather", "arguments": {"city": "Seoul"}}')
             + "\n\n다음으로 셸을 봅니다.\n\n"
             + f'```{tools_module.FENCE}\n{{"name": "run_shell", "arguments": {{"cmd": "rm')

    with pytest.raises(tools_module.ToolBridgeError, match="never closes"):
        tools_module.parse_reply(reply, allowed=ALLOWED)


def test_content_never_carries_a_leftover_fence():
    reply = ("먼저 날씨입니다.\n"
             + block('{"name": "get_weather", "arguments": {"city": "Seoul"}}')
             + "\n그다음 답하겠습니다.")

    content, calls = tools_module.parse_reply(reply, allowed=ALLOWED)

    assert tools_module.FENCE not in content
    assert "```" not in content
    assert len(calls) == 1
