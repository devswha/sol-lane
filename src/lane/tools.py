"""The tool-call bridge: OpenAI tool specs down, tool calls back up.

Sol Pro has no function-calling API. It has a text box. So the bridge is a
protocol carried in prose: the declared tools are rendered into the prompt, and
the reply is parsed back into OpenAI `tool_calls`.

The whole design rests on one rule — **a tool call that cannot be parsed is never
executed**. gjc runs shell commands and edits files with what comes back; a
generous parser that guesses at a half-written call is worse than no bridge. So
every block that announces itself as a call and then fails to be one is an error,
and only text with no call blocks at all is treated as a plain answer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

# The model is asked for exactly this: a fenced block per call, JSON inside.
# A fence is unambiguous in a way that "the model wrote some JSON somewhere" is
# not, and it survives being surrounded by prose.
FENCE = "tool_call"
BLOCK_RE = re.compile(rf"^[ \t]*```{FENCE}[ \t]*\n(.*?)\n[ \t]*```[ \t]*$",
                      re.DOTALL | re.MULTILINE)
# Anything that looks like an opening fence but never closes: a truncated reply.
OPENING_RE = re.compile(rf"```{FENCE}\b")

PROTOCOL = f"""\
[TOOLS]
You can call the tools below. To call one, reply with a fenced block exactly like
this, and nothing else inside the fence:

```{FENCE}
{{"name": "<tool name>", "arguments": {{<JSON object of arguments>}}}}
```

Rules:
- One block per call. Repeat the block to request several calls.
- `arguments` must be a JSON object, even when empty: `{{}}`.
- Use only the tool names listed below, spelled exactly.
- Do not wrap the block in extra prose explaining what you are about to do.
- When no tool is needed, answer normally with no fenced block.

Available tools:
"""


class ToolBridgeError(Exception):
    """A reply announced a tool call that cannot be executed as written."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, which is what the OpenAI schema carries

    def as_openai(self) -> dict:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


def declared_names(tools: list[dict]) -> list[str]:
    """Tool names from an OpenAI `tools` array, in declaration order."""
    names = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return names


def render_tools(tools: list[dict]) -> str:
    """The prompt block that teaches the protocol and lists the tools."""
    lines = [PROTOCOL]
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        name = function["name"]
        description = str(function.get("description") or "").strip()
        parameters = function.get("parameters")
        schema = json.dumps(parameters, ensure_ascii=False, sort_keys=True) if parameters else "{}"
        lines.append(f"- {name}: {description}" if description else f"- {name}")
        lines.append(f"  parameters: {schema}")
    return "\n".join(lines)


def call_id(name: str, arguments: str, index: int) -> str:
    """Deterministic id: the same reply parsed twice yields the same call ids."""
    digest = hashlib.sha256(f"{index}\0{name}\0{arguments}".encode()).hexdigest()
    return f"call_{digest[:24]}"


def parse_reply(text: str, *, allowed: list[str]) -> tuple[str, list[ToolCall]]:
    """Split a reply into its visible content and its tool calls.

    Returns ``(content, calls)``. No blocks means no calls — an ordinary answer.
    A block that is not a valid call for a declared tool raises instead of being
    dropped, because dropping it would present a model that tried to act as one
    that chose not to.
    """
    blocks = list(BLOCK_RE.finditer(text))
    remainder = BLOCK_RE.sub("", text)
    # Checked on what is left after the complete blocks, not on the whole reply:
    # one good call followed by a truncated one used to return the first and let
    # the second through as prose. A dropped call reads as a model that chose not
    # to act, which is the failure this module exists to refuse.
    if OPENING_RE.search(remainder):
        raise ToolBridgeError(
            "reply opens a tool_call block that never closes; refusing to "
            "guess at a truncated call"
        )
    if not blocks:
        return text.strip(), []

    calls = []
    for index, block in enumerate(blocks):
        calls.append(_parse_block(block.group(1), index=index, allowed=allowed))
    return remainder.strip(), calls


def _parse_block(payload: str, *, index: int, allowed: list[str]) -> ToolCall:
    try:
        parsed = json.loads(payload)
    except ValueError as error:
        raise ToolBridgeError(f"tool_call block {index + 1} is not JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ToolBridgeError(f"tool_call block {index + 1} is not a JSON object")

    name = parsed.get("name")
    if not isinstance(name, str) or not name:
        raise ToolBridgeError(f"tool_call block {index + 1} has no tool name")
    if name not in allowed:
        raise ToolBridgeError(
            f"reply calls {name!r}, which was not offered "
            f"(offered: {', '.join(allowed) or 'nothing'})"
        )

    arguments = parsed.get("arguments", {})
    if isinstance(arguments, str):
        # A model that stringifies its own arguments is common enough to accept,
        # but only when the string really is a JSON object.
        try:
            arguments = json.loads(arguments)
        except ValueError as error:
            raise ToolBridgeError(
                f"tool_call {name!r} has arguments that are neither an object nor JSON: {error}"
            ) from error
    if not isinstance(arguments, dict):
        raise ToolBridgeError(f"tool_call {name!r} has arguments that are not an object")

    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return ToolCall(id=call_id(name, serialized, index), name=name, arguments=serialized)


def render_tool_result(message: dict) -> str:
    """A `role: tool` message as prose the next prompt can carry.

    The transcript is re-rendered on every request, so a tool result has to be
    readable text: the model never sees the OpenAI envelope.
    """
    name = message.get("name") or "tool"
    call = message.get("tool_call_id") or "?"
    content = message.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False) if content is not None else ""
    return f"result of {name} (call {call}):\n{content.strip()}"


def render_assistant_calls(message: dict) -> str:
    """An assistant turn that made tool calls, as prose for the transcript."""
    rendered = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        name = function.get("name") or "?"
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        rendered.append(f"called {name} with {arguments}")
    return "\n".join(rendered)
