"""Automatic case generation from debugging sessions.

Reads session histories, uses the LLM to extract structured troubleshooting
cases (symptom -> diagnosis -> root cause -> fix -> verification), classifies
them by domain, and writes each case as a standalone Markdown file under
data/cases/<domain>/.

Each case includes a YAML frontmatter block with tags and search_queries
(user-facing query variations) for knowledge-base retrieval, plus full
sections: symptom, error message, prerequisites, diagnosis, root cause,
solution, verification, rollback, risk, and Q&A.

Designed to be called automatically after an agent run completes (fire-and-forget
background task) or manually via the API.
"""
import os
import re
import json
import asyncio
from datetime import datetime
from typing import Optional, List

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_core.messages import messages_from_dict

# Domains for case classification
DOMAINS = [
    "network",
    "software-install",
    "system-diagnosis",
    "storage",
    "security",
    "hardware-driver",
    "performance",
    "config-management",
    "other",
]

CASES_DIR = os.path.join("data", "cases")
MIN_MESSAGES_FOR_CASE = 6


def _slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.strip().lower())
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:60] or "untitled"


def _render_session_for_llm(messages, session_data: dict) -> str:
    lines = []
    conn = session_data.get("conn_type", "unknown")
    host = (session_data.get("connection_params") or {}).get("host", "")
    lines.append(f"[Device: {conn}:{host}]")
    lines.append("")
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        elif isinstance(m, HumanMessage):
            content = str(m.content or "").strip()
            if content:
                lines.append(f"USER: {content[:500]}")
        elif isinstance(m, AIMessage):
            tool_calls = getattr(m, "tool_calls", None) or []
            if tool_calls:
                cmds = []
                for tc in tool_calls:
                    args = tc.get("args", {})
                    cmd = args.get("command", str(args)) if isinstance(args, dict) else str(args)
                    cmds.append(cmd[:150])
                lines.append(f"AGENT_COMMAND: {json.dumps(cmds, ensure_ascii=False)[:400]}")
            content = str(getattr(m, "content", "") or "").strip()
            if content:
                lines.append(f"AGENT: {content[:500]}")
        elif isinstance(m, ToolMessage):
            content = str(getattr(m, "content", "") or "").strip()
            if len(content) > 600:
                content = content[:300] + f"\n... [truncated {len(content)-600} chars] ...\n" + content[-300:]
            if content:
                lines.append(f"OUTPUT: {content}")
    transcript = "\n".join(lines)
    if len(transcript) > 30000:
        transcript = transcript[:15000] + "\n... [transcript truncated] ...\n" + transcript[-15000:]
    return transcript


