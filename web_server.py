import asyncio
import os
import json
import uuid
import threading
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from fastapi import UploadFile, File as FastAPIFile

from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
from langchain_core.messages import messages_from_dict, messages_to_dict
from agent import build_hardware_agent
from llm import get_llm


# --- Cost / token tracking config (configurable via env) ---------------------
# Per-1M-token prices in USD. Defaults follow DeepSeek-chat (cache-miss).
# Override in .env for your provider. Tokens are always tracked regardless.
PRICE_INPUT_PER_1M = float(os.getenv("PRICE_INPUT_PER_1M", "0.27"))
PRICE_OUTPUT_PER_1M = float(os.getenv("PRICE_OUTPUT_PER_1M", "1.10"))
CURRENCY = os.getenv("COST_CURRENCY", "USD")


from tools import DEVICE_MANAGER, DEVICE_PROFILE_MANAGER
from vault import VAULT
import audit
from device_groups import GROUP_MANAGER
from baseline import BASELINE_MANAGER
import case_generator
from external_api import router as external_router


def clean_message_history(messages):
    """
    Clean up message history to ensure validity for LLM API:
    1. Every AIMessage with tool_calls must have corresponding ToolMessages for all of its tool_call IDs.
       If any tool_call is missing its ToolMessage, we discard the AIMessage and all its associated ToolMessages.
    2. Every ToolMessage must have a preceding AIMessage with a matching tool_call ID.
       If not, we discard the ToolMessage.
    """
    tool_messages_by_id = {}
    for msg in messages:
        if isinstance(msg, ToolMessage) and msg.tool_call_id:
            tool_messages_by_id[msg.tool_call_id] = msg

    valid_message_ids = set()

    for msg in messages:
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                has_missing = False
                for tc in msg.tool_calls:
                    tc_id = tc.get("id")
                    if not tc_id or tc_id not in tool_messages_by_id:
                        has_missing = True
                        break
                if not has_missing:
                    valid_message_ids.add(id(msg))
                    for tc in msg.tool_calls:
                        tc_id = tc.get("id")
                        valid_message_ids.add(id(tool_messages_by_id[tc_id]))
            else:
                valid_message_ids.add(id(msg))
        elif isinstance(msg, ToolMessage):
            pass
        else:
            valid_message_ids.add(id(msg))

    cleaned = [msg for msg in messages if id(msg) in valid_message_ids]
    return cleaned


def compute_usage(messages):
    """Sum reported token usage across all AIMessages. Returns cumulative
    input/output tokens consumed by the session so far."""
    input_tokens = 0
    output_tokens = 0
    for m in messages:
        usage = getattr(m, "usage_metadata", None)
        if isinstance(usage, dict):
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
    cost = round(
        (input_tokens / 1_000_000) * PRICE_INPUT_PER_1M
        + (output_tokens / 1_000_000) * PRICE_OUTPUT_PER_1M,
        4,
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost": cost,
        "currency": CURRENCY,
        "price_input_per_1m": PRICE_INPUT_PER_1M,
        "price_output_per_1m": PRICE_OUTPUT_PER_1M,
    }


def _device_key_for_session(session_id):
    """Resolve the persistent device key for a session: prefer the live
    connection, fall back to the stored connection params."""
    key = DEVICE_MANAGER.get_device_key(session_id)
    if key:
        return key
    data = SESSION_MANAGER.load_session(session_id)
    params = (data or {}).get("connection_params", {})
    return params.get("host") or params.get("serial_port")


def _connection_params_for_session(session_id):
    """Return the stored connection_params for a session (used to give the
    agent's industrial-protocol tools their default host/credentials without
    the user having to repeat them in every message)."""
    data = SESSION_MANAGER.load_session(session_id)
    return (data or {}).get("connection_params", {}) or {}


app = FastAPI()

# Mount external tool API (for AI agents) under /api/v1, cleanly separated
# from the existing /api/* human-interaction endpoints.
app.include_router(external_router, prefix="/api/v1")

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static", html=True), name="static")


@app.get("/")
async def serve_root():
    return FileResponse("static/index.html")


class ConnectRequest(BaseModel):
    session_id: str
    conn_type: str
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = 22
    serial_port: Optional[str] = None
    baudrate: Optional[int] = 115200
    industrial: Optional[dict] = None


class PasswordSubmit(BaseModel):
    password: str
    session_id: Optional[str] = None


class InterventionSubmit(BaseModel):
    action: str          # "send" | "abort" | "wait"
    input: Optional[str] = ""
    session_id: Optional[str] = None


class InterruptRequest(BaseModel):
    session_id: str


