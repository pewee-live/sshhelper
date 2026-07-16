"""Context compaction logic -- extracted from web_server.py for clarity.

Token-budget-driven context management: keeps the first HumanMessage (original
goal) and a recent window, truncates oversized ToolMessage output, and only
summarizes a focused delta when absolutely necessary.
"""
import os
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage


# Model context window, configurable via the MODEL_CONTEXT_WINDOW env var.
CONTEXT_WINDOW_TOKENS = int(os.getenv("MODEL_CONTEXT_WINDOW", "64000"))
CONTEXT_BUDGET_FRACTION = float(os.getenv("MODEL_CONTEXT_BUDGET", "0.8"))
CONTEXT_TARGET_HEADROOM_TOKENS = 8000
RECENT_WINDOW_MESSAGES = 12
TOOL_BULK_TRUNCATE_CHARS = 600


def last_input_tokens(messages):
    for m in reversed(messages):
        usage = getattr(m, "usage_metadata", None)
        if isinstance(usage, dict) and usage.get("input_tokens"):
            return int(usage["input_tokens"])
        meta = getattr(m, "response_metadata", None) or {}
        token_usage = meta.get("token_usage") if isinstance(meta, dict) else None
        if isinstance(token_usage, dict) and token_usage.get("prompt_tokens"):
            return int(token_usage["prompt_tokens"])
    return None


def approx_tokens(messages):
    total = 0
    for m in messages:
        total += len(str(getattr(m, "content", "") or ""))
        for tc in getattr(m, "tool_calls", []) or []:
            total += len(str(tc.get("args", "")))
    return total // 4


def truncate_tool_content(msg):
    content = str(getattr(msg, "content", "") or "")
    if len(content) <= TOOL_BULK_TRUNCATE_CHARS:
        return msg
    head = content[: TOOL_BULK_TRUNCATE_CHARS // 2]
    tail = content[-TOOL_BULK_TRUNCATE_CHARS // 2 :]
    return ToolMessage(
        content=f"{head}\n... [truncated {len(content) - TOOL_BULK_TRUNCATE_CHARS} chars] ...\n{tail}",
        tool_call_id=getattr(msg, "tool_call_id", ""),
        name=getattr(msg, "name", None),
    )


def find_safe_split(messages, keep_recent):
    split_idx = len(messages) - keep_recent
    while split_idx > 0:
        msg_at_split = messages[split_idx]
        if isinstance(msg_at_split, ToolMessage):
            split_idx -= 1
            continue
        prev = messages[split_idx - 1]
        if isinstance(prev, AIMessage) and getattr(prev, "tool_calls", None):
            split_idx -= 1
            continue
        break
    return max(split_idx, 0)


async def compact_messages(messages, get_llm_fn=None):
    budget = int(CONTEXT_WINDOW_TOKENS * CONTEXT_BUDGET_FRACTION)
    last_tokens = last_input_tokens(messages)
    est_tokens = last_tokens if last_tokens else approx_tokens(messages)
    if est_tokens < budget:
        return messages

    first_human_idx = next(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), None
    )
    head = []
    body_start = 0
    if first_human_idx is not None:
        head = messages[: first_human_idx + 1]
        body_start = first_human_idx + 1

    tail = messages[-RECENT_WINDOW_MESSAGES:]
    split_idx = find_safe_split(messages, RECENT_WINDOW_MESSAGES)
    split_idx = max(split_idx, body_start)

    middle = messages[body_start:split_idx]
    if not middle:
        return messages

    trimmed_middle = [
        truncate_tool_content(m) if isinstance(m, ToolMessage) else m
        for m in middle
    ]

    trimmed_tokens = approx_tokens(head + trimmed_middle + tail)
    target = budget - CONTEXT_TARGET_HEADROOM_TOKENS
    if trimmed_tokens <= target:
        return head + trimmed_middle + tail

    delta_for_summary = [
        m for m in trimmed_middle
        if not (isinstance(m, SystemMessage) and "Conversation Summary" in (getattr(m, "content", "") or ""))
    ]
    if not delta_for_summary:
        return head + tail

    text = "\n".join(
        f"[{type(m).__name__}] {summarizable_text(m)}" for m in delta_for_summary
    )
    if get_llm_fn is None:
        from llm import get_llm as get_llm_fn
    llm = get_llm_fn()
    prompt = [
        SystemMessage(content=(
            "Summarize the following earlier debugging steps concisely. Keep: "
            "every command executed and its exit status, key findings, and the current "
            "state/conclusion. Drop verbose command output already reflected in "
            "later conclusions."
        )),
        HumanMessage(content=text[:20000]),
    ]
    try:
        summary_response = await llm.ainvoke(prompt)
        summary_text = summary_response.content
    except Exception as e:
        print(f"Context summarization failed, keeping trimmed history: {e}")
        return head + trimmed_middle + tail

    summary_msg = SystemMessage(content=f"Earlier Conversation Summary:\n{summary_text}")
    return head + [summary_msg] + tail


def summarizable_text(msg):
    if isinstance(msg, ToolMessage):
        content = str(getattr(msg, "content", "") or "")
        if len(content) > 240:
            content = content[:120] + " ... " + content[-120:]
        return content
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        cmds = "; ".join(
            str(tc.get("args", {}).get("command", tc.get("args", ""))) for tc in tool_calls
        )
        body = str(getattr(msg, "content", "") or "").strip()
        return f"(tool calls: {cmds}){(' ' + body) if body else ''}"
    return str(getattr(msg, "content", "") or "")


async def summarize_context(messages, max_turns=20):
    """Compatibility wrapper -- delegates to compact_messages."""
    return await compact_messages(messages)