async def _generate_cases_for_session(session_id: str, session_data: dict, messages: list) -> List[dict]:
    from llm import get_llm

    transcript = _render_session_for_llm(messages, session_data)
    if len(transcript) < 200:
        return []

    prompt = f"""You are a technical documentation expert creating a knowledge-base entry from a debugging session.

Analyze the transcript and extract structured troubleshooting cases. A valid case MUST be a closed loop: symptom, diagnosis, root cause, verified solution. If the session did not reach a conclusion, return empty.

Split multiple independent problems into separate cases. Skip dead ends; keep only the effective path.

For each case you MUST provide ALL of these fields:

1. title: concise descriptive title (in the session's language).
2. domain: exactly one of: {", ".join(DOMAINS)}.
3. platform: OS/device context.
4. tags: 5-8 keywords/phrases for categorization (include both the tool names and the problem category).
5. error_message: the ACTUAL error text the user would see (extract verbatim from transcript output if present, or describe the typical error message). This is critical for search.
6. search_queries: 5-8 different ways a user might describe or search for this problem in natural language (mix the session's language and English; include queries with specific error text, tool names, symptom descriptions).
7. symptom: what the user observed.
8. prerequisites: conditions that must be met BEFORE applying the fix (permissions, OS version, dependencies, network access).
9. diagnosis: key diagnostic steps with critical command outputs (brief).
10. root_cause: one-sentence explanation.
11. solution: exact commands with brief explanation of each.
12. verification: how to confirm the fix worked.
13. rollback: how to undo the changes if something goes wrong.
14. risk: potential side effects, performance impacts, or security considerations.
15. qa: 2-3 common questions with answers.

Transcript:
---
{transcript}
---

Respond ONLY with valid JSON (no markdown fences). Schema:
{{
  "cases": [
    {{
      "title": "...",
      "domain": "...",
      "platform": "...",
      "tags": ["...", "..."],
      "error_message": "...",
      "search_queries": ["...", "..."],
      "symptom": "...",
      "prerequisites": "...",
      "diagnosis": "...",
      "root_cause": "...",
      "solution": "...",
      "verification": "...",
      "rollback": "...",
      "risk": "...",
      "qa": [{{"q": "...", "a": "..."}}]
    }}
  ]
}}

If no closed-loop case, respond: {{"cases": []}}"""

    llm = get_llm()
    response = await llm.ainvoke([HumanMessage(content=prompt)])
    raw = str(response.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    cases = data.get("cases", [])
    results = []
    for c in cases:
        if not c.get("title") or not c.get("root_cause") or not c.get("solution"):
            continue
        c["domain"] = c.get("domain", "other") if c.get("domain") in DOMAINS else "other"
        c["session_id"] = session_id
        c["generated_at"] = datetime.now().isoformat()
        # Ensure list fields exist.
        if not isinstance(c.get("tags"), list):
            c["tags"] = []
        if not isinstance(c.get("search_queries"), list):
            c["search_queries"] = []
        if not isinstance(c.get("qa"), list):
            c["qa"] = []
        results.append(c)
    return results


def _yaml_escape(s: str) -> str:
    """Minimal YAML scalar escaping for frontmatter values."""
    s = str(s or "")
    if any(c in s for c in [':', '#', '{', '}', '[', ']', ',', '&', '*', '!', '|', '>', "'", '"', '%', '@', '`']):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _case_to_markdown(case: dict) -> str:
    """Render a case dict as a Markdown document with YAML frontmatter."""
    # --- YAML frontmatter (for retrieval / embedding) ---
    fm_lines = ["---"]
    fm_lines.append(f"title: {_yaml_escape(case.get('title', ''))}")
    fm_lines.append(f"domain: {_yaml_escape(case.get('domain', 'other'))}")
    fm_lines.append(f"platform: {_yaml_escape(case.get('platform', ''))}")
    fm_lines.append(f"generated_at: {_yaml_escape(case.get('generated_at', ''))}")
    fm_lines.append(f"source_session: {_yaml_escape(case.get('session_id', ''))}")
    tags = case.get("tags", [])
    if tags:
        fm_lines.append("tags:")
        for tag in tags:
            fm_lines.append(f"  - {_yaml_escape(tag)}")
    else:
        fm_lines.append("tags: []")
    sq = case.get("search_queries", [])
    if sq:
        fm_lines.append("search_queries:")
        for q in sq:
            fm_lines.append(f"  - {_yaml_escape(q)}")
    else:
        fm_lines.append("search_queries: []")
    fm_lines.append("---")
    fm_lines.append("")

    # --- Body sections ---
    body = [
        f"# {case.get('title', '')}",
        "",
        f"- **Domain:** {case.get('domain', '-')}",
        f"- **Platform:** {case.get('platform', '-')}",
        "",
        "## Symptom",
        "",
        case.get("symptom", ""),
        "",
        "## Error Message",
        "",
        case.get("error_message", "N/A"),
        "",
        "## Prerequisites",
        "",
        case.get("prerequisites", "N/A"),
        "",
        "## Diagnosis",
        "",
        case.get("diagnosis", ""),
        "",
        "## Root Cause",
        "",
        case.get("root_cause", ""),
        "",
        "## Solution",
        "",
        case.get("solution", ""),
        "",
        "## Verification",
        "",
        case.get("verification", ""),
        "",
        "## Rollback",
        "",
        case.get("rollback", "N/A"),
        "",
        "## Risk & Considerations",
        "",
        case.get("risk", "N/A"),
    ]

    qa_list = case.get("qa", [])
    if qa_list:
        body += ["", "## Common Questions", ""]
        for item in qa_list:
            body += [f"**Q: {item.get('q', '')}**", "", f"A: {item.get('a', '')}", ""]

    body.append("")
    body.append("---")
    return "\n".join(fm_lines + body)


def _save_case(case: dict) -> Optional[str]:
    domain = case.get("domain", "other")
    slug = _slugify(case["title"])
    domain_dir = os.path.join(CASES_DIR, domain)
    os.makedirs(domain_dir, exist_ok=True)
    filepath = os.path.join(domain_dir, f"{slug}.md")
    # Overwrite: the upgraded template is strictly richer than the old one.
    md = _case_to_markdown(case)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(md)
    return filepath


def _parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from a case Markdown file. Returns a dict."""
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    fm_text = parts[1].strip()
    meta = {}
    current_list_key = None
    for line in fm_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("- "):
            if current_list_key:
                meta.setdefault(current_list_key, []).append(stripped[2:].strip().strip('"'))
        elif ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"')
            if val == "[]":
                meta[key] = []
                current_list_key = None
            elif val:
                meta[key] = val
                current_list_key = None
            else:
                # Empty value -> might be a list header
                current_list_key = key
                meta.setdefault(key, [])
    return meta


async def generate_from_session(session_id: str, session_data: dict, messages: list) -> int:
    if len(messages) < MIN_MESSAGES_FOR_CASE:
        return 0
    try:
        cases = await _generate_cases_for_session(session_id, session_data, messages)
    except Exception as e:
        print(f"[case_generator] LLM extraction failed for {session_id}: {e}")
        return 0
    saved = 0
    for case in cases:
        path = _save_case(case)
        if path:
            saved += 1
            print(f"[case_generator] saved case: {path}")
    return saved


async def generate_from_all_sessions(session_manager) -> int:
    total = 0
    for s in session_manager.list_sessions():
        sid = s["session_id"]
        data = session_manager.load_session(sid)
        if not data:
            continue
        try:
            from web_server import clean_message_history
            messages = clean_message_history(messages_from_dict(data.get("messages", [])))
        except Exception:
            messages = messages_from_dict(data.get("messages", []))
        if len(messages) < MIN_MESSAGES_FOR_CASE:
            continue
        count = await generate_from_session(sid, data, messages)
        total += count
    return total


def list_cases() -> List[dict]:
    """List all generated cases with frontmatter metadata."""
    out = []
    if not os.path.isdir(CASES_DIR):
        return out
    for domain in sorted(os.listdir(CASES_DIR)):
        domain_dir = os.path.join(CASES_DIR, domain)
        if not os.path.isdir(domain_dir):
            continue
        for fn in sorted(os.listdir(domain_dir)):
            if not fn.endswith(".md"):
                continue
            try:
                with open(os.path.join(domain_dir, fn), "r", encoding="utf-8") as f:
                    content = f.read()
                meta = _parse_frontmatter(content)
                title = meta.get("title", fn.replace(".md", "").replace("-", " "))
                out.append({
                    "domain": domain,
                    "filename": fn,
                    "title": title,
                    "path": os.path.join(domain_dir, fn),
                    "size": len(content),
                    "tags": meta.get("tags", []),
                    "search_queries": meta.get("search_queries", []),
                    "platform": meta.get("platform", ""),
                })
            except Exception:
                pass
    return out