class SessionManager:
    def __init__(self, data_dir="data/sessions"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

    def list_sessions(self):
        sessions = []
        for filename in os.listdir(self.data_dir):
            if filename.endswith(".json"):
                with open(os.path.join(self.data_dir, filename), "r", encoding="utf-8") as f:
                    try:
                        data = json.load(f)
                        sessions.append({
                            "session_id": data.get("session_id"),
                            "name": data.get("name", "Unknown Session"),
                            "conn_type": data.get("conn_type"),
                            "host": data.get("connection_params", {}).get("host"),
                            "username": data.get("connection_params", {}).get("username"),
                            "serial_port": data.get("connection_params", {}).get("serial_port"),
                            "updated_at": data.get("updated_at"),
                        })
                    except Exception:
                        pass
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def create_session(self, name="New Session"):
        session_id = str(uuid.uuid4())
        data = {
            "session_id": session_id,
            "name": name,
            "conn_type": None,
            "connection_params": {},
            "messages": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        self.save_session(session_id, data)
        return session_id

    def load_session(self, session_id):
        filepath = os.path.join(self.data_dir, f"{session_id}.json")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def save_session(self, session_id, data):
        data["updated_at"] = datetime.now().isoformat()
        filepath = os.path.join(self.data_dir, f"{session_id}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


SESSION_MANAGER = SessionManager()

# --- Global runtime state --------------------------------------------------
# Concurrency model: an agent run is a session-scoped background task that is
# fully decoupled from any WebSocket. WebSockets are just "viewers" that attach
# to a session. Switching sessions (detaching the viewer) never cancels the run.
agent_app = None
main_loop: Optional[asyncio.AbstractEventLoop] = None

active_sessions = {}        # session_id -> messages list (LLM history)
active_agent_tasks = {}     # session_id -> asyncio.Task (running or finished)
session_viewers = {}        # session_id -> WebSocket currently attached (or None)
session_events = {}         # session_id -> list[dict] live event buffer for replay
active_session_id = None    # last-viewed session, used only as a fallback
session_safe_mode = {}      # session_id -> bool (user-toggled safe mode)

# Cross-thread human-input plumbing (tool threads block on these).
password_events = {}        # session_id -> threading.Event
password_values = {}        # session_id -> str
pending_passwords = {}      # session_id -> prompt text awaiting a human
intervention_events = {}    # session_id -> threading.Event
intervention_values = {}    # session_id -> {"action", "input"}
pending_interventions = {}  # session_id -> context text awaiting a human

SESSION_EVENT_BUFFER_CAP = 1500


def _is_running(session_id):
    task = active_agent_tasks.get(session_id)
    return task is not None and not task.done()


async def broadcast(session_id, event):
    """Append an event to the session's replay buffer and push it to the viewer
    if one is currently attached."""
    buf = session_events.setdefault(session_id, [])
    buf.append(event)
    if len(buf) > SESSION_EVENT_BUFFER_CAP:
        del buf[: len(buf) - SESSION_EVENT_BUFFER_CAP]
    ws = session_viewers.get(session_id)
    if ws is not None:
        try:
            await ws.send_json(event)
        except Exception:
            pass


def web_on_output(text: str, session_id: Optional[str] = None):
    """Stream terminal prints to the web interface (called from tool threads)."""
    sid = session_id or "_default"
    event = {"type": "log", "content": text}
    buf = session_events.setdefault(sid, [])
    buf.append(event)
    if len(buf) > SESSION_EVENT_BUFFER_CAP:
        del buf[: len(buf) - SESSION_EVENT_BUFFER_CAP]
    ws = session_viewers.get(sid)
    if ws is not None and main_loop:
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(event), main_loop)
        except Exception:
            pass
    elif sid == "_default":
        print(text, end="", flush=True)


def web_on_password_request(prompt: str, session_id: Optional[str] = None) -> str:
    """Request a password from the web user and block until submitted."""
    sid = session_id or "_default"
    if main_loop is None:
        from getpass import getpass
        return getpass(prompt)
    pending_passwords[sid] = prompt
    ws = session_viewers.get(sid)
    if ws is not None:
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "password_request", "prompt": prompt, "session_id": sid}),
                main_loop,
            )
        except Exception:
            pass
    ev = password_events.setdefault(sid, threading.Event())
    ev.clear()
    ev.wait()
    pending_passwords.pop(sid, None)
    return password_values.pop(sid, "")


def web_on_intervention(context: str, session_id: Optional[str] = None) -> dict:
    """Hand control to the human when a command stalls. Blocks the tool thread
    until the human decides what to do."""
    sid = session_id or "_default"
    pending_interventions[sid] = context
    ws = session_viewers.get(sid)
    if ws is not None and main_loop:
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "intervention_request", "context": context, "session_id": sid}),
                main_loop,
            )
        except Exception:
            pass
    ev = intervention_events.setdefault(sid, threading.Event())
    ev.clear()
    ev.wait()
    pending_interventions.pop(sid, None)
    return intervention_values.pop(sid, {"action": "wait"})


def web_on_state_change(state: str, session_id: Optional[str] = None):
    sid = session_id or "_default"
    ws = session_viewers.get(sid)
    if ws is not None and main_loop:
        try:
            asyncio.run_coroutine_threadsafe(
                ws.send_json({"type": "status", "content": state, "session_id": sid}), main_loop
            )
        except Exception:
            pass


DEVICE_MANAGER.on_output = web_on_output
DEVICE_MANAGER.on_password_request = web_on_password_request
DEVICE_MANAGER.on_state_change = web_on_state_change
DEVICE_MANAGER.on_intervention = web_on_intervention


@app.on_event("startup")
def startup_event():
    global main_loop, agent_app
    main_loop = asyncio.get_running_loop()
    try:
        agent_app = build_hardware_agent()
        print("LangGraph Agent loaded successfully.")
    except Exception as e:
        print(f"Agent failed to build: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Graceful shutdown: cancel every running agent task and give each a chance
    to persist its messages (their CancelledError path calls _persist_session)
    before the process exits. Without this, a Docker `stop` (SIGTERM) or Ctrl+C
    would drop whatever was in-flight."""
    print("[shutdown] flushing in-flight agent sessions...")
    tasks = list(active_agent_tasks.items())
    for session_id, task in tasks:
        if not task.done():
            task.cancel()
    # Let each cancelled task reach its persistence path before we exit.
    if tasks:
        await asyncio.gather(
            *(t for _, t in tasks if not t.done()),
            return_exceptions=True,
        )
    # Belt-and-suspenders: ensure every active session's latest messages are saved.
    for session_id, messages in list(active_sessions.items()):
        _persist_session(session_id, messages)
    print("[shutdown] all sessions persisted.")


@app.get("/api/sessions")
async def list_sessions():
    sessions = SESSION_MANAGER.list_sessions()
    for s in sessions:
        s["running"] = _is_running(s.get("session_id"))
    return {"status": "success", "sessions": sessions}


@app.post("/api/sessions")
async def create_session():
    session_id = SESSION_MANAGER.create_session()
    active_sessions[session_id] = []
    return {"status": "success", "session_id": session_id}


class RenameRequest(BaseModel):
    name: str


@app.post("/api/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: RenameRequest):
    data = SESSION_MANAGER.load_session(session_id)
    if not data:
        return {"status": "error", "message": "Session not found"}
    name = (req.name or "").strip()
    if not name:
        return {"status": "error", "message": "Name cannot be empty"}
    data["name"] = name
    SESSION_MANAGER.save_session(session_id, data)
    return {"status": "success", "name": name}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    # Cancel any running task for this session before removing it.
    task = active_agent_tasks.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()
    active_sessions.pop(session_id, None)
    session_viewers.pop(session_id, None)
    session_events.pop(session_id, None)
    filepath = os.path.join(SESSION_MANAGER.data_dir, f"{session_id}.json")
    removed = False
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            removed = True
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "success", "removed": removed}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    data = SESSION_MANAGER.load_session(session_id)
    if data:
        if session_id not in active_sessions:
            try:
                raw_msgs = messages_from_dict(data.get("messages", []))
                active_sessions[session_id] = clean_message_history(raw_msgs)
            except Exception as e:
                print("Error loading messages:", e)
                active_sessions[session_id] = []
        else:
            cleaned = clean_message_history(active_sessions[session_id])
            if len(cleaned) < len(active_sessions[session_id]):
                active_sessions[session_id].clear()
                active_sessions[session_id].extend(cleaned)
                data["messages"] = messages_to_dict(active_sessions[session_id])
                SESSION_MANAGER.save_session(session_id, data)

        history = []
        for m in active_sessions[session_id]:
            if isinstance(m, HumanMessage):
                history.append({"type": "user_message", "content": m.content})
            elif hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    history.append({"type": "tool_call", "name": tc["name"], "args": tc["args"]})
            elif getattr(m, "content", None):
                history.append({"type": "agent_message", "content": m.content})

        data["history"] = history
        data["running"] = _is_running(session_id)
        data["usage"] = compute_usage(active_sessions.get(session_id, []))
        device_key = _device_key_for_session(session_id)
        data["device_profile"] = DEVICE_PROFILE_MANAGER.get_profile(device_key)
        return {"status": "success", "session": data}
    return {"status": "error", "message": "Session not found"}


def _render_session_markdown(session_id, data, messages):
    """Render a session's full debugging transcript as a readable Markdown report."""
    name = data.get("name", "Session")
    params = data.get("connection_params", {})
    device = data.get("conn_type") or "unknown"
    target = params.get("host") or params.get("serial_port") or "-"
    usage = compute_usage(messages)

    lines = [
        f"# Debug Session: {name}",
        "",
        f"- **Device:** {device} `{target}`",
        f"- **Created:** {data.get('created_at', '-')}",
        f"- **Updated:** {data.get('updated_at', '-')}",
        f"- **Tokens:** {usage['total_tokens']:,} (in {usage['input_tokens']:,} / out {usage['output_tokens']:,})",
        f"- **Est. cost:** {usage['estimated_cost']} {usage['currency']}",
        "",
        "---",
        "",
    ]

    for m in messages:
        if isinstance(m, HumanMessage):
            lines += ["## User", "", str(m.content), ""]
        elif isinstance(m, AIMessage):
            tool_calls = getattr(m, "tool_calls", None) or []
            if tool_calls:
                lines.append("### Agent — commands")
                lines.append("")
                for tc in tool_calls:
                    args = tc.get("args", {})
                    cmd = args.get("command") if isinstance(args, dict) else args
                    lines.append(f"- `{cmd}`")
                lines.append("")
            content = str(getattr(m, "content", "") or "").strip()
            if content:
                lines += ["### Agent", "", content, ""]
        elif isinstance(m, ToolMessage):
            content = str(getattr(m, "content", "") or "").rstrip()
            # Keep tool output readable but trim very long dumps in the report.
            if len(content) > 3000:
                content = content[:1500] + f"\n\n... [{len(content) - 3000} chars truncated] ...\n\n" + content[-1500:]
            lines += ["<details><summary>Command output</summary>", "", "```", content, "```", "", "</details>", ""]

    return "\n".join(lines)


@app.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str, format: str = "markdown"):
    data = SESSION_MANAGER.load_session(session_id)
    if not data:
        return {"status": "error", "message": "Session not found"}
    messages = active_sessions.get(session_id)
    if messages is None:
        try:
            messages = clean_message_history(messages_from_dict(data.get("messages", [])))
        except Exception:
            messages = []

    if format == "json":
        content = json.dumps(
            {**data, "usage": compute_usage(messages)},
            ensure_ascii=False, indent=2,
        )
        mime = "application/json"
        ext = "json"
    else:
        content = _render_session_markdown(session_id, data, messages)
        mime = "text/markdown"
        ext = "md"

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in (data.get("name", "session")))[:60]
    filename = f"{safe_name}.{ext}"

    import io
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/status")
async def get_status(session_id: Optional[str] = None):
    """Connection + agent status for a session."""
    sid = session_id or active_session_id
    status = {
        "device_profiles": len(DEVICE_PROFILE_MANAGER.list_profiles()),
        "connected": False,
        "active_session_id": active_session_id,
        "running": _is_running(sid),
    }
    conn = DEVICE_MANAGER.get_connection(sid)
    if conn.conn_type == "ssh" and getattr(conn, "ssh_client", None):
        try:
            host = conn.ssh_client.get_transport().getpeername()[0]
        except Exception:
            host = "Unknown"
        status.update({"connected": True, "conn_type": "ssh", "message": f"Connected to SSH at {host}"})
    elif conn.conn_type == "serial" and getattr(conn, "serial_client", None):
        status.update({"connected": True, "conn_type": "serial", "message": f"Connected to Serial port {conn.serial_client.port}"})
    return status


