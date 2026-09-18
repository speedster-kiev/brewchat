"""One chat turn: system prompt + session history + the three tools, via the SDK tool runner.

History handling: the SDK's ``BetaToolRunner`` keeps its own copy of the
conversation and does not expose it, so this module mirrors it as it iterates
(the pattern the SDK docs recommend): each assistant message is stored whole
(thinking, text, tool_use blocks) and each tool-result message is taken from
``runner.generate_tool_call_response()``, which is cached, so tools still run
exactly once. Everything is stored as plain API-param dicts, valid to resend.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anthropic

from brewchat.agent.models import OrderList
from brewchat.agent.prompt import render_system_prompt
from brewchat.agent.session import Session, current_session

if TYPE_CHECKING:
    from brewchat.agent.tools import ToolContext

# Server-side refusal fallback: on a policy decline the API re-runs the request
# on a fallback model within the same call. Only sent to models that support it.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-mythos-5")
# Models that accept thinking={"type": "adaptive"}. Others (Haiku 4.5, 4.5-era
# and older) run without thinking: the cheapest setting, and no budget to tune.
ADAPTIVE_THINKING_MODELS = (
    "claude-opus-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
)
MAX_TOOL_ITERATIONS = 40

EMPTY_REPLY = (
    "Sorry, I couldn't finish that reply. Could you send the message again, "
    "or paste the recipe in a smaller piece?"
)

# ---------------------------------------------------------------------------
# Pasted-recipe detection and wrapping
# ---------------------------------------------------------------------------

_UNITS = (
    r"kg|kgs|g|gr|grams?|mg|lbs?|pounds?|oz|ounces?|ml|cl|dl|l|ltr|liters?|litres?|"
    r"gal|gallons?|qt|quarts?|pkgs?|packs?|packets?|sachets?|vials?|smack\s*packs?|"
    r"tsp|tbsp|teaspoons?|tablespoons?|cups?|tablets?|units?|x|%"
)
# "5 kg", "5.5kg", "1,5 kg", "1/2 tsp", "1 x", "2 packs"
_QTY_UNIT_RE = re.compile(
    rf"(?<![\w.])\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?\s*(?:{_UNITS})(?![A-Za-z])", re.IGNORECASE
)
_RECIPE_TAG_RE = re.compile(r"<(\s*/?\s*recipe)", re.IGNORECASE)
MIN_QTY_LINES = 3


def _is_qty_line(line: str) -> bool:
    return bool(_QTY_UNIT_RE.search(line))


def looks_like_recipe(text: str) -> bool:
    """Heuristic: multi-line text with at least three quantity+unit lines."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < MIN_QTY_LINES:
        return False
    return sum(1 for ln in lines if _is_qty_line(ln)) >= MIN_QTY_LINES


def _paragraphs(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) line ranges of blank-line-separated paragraphs."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, ln in enumerate(lines):
        if ln.strip():
            if start is None:
                start = i
        elif start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(lines)))
    return out


def _is_conversational(block: list[str], *, lead: bool) -> bool:
    """A short, quantity-free chunk that reads like a message to the assistant."""
    if not block or len(block) > 3 or any(_is_qty_line(ln) for ln in block):
        return False
    last = block[-1].rstrip()
    enders = (":", "?", "!", ".") if lead else ("?", "!")
    return last.endswith(enders)


def split_recipe_message(text: str) -> tuple[str, str, str]:
    """Split a message into (lead_in, recipe_body, tail).

    The lead-in is a short conversational opener ("Can you price this up?")
    that should stay outside the data block; likewise a trailing question.
    """
    lines = text.strip("\n").splitlines()
    paras = _paragraphs(lines)
    lead_end = 0
    tail_start = len(lines)
    if len(paras) >= 2:
        s, e = paras[0]
        if _is_conversational(lines[s:e], lead=True):
            lead_end = e
        s, e = paras[-1]
        if (s, e) != paras[0] and _is_conversational(lines[s:e], lead=False):
            tail_start = s
    if lead_end == 0 and lines:
        # Single opener line glued to the recipe: "Price this up:\n5 kg Pale..."
        first = lines[0].rstrip()
        if first.endswith((":", "?")) and not _is_qty_line(first) and len(first.split()) >= 3:
            lead_end = 1
    lead = "\n".join(lines[:lead_end]).strip()
    body = "\n".join(lines[lead_end:tail_start]).strip("\n")
    tail = "\n".join(lines[tail_start:]).strip()
    return lead, body, tail


def neutralise_recipe_tags(text: str) -> str:
    """Stop pasted text from closing (or opening) the delimiting block."""
    return _RECIPE_TAG_RE.sub(r"&lt;\1", text)


def wrap_user_message(text: str) -> str:
    """Wrap likely-pasted recipe text in a ``<recipe>`` data block; leave chat as-is."""
    if not looks_like_recipe(text):
        return text
    lead, body, tail = split_recipe_message(text)
    parts = []
    if lead:
        parts.append(lead)
    parts.append(f"<recipe>\n{neutralise_recipe_tags(body)}\n</recipe>")
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


USAGE_FIELDS = (
    "input_tokens",  # uncached input only
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",  # includes thinking tokens
)


def add_usage(total: dict[str, int], usage: Any) -> None:
    """Accumulate one API response's ``usage`` into ``total`` (plus an ``api_calls`` count)."""
    total["api_calls"] = total.get("api_calls", 0) + 1
    for name in USAGE_FIELDS:
        total[name] = total.get(name, 0) + (getattr(usage, name, None) or 0)


@dataclass
class TurnResult:
    reply: str
    order_list: OrderList | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cache_timestamp: str = ""
    # Summed over every API call in the turn; see USAGE_FIELDS.
    usage: dict[str, int] = field(default_factory=dict)
    # Set when the turn was cut short by ``stop_after`` (the tool that triggered it).
    stopped_after: str | None = None


def _block_to_param(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    # SDK models: API field names, only what the API sent; ParsedBetaTextBlock's
    # parsed_output is excluded from dumps. Thinking signatures are preserved.
    return block.to_dict(mode="json", exclude_none=True)


def _message_to_param(message: Any) -> dict[str, Any]:
    return {"role": message.role, "content": [_block_to_param(b) for b in message.content]}


def _tool_result_to_param(msg: dict[str, Any]) -> dict[str, Any]:
    content = msg.get("content")
    if isinstance(content, list):
        content = [_block_to_param(b) for b in content]
    return {"role": msg["role"], "content": content}


class Runner:
    def __init__(
        self,
        ctx: ToolContext,
        client: anthropic.Anthropic | None = None,
        tools: list | None = None,
        model: str | None = None,
        max_tokens: int = 16000,
        *,
        refusal_fallbacks: bool = True,
    ) -> None:
        self.ctx = ctx
        self.client = client if client is not None else anthropic.Anthropic()
        if tools is None:
            from brewchat.agent.tools import make_tools

            tools = make_tools(ctx)
        self.tools = tools
        # None -> the configured model (settings.model, from $BREWCHAT_MODEL).
        self.model = model or ctx.settings.model
        self.max_tokens = max_tokens
        self.refusal_fallbacks = refusal_fallbacks

    def _request_kwargs(self, system_prompt: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # Tools render before system, so this breakpoint caches tools + system.
            "system": [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ],
            # Top-level auto-caching: the growing history is reused across the
            # iterations of the tool loop and across turns.
            "cache_control": {"type": "ephemeral"},
            "tool_choice": {"type": "auto"},
            "tools": self.tools,
            "messages": messages,
            "max_iterations": MAX_TOOL_ITERATIONS,
        }
        if self.model.startswith(ADAPTIVE_THINKING_MODELS):
            kwargs["thinking"] = {"type": "adaptive"}
        if self.refusal_fallbacks and self.model.startswith(FALLBACK_MODELS):
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def run_turn(
        self, session: Session, message: str, *, stop_after: Collection[str] = ()
    ) -> TurnResult:
        """Run one user turn through the tool loop.

        ``stop_after``: tool names that end the turn once they have run, without
        another model call (the parse eval stops after ``submit_parsed_recipe``).
        """
        token = current_session.set(session)
        try:
            session.touch()
            self.ctx.session_log.record_message(session.session_id)
            raw_ts = self.ctx.index.cache_timestamp
            cache_ts = raw_ts if isinstance(raw_ts, str) else raw_ts.isoformat()
            system_prompt = render_system_prompt(self.ctx.settings, raw_ts)
            order_before = session.order_list

            user_msg = {"role": "user", "content": wrap_user_message(message)}
            new_msgs: list[dict[str, Any]] = [user_msg]
            runner = self.client.beta.messages.tool_runner(
                **self._request_kwargs(system_prompt, [*session.messages, user_msg])
            )

            tool_calls: list[dict[str, Any]] = []
            usage: dict[str, int] = {}
            stopped_after = None
            final = None
            for msg in runner:
                final = msg
                add_usage(usage, msg.usage)
                new_msgs.append(_message_to_param(msg))
                uses = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
                for b in uses:
                    tool_calls.append({"name": b.name, "input": b.input})
                if msg.stop_reason == "tool_use":
                    response = runner.generate_tool_call_response()  # cached; runs once
                    if response is not None:
                        new_msgs.append(_tool_result_to_param(response))
                    stopped_after = next((b.name for b in uses if b.name in stop_after), None)
                    if stopped_after:
                        break

            if final is not None:
                self._close_dangling_tool_uses(final, new_msgs)
            # Commit only after the whole turn succeeded, so a failed API call
            # never leaves a half-turn in history.
            session.messages.extend(new_msgs)
            session.touch()

            reply = ""
            if final is not None:
                reply = "".join(
                    b.text for b in final.content if getattr(b, "type", None) == "text"
                ).strip()
            if not stopped_after:
                reply = reply or EMPTY_REPLY
            order = session.order_list if session.order_list is not order_before else None
            return TurnResult(
                reply=reply,
                order_list=order,
                tool_calls=tool_calls,
                cache_timestamp=cache_ts,
                usage=usage,
                stopped_after=stopped_after,
            )
        finally:
            current_session.reset(token)

    @staticmethod
    def _close_dangling_tool_uses(final: Any, new_msgs: list[dict[str, Any]]) -> None:
        """If the turn ended (max_tokens, refusal, iteration cap) with unanswered
        tool_use blocks, answer them with errors so the history stays resendable."""
        if new_msgs and new_msgs[-1]["role"] == "user":
            return  # tool results already follow the last assistant message
        pending = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
        if not pending:
            return
        new_msgs.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": "Not executed: the turn ended before this tool ran.",
                        "is_error": True,
                    }
                    for b in pending
                ],
            }
        )