@app.get("/api/health")
async def health():
    """Liveness/readiness probe for container orchestrators & reverse proxies."""
    return {"status": "ok", "agent_ready": agent_app is not None}


class SafeModeRequest(BaseModel):
    session_id: Optional[str] = None
    enabled: bool


@app.post("/api/safe-mode")
async def toggle_safe_mode(req: SafeModeRequest):
    """Toggle safe mode for a session. When enabled, the agent will NOT execute
    any system-modifying commands -- it will only suggest them with explanations."""
    sid = req.session_id or active_session_id
    if not sid:
        return {"status": "error", "message": "No session specified."}
    session_safe_mode[sid] = bool(req.enabled)
    return {"status": "success", "session_id": sid, "safe_mode": session_safe_mode[sid]}


@app.get("/api/safe-mode")
async def get_safe_mode(session_id: Optional[str] = None):
    sid = session_id or active_session_id
    return {"safe_mode": session_safe_mode.get(sid, False) if sid else False}

# --- Case generation & knowledge base ---

@app.post("/api/cases/generate")
async def generate_cases(session_id: Optional[str] = None, all_sessions: bool = False):
    """Manually trigger case generation from one session or all sessions."""
    if all_sessions:
        count = await case_generator.generate_from_all_sessions(SESSION_MANAGER)
        return {"status": "success", "generated": count}
    sid = session_id or active_session_id
    if not sid:
        return {"status": "error", "message": "No session specified."}
    data = SESSION_MANAGER.load_session(sid)
    if not data:
        return {"status": "error", "message": "Session not found."}
    try:
        messages = clean_message_history(messages_from_dict(data.get("messages", [])))
    except Exception:
        messages = messages_from_dict(data.get("messages", []))
    count = await case_generator.generate_from_session(sid, data, messages)
    return {"status": "success", "generated": count}


@app.get("/api/cases")
async def list_cases_endpoint():
    """List all generated cases grouped by domain."""
    cases = case_generator.list_cases()
    by_domain = {}
    for c in cases:
        by_domain.setdefault(c["domain"], []).append(c)
    return {"status": "success", "cases": cases, "by_domain": by_domain, "total": len(cases)}


@app.get("/api/cases/{domain}/{filename}")
async def get_case(domain: str, filename: str):
    """Read a single case's full Markdown content."""
    safe_domain = os.path.basename(domain)
    safe_fn = os.path.basename(filename)
    path = os.path.join("data", "cases", safe_domain, safe_fn)
    if not os.path.exists(path):
        return {"status": "error", "message": "Case not found"}
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content, media_type="text/markdown")

@app.post("/api/connect")
async def connect(req: ConnectRequest):
    global active_session_id
    try:
        session_data = SESSION_MANAGER.load_session(req.session_id)
        if not session_data:
            return {"status": "error", "message": "Session not found"}

        if req.conn_type == "ssh":
            msg = DEVICE_MANAGER.connect_ssh(req.host, req.username, req.password, req.port, session_id=req.session_id)
            session_data["conn_type"] = "ssh"
            session_data["connection_params"] = {"host": req.host, "username": req.username, "port": req.port}
            if session_data["name"] == "New Session":
                session_data["name"] = f"SSH: {req.host}"
        elif req.conn_type == "serial":
            msg = DEVICE_MANAGER.connect_serial(req.serial_port, req.baudrate, session_id=req.session_id)
            session_data["conn_type"] = "serial"
            session_data["connection_params"] = {"serial_port": req.serial_port, "baudrate": req.baudrate}
            if session_data["name"] == "New Session":
                session_data["name"] = f"Serial: {req.serial_port}"
        elif req.conn_type in ("snmp", "redfish", "ipmi", "modbus"):
            # Industrial protocols don't open a persistent connection -- they store
            # connection params in the session so the agent's protocol tools can
            # use them on each query. Optionally persist credentials to the vault.
            params = req.industrial or {}
            host = params.get("host", "") or req.host or ""
            session_data["conn_type"] = req.conn_type
            session_data["connection_params"] = params
            if host:
                session_data["connection_params"]["host"] = host
            if session_data["name"] == "New Session":
                session_data["name"] = f"{req.conn_type.upper()}: {host}"
            # Persist credentials for redfish/ipmi into the encrypted vault.
            if req.conn_type in ("redfish", "ipmi") and host:
                try:
                    VAULT.store(
                        device_key=f"{req.conn_type}:{host}",
                        conn_type=req.conn_type,
                        params=params,
                        secret=params.get("password"),
                    )
                except Exception:
                    pass
            msg = f"Configured {req.conn_type.upper()} access to {host}. The agent can now query this device via the {req.conn_type}_query tool."
        else:
            return {"status": "error", "message": "Unknown conn_type"}

        SESSION_MANAGER.save_session(req.session_id, session_data)
        active_session_id = req.session_id
        return {"status": "success", "message": msg}
    except Exception as e:
        audit.record(
            session_id=req.session_id,
            device=f"{req.conn_type}:{req.host or req.serial_port}",
            command=f"<connect failed>", exit_status=1, source="ui", detail=str(e)[:200],
        )
        return {"status": "error", "message": str(e)}


@app.post("/api/disconnect")
async def disconnect_hardware(session_id: Optional[str] = None):
    global active_session_id
    sid = session_id or active_session_id
    task = active_agent_tasks.get(sid)
    if task is not None and not task.done():
        task.cancel()
    try:
        DEVICE_MANAGER.disconnect(session_id=sid)
        if active_session_id == sid:
            active_session_id = None
        audit.record(session_id=sid, device="*", command="<disconnect>", source="ui")
        return {"status": "success", "message": "Disconnected"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/devices")
async def list_devices():
    """List all known device profiles (memory)."""
    return {"status": "success", "devices": DEVICE_PROFILE_MANAGER.list_profiles()}


# --- File upload (staging area for the agent's upload_file tool) ---
# Files uploaded via the UI land in data/uploads/<session_id>/ so the agent can
# reference them by a stable local path when calling the upload_file tool.
UPLOAD_DIR = os.path.join("data", "uploads")


@app.post("/api/sessions/{session_id}/upload")
async def upload_session_file(session_id: str, file: UploadFile = FastAPIFile(...)):
    dest_dir = os.path.join(UPLOAD_DIR, session_id)
    os.makedirs(dest_dir, exist_ok=True)
    safe_name = os.path.basename(file.filename or "upload.bin")
    dest = os.path.join(dest_dir, safe_name)
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    abs_dest = os.path.abspath(dest)
    return JSONResponse({
        "status": "success",
        "local_path": abs_dest,
        "filename": safe_name,
        "size": os.path.getsize(abs_dest),
        "hint": f"File staged. Tell the agent to upload it with: upload_file(local_path='{abs_dest}', remote_path='/path/on/device').",
    })


class DeviceProfileUpdate(BaseModel):
    device_key: str
    notes: Optional[str] = None


@app.post("/api/devices")
async def update_device_notes(req: DeviceProfileUpdate):
    """Manually append notes to a device's profile from the UI."""
    profile = DEVICE_PROFILE_MANAGER.update(req.device_key, notes=req.notes)
    if profile:
        return {"status": "success", "profile": profile}
    return {"status": "error", "message": "Could not update device profile"}


# --- Credential vault + audit log ---

class VaultEntry(BaseModel):
    device_key: str
    conn_type: str = "ssh"
    host: Optional[str] = None
    username: Optional[str] = None
    port: Optional[int] = 22
    secret: Optional[str] = None


@app.get("/api/vault/devices")
async def list_vault():
    return {"status": "success", "devices": VAULT.list()}


@app.post("/api/vault/devices")
async def store_vault(req: VaultEntry):
    params = {"host": req.host or req.device_key, "username": req.username, "port": req.port or 22}
    entry = VAULT.store(req.device_key, req.conn_type, params, req.secret)
    return {"status": "success", "entry": entry}


@app.delete("/api/vault/devices/{device_key}")
async def delete_vault(device_key: str):
    removed = VAULT.delete(device_key)
    return {"status": "success" if removed else "error", "removed": removed}


@app.get("/api/audit")
async def get_audit(
    session_id: Optional[str] = None,
    device: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 200,
):
   records = audit.query(session_id=session_id, device=device, source=source, limit=min(limit, 1000))
   return {"status": "success", "records": records, "count": len(records)}


# --- Device groups (batch orchestration) ---

class DeviceTargetModel(BaseModel):
    host: str
    conn_type: str = "ssh"
    username: Optional[str] = "root"
    port: int = 22


class CreateGroupRequest(BaseModel):
    name: str
    targets: list[DeviceTargetModel] = []


class AddDeviceRequest(BaseModel):
    host: str
    conn_type: str = "ssh"
    username: Optional[str] = "root"
    port: int = 22


@app.get("/api/device-groups")
async def list_groups():
    return {"status": "success", "groups": GROUP_MANAGER.list()}


@app.post("/api/device-groups")
async def create_group(req: CreateGroupRequest):
    targets = [t.model_dump() for t in req.targets]
    group = GROUP_MANAGER.create(req.name, targets)
    return {"status": "success", "group": group.to_dict()}


@app.get("/api/device-groups/{group_id}")
async def get_group(group_id: str):
    group = GROUP_MANAGER.get(group_id)
    if not group:
        return {"status": "error", "message": "Group not found"}
    return {"status": "success", "group": group.to_dict()}


@app.post("/api/device-groups/{group_id}/devices")
async def add_device_to_group(group_id: str, req: AddDeviceRequest):
    group = GROUP_MANAGER.add_device(group_id, req.model_dump())
    if not group:
        return {"status": "error", "message": "Group not found"}
    return {"status": "success", "group": group.to_dict()}


@app.delete("/api/device-groups/{group_id}")
async def delete_group(group_id: str):
    removed = GROUP_MANAGER.delete(group_id)
    return {"status": "success" if removed else "error", "removed": removed}


# --- Configuration baselines & drift ---

@app.get("/api/baselines/{device_key}")
async def list_baselines(device_key: str):
    """List all config snapshots for a device."""
    return {"status": "success", "snapshots": BASELINE_MANAGER.list_snapshots(device_key)}


@app.get("/api/baselines/{device_key}/diff")
async def get_baseline_diff(device_key: str, older: str = "", newer: str = ""):
    """Get the drift diff between two snapshots of a device."""
    snaps = BASELINE_MANAGER.list_snapshots(device_key)
    if len(snaps) < 2:
        return {"status": "error", "message": "Need at least 2 snapshots to diff."}
    diff = BASELINE_MANAGER.diff(device_key, newer_ts=newer or None, older_ts=older or None)
    if not diff:
        return {"status": "error", "message": "Could not compute diff."}
    return {"status": "success", "diff": diff, "text": BASELINE_MANAGER.format_diff(diff)}


@app.post("/api/password")
async def submit_password(req: PasswordSubmit):
    sid = req.session_id or active_session_id or "_default"
    password_values[sid] = req.password
    if sid in password_events:
        password_events[sid].set()
    return {"status": "ok"}


@app.post("/api/intervention")
async def submit_intervention(req: InterventionSubmit):
    sid = req.session_id or active_session_id or "_default"
    intervention_values[sid] = {"action": req.action, "input": req.input or ""}
    if sid in intervention_events:
        intervention_events[sid].set()
    return {"status": "ok"}


@app.post("/api/interrupt")
async def interrupt_execution(req: InterruptRequest):
    session_id = req.session_id
    interrupted = False
    task = active_agent_tasks.get(session_id)
    if task is not None and not task.done():
        task.cancel()
        interrupted = True
    DEVICE_MANAGER.interrupt(session_id=session_id)
    sid = session_id or "_default"
    # Release any blocking human-input prompts so the tool thread can exit.
    intervention_values[sid] = {"action": "abort", "input": ""}
    if sid in intervention_events:
        intervention_events[sid].set()
    password_values[sid] = ""
    if sid in password_events:
        password_events[sid].set()
    return {"status": "success", "interrupted": interrupted}


# Context compaction logic is extracted to context_compaction.py.
from context_compaction import summarize_context, compact_messages


def _persist_session(session_id, messages):
    try:
        session_data = SESSION_MANAGER.load_session(session_id)
        if session_data:
            session_data["messages"] = messages_to_dict(messages)
            SESSION_MANAGER.save_session(session_id, session_data)
    except Exception as e:
       print(f"Failed to persist session {session_id}: {e}")


def _trigger_case_generation(session_id: str, messages: list):
    """Fire-and-forget: extract reusable cases from a just-finished session.
    Runs as a background asyncio task so the user never waits for it."""
    async def _do_generate():
        try:
            data = SESSION_MANAGER.load_session(session_id)
            if not data:
                return
            count = await case_generator.generate_from_session(session_id, data, messages)
            if count > 0:
                print(f"[cases] generated {count} case(s) from session {session_id}")
                await broadcast(session_id, {"type": "case_generated", "count": count})
        except Exception as e:
            print(f"[cases] auto-generation failed for {session_id}: {e}")
    if main_loop:
        asyncio.run_coroutine_threadsafe(_do_generate(), main_loop)


async def run_agent_workflow(session_id: str, messages: list):
    """Run the agent graph as a session-scoped background task. It broadcasts
    events to whatever viewer is attached and buffers them for late viewers."""
    try:
        device_key = _device_key_for_session(session_id)
        device_profile = DEVICE_PROFILE_MANAGER.get_profile_text(device_key)
        config = {
            "recursion_limit": 500,
            "configurable": {
                "session_id": session_id,
                "device_profile": device_profile,
                "connection_params": _connection_params_for_session(session_id),
                "safe_mode": session_safe_mode.get(session_id, False),
            },
        }
        await broadcast(session_id, {"type": "status", "content": "Thinking..."})
        _last_persist_msg_count = len(messages)
        # Persist at most once per this many new messages, so a crash mid-run
        # loses only the last second of work rather than the whole turn.
        _PERSIST_EVERY_N_MSGS = 2
        async for event in agent_app.astream({"messages": messages}, config=config):
            for node_name, node_state in event.items():
                if node_name == "agent":
                    msg = node_state["messages"][-1]
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            await broadcast(session_id, {"type": "tool_call", "name": tc["name"], "args": tc["args"]})
                    else:
                        await broadcast(session_id, {"type": "agent_message", "content": msg.content})
                    # Stream the model reasoning/thinking trace if present.
                    reasoning = ""
                    try:
                        ak = getattr(msg, "additional_kwargs", {}) or {}
                        reasoning = ak.get("reasoning_content") or ak.get("reasoning") or ""
                    except Exception:
                        pass
                    if reasoning:
                        await broadcast(session_id, {"type": "reasoning", "content": reasoning})
                elif node_name in ["tools", "invalid_tools"]:
                    await broadcast(session_id, {"type": "status", "content": "Thinking..."})
                new_msgs = node_state["messages"]
                if isinstance(new_msgs, list):
                    messages.extend(new_msgs)
                else:
                    messages.append(new_msgs)

                # Incremental checkpoint: persist periodically so a server crash
                # never wipes a whole turn's worth of executed commands/output.
                if len(messages) - _last_persist_msg_count >= _PERSIST_EVERY_N_MSGS:
                    _persist_session(session_id, messages)
                    _last_persist_msg_count = len(messages)

        # Final flush of any messages added since the last checkpoint.
        if len(messages) != _last_persist_msg_count:
            _persist_session(session_id, messages)
        await broadcast(session_id, {"type": "status", "content": "Ready"})
        # Notify any attached viewer (and buffer it for late viewers) that the
        # run finished -- the UI uses this to fire a browser notification/sound,
        # especially when the user has switched to another tab.
        await broadcast(session_id, {"type": "run_done", "session_id": session_id})
        # History is now durably saved; clear the live replay buffer.
        session_events.setdefault(session_id, []).clear()
        # Auto-generate reusable cases from this session in the background.
        # Fire-and-forget: the user should never wait for case extraction.
        _trigger_case_generation(session_id, messages)
    except asyncio.CancelledError:
        try:
            await broadcast(session_id, {"type": "status", "content": "Ready"})
            await broadcast(session_id, {"type": "agent_message", "content": "Execution interrupted by user."})
        except Exception:
            pass
        _persist_session(session_id, messages)
        raise
    except Exception as e:
        print(f"Graph Execution Error in session {session_id}: {e}")
        try:
            await broadcast(session_id, {"type": "error", "content": f"Graph Execution Error: {str(e)}"})
        except Exception:
            pass
        _persist_session(session_id, messages)


@app.websocket("/ws/chat")
async def websocket_endpoint(ws: WebSocket, session_id: str = Query(...)):
    global active_session_id
    await ws.accept()
    session_viewers[session_id] = ws
    active_session_id = session_id

    if session_id not in active_sessions:
        data = SESSION_MANAGER.load_session(session_id)
        if data:
            try:
                raw_msgs = messages_from_dict(data.get("messages", []))
                active_sessions[session_id] = clean_message_history(raw_msgs)
            except Exception:
                active_sessions[session_id] = []
        else:
            active_sessions[session_id] = []
    else:
        cleaned = clean_message_history(active_sessions[session_id])
        if len(cleaned) < len(active_sessions[session_id]):
            active_sessions[session_id].clear()
            active_sessions[session_id].extend(cleaned)
            data = SESSION_MANAGER.load_session(session_id)
            if data:
                data["messages"] = messages_to_dict(active_sessions[session_id])
                SESSION_MANAGER.save_session(session_id, data)

    messages = active_sessions[session_id]

    # Replay buffered live events so a reconnecting viewer catches up on a run
    # that started (or finished) while no one was watching.
    for ev in list(session_events.get(session_id, [])):
        try:
            await ws.send_json(ev)
        except Exception:
            break

    # If a password/intervention request fired while nobody was watching,
    # re-deliver it now so the human can answer.
    pp = pending_passwords.get(session_id)
    if pp is not None:
        try:
            await ws.send_json({"type": "password_request", "prompt": pp, "session_id": session_id})
        except Exception:
            pass
    pi = pending_interventions.get(session_id)
    if pi is not None:
        try:
            await ws.send_json({"type": "intervention_request", "context": pi, "session_id": session_id})
        except Exception:
            pass

    try:
        await ws.send_json({"type": "status", "content": "Thinking..." if _is_running(session_id) else "Ready"})
    except Exception:
        pass

    try:
        while True:
            data = await ws.receive_text()
            if not data.strip():
                continue

            # One run per session at a time; ignore messages while busy.
            if _is_running(session_id):
                try:
                    await ws.send_json({"type": "agent_message", "content": "A task is already running in this session. Wait for it to finish or stop it first."})
                except Exception:
                    pass
                continue

            cleaned = clean_message_history(messages)
            if len(cleaned) < len(messages):
                messages.clear()
                messages.extend(cleaned)
                data_sess = SESSION_MANAGER.load_session(session_id)
                if data_sess:
                    data_sess["messages"] = messages_to_dict(messages)
                    SESSION_MANAGER.save_session(session_id, data_sess)

            messages.append(HumanMessage(content=data))
            # Persist the user's message immediately, before the (possibly long)
            # agent run starts. If the run crashes or the server dies, we still
            # know what the user asked.
            _persist_session(session_id, messages)
            await broadcast(session_id, {"type": "user_message", "content": data})
            await broadcast(session_id, {"type": "status", "content": "Thinking..."})

            messages = await summarize_context(messages)
            active_sessions[session_id] = messages

            task = asyncio.create_task(run_agent_workflow(session_id, messages))
            active_agent_tasks[session_id] = task
            task.add_done_callback(lambda t, sid=session_id: active_agent_tasks.pop(sid, None))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error for session {session_id}: {e}")
    finally:
        # Only detach the viewer. NEVER cancel the agent task here -- that is the
        # whole point of concurrent sessions.
        if session_viewers.get(session_id) is ws:
            session_viewers[session_id] = None


if __name__ == "__main__":
    import uvicorn

    print("Starting Web Server at http://localhost:8000/")
    uvicorn.run("web_server:app", host="0.0.0.0", port=8000, reload=True)
