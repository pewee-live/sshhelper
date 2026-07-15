"""External Tool API -- RESTful endpoints for AI agents.

This module exposes the system's core device-operation capabilities as standard
HTTP endpoints under /api/v1/tools/, designed for consumption by external AI
agents (Dify, Coze, LangChain, etc.). It is intentionally separated from the
existing WebSocket-based human-interaction layer in web_server.py to avoid
coupling the two entry points.

Core logic (DEVICE_MANAGER, industrial clients, vault, groups, baselines) is
reused directly -- this layer only adds HTTP request/response handling and
credential resolution.
"""
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tools import (
    DEVICE_MANAGER,
    _clean_terminal_output,
)
from vault import VAULT
from device_groups import GROUP_MANAGER
from baseline import BASELINE_MANAGER
import audit
import paramiko

router = APIRouter(prefix="/tools", tags=["Device Tools"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_credentials(host: str, username: str = "root", password: Optional[str] = None,
                         port: int = 22) -> dict:
    """Resolve device credentials: use the provided password, or fall back to
    the encrypted vault. Returns a dict ready for paramiko.connect()."""
    if not password:
        password = VAULT.resolve(host)
    return {
        "hostname": host,
        "username": username,
        "password": password,
        "port": port,
    }


def _exec_ssh_one_shot(host: str, command: str, creds: dict) -> dict:
    """Open a temporary SSH connection, run a command, return structured result.
    This is the same pattern used by batch_run's _run_on_one_device, factored
    out so external API endpoints and batch ops share one implementation."""
    result = {"host": host, "exit_status": None, "output": ""}
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(timeout=10, **creds)
        try:
            stdin, stdout, stderr = client.exec_command(command, get_pty=True, timeout=120)
            output = _clean_terminal_output(stdout.read().decode("utf-8", errors="replace"))
            exit_status = stdout.channel.recv_exit_status()
            err = _clean_terminal_output(stderr.read().decode("utf-8", errors="replace"))
            if err:
                output += "\n[stderr]\n" + err
            result["exit_status"] = exit_status
            result["output"] = output.strip()[:8000]
        finally:
            client.close()
    except Exception as e:
        result["output"] = f"Connection error: {e}"
    return result


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    host: str = Field(..., description="Device IP or hostname")
    command: str = Field(..., description="Shell command to execute")
    username: str = Field("root", description="SSH username")
    password: Optional[str] = Field(None, description="SSH password (omit to use vault)")
    port: int = Field(22, description="SSH port")


class SnmpRequest(BaseModel):
    host: str = Field(..., description="Device IP")
    oid_or_name: str = Field(..., description="OID or known name (e.g. 'sysDescr', 'ifOperStatus')")
    operation: str = Field("get", description="'get' for single value, 'walk' for table traversal")
    community: str = Field("public")
    port: int = Field(161)
    version: int = Field(2, description="1 or 2 (SNMPv2c)")


class ModbusRequest(BaseModel):
    host: str = Field(..., description="Device IP")
    operation: str = Field(..., description="read_holding_registers | read_coils | write_register | write_coil")
    address: int = Field(..., description="Starting register/coil address")
    value: Optional[int] = Field(None, description="Value for write operations")
    count: int = Field(1, description="Number of registers/coils to read")
    port: int = Field(502)
    unit_id: int = Field(1)


class RedfishRequest(BaseModel):
    host: str = Field(..., description="BMC IP")
    username: str = Field(..., description="BMC username")
    password: str = Field(..., description="BMC password")
    path: str = Field("", description="Redfish resource path (e.g. 'Systems', 'Chassis/1/Thermal'). Empty = service root.")
    port: int = Field(443)
    use_https: bool = Field(True)


class IpmiRequest(BaseModel):
    host: str = Field(..., description="BMC IP")
    username: str = Field(..., description="BMC username")
    password: str = Field(..., description="BMC password")
    operation: str = Field("power", description="power | sensors | sel | inventory")
    port: int = Field(623)


class FileTransferRequest(BaseModel):
    host: str = Field(..., description="Device IP")
    local_path: str = Field(..., description="Path on the agent server")
    remote_path: str = Field(..., description="Path on the device")
    username: str = Field("root")
    password: Optional[str] = Field(None)
    port: int = Field(22)


class RebootRequest(BaseModel):
    host: str = Field(..., description="Device IP")
    username: str = Field("root")
    password: Optional[str] = Field(None)
    port: int = Field(22)
    wait_seconds: int = Field(60, description="Max seconds to wait for device to come back")


class BatchRunRequest(BaseModel):
    group_id: str = Field(..., description="Device group ID")
    command: str = Field(..., description="Shell command to run on every device")
    batch_size: int = Field(10, description="Concurrent devices per wave")
    max_failure_pct: int = Field(20, description="Abort threshold (percent of failures in a wave)")


class SnapshotRequest(BaseModel):
    host: str = Field(..., description="Device IP")
    username: str = Field("root")
    password: Optional[str] = Field(None)
    port: int = Field(22)


class DiffRequest(BaseModel):
    host: str = Field(..., description="Device IP")
    older_timestamp: str = Field("", description="Older snapshot timestamp (empty = auto)")
    newer_timestamp: str = Field("", description="Newer snapshot timestamp (empty = auto)")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/execute", summary="Execute a shell command on a device via SSH")
async def tool_execute(req: ExecuteRequest):
    """Run an arbitrary shell command on the target device. Credentials are
    resolved from the vault if no password is provided."""
    creds = _resolve_credentials(req.host, req.username, req.password, req.port)
    audit.record(session_id=None, device=f"ssh:{req.host}", command=req.command[:200], source="api")
    result = _exec_ssh_one_shot(req.host, req.command, creds)
    return {"status": "success", **result}


@router.post("/snmp", summary="Query a network device via SNMP")
async def tool_snmp(req: SnmpRequest):
    from industrial import SnmpClient
    try:
        client = SnmpClient(req.host, req.community, req.port, req.version)
        if req.operation == "walk":
            output = client.walk(req.oid_or_name)
        else:
            output = client.get(req.oid_or_name)
        return {"status": "success", "host": req.host, "output": output}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/modbus", summary="Read/write a Modbus TCP device")
async def tool_modbus(req: ModbusRequest):
    from industrial import ModbusClient
    try:
        client = ModbusClient(req.host, req.port, req.unit_id)
        if req.operation == "read_holding_registers":
            output = client.read_holding_registers(req.address, req.count)
        elif req.operation == "read_coils":
            output = client.read_coils(req.address, req.count)
        elif req.operation == "write_register":
            if req.value is None:
                raise HTTPException(400, "write_register requires a value")
            output = client.write_register(req.address, req.value)
        elif req.operation == "write_coil":
            output = client.write_coil(req.address, bool(req.value))
        else:
            raise HTTPException(400, f"Unknown operation: {req.operation}")
        client.close()
        return {"status": "success", "host": req.host, "output": output}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/redfish", summary="Query a server BMC via Redfish REST API")
async def tool_redfish(req: RedfishRequest):
    from industrial import RedfishClient
    try:
        client = RedfishClient(req.host, req.username, req.password, req.port, req.use_https)
        output = client.root() if not req.path else client.get(req.path)
        client.close()
        return {"status": "success", "host": req.host, "output": output}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/ipmi", summary="Query a server BMC via IPMI 2.0")
async def tool_ipmi(req: IpmiRequest):
    from industrial import IpmiClient
    try:
        client = IpmiClient(req.host, req.username, req.password, req.port)
        ops = {
            "power": client.get_power_state,
            "sensors": client.get_sensors,
            "sel": client.get_sel,
            "inventory": client.get_identify,
        }
        if req.operation not in ops:
            raise HTTPException(400, f"Unknown operation: {req.operation}")
        output = ops[req.operation]()
        return {"status": "success", "host": req.host, "output": output}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/upload", summary="Upload a file to a device via SFTP")
async def tool_upload(req: FileTransferRequest):
    """Push a local file to the device. The local_path must be accessible on the
    server running this system."""
    import os
    if not os.path.isfile(req.local_path):
        raise HTTPException(404, f"Local file not found: {req.local_path}")
    # Use a temporary session-like connection via DEVICE_MANAGER.
    temp_sid = f"ext_upload_{req.host}"
    try:
        DEVICE_MANAGER.connect_ssh(req.host, req.username, req.password, req.port, session_id=temp_sid)
        result = DEVICE_MANAGER.upload_file(req.local_path, req.remote_path, session_id=temp_sid)
        return {"status": "success", "host": req.host, "result": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


@router.post("/download", summary="Download a file from a device via SFTP")
async def tool_download(req: FileTransferRequest):
    temp_sid = f"ext_dl_{req.host}"
    try:
        DEVICE_MANAGER.connect_ssh(req.host, req.username, req.password, req.port, session_id=temp_sid)
        result = DEVICE_MANAGER.download_file(req.remote_path, req.local_path, session_id=temp_sid)
        return {"status": "success", "host": req.host, "result": result}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


@router.post("/reboot", summary="Reboot a device and wait for it to come back")
async def tool_reboot(req: RebootRequest):
    """Issues a reboot and polls until the device is reachable again, then runs
    a sanity check (echo alive; uptime)."""
    temp_sid = f"ext_reboot_{req.host}"
    try:
        DEVICE_MANAGER.connect_ssh(req.host, req.username, req.password, req.port, session_id=temp_sid)
        # Issue reboot
        conn = DEVICE_MANAGER.get_connection(temp_sid)
        try:
            conn.ssh_client.exec_command("nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 &", timeout=5)
        except Exception:
            pass  # expected as connection drops
        # Reconnect
        ok = DEVICE_MANAGER.reconnect(session_id=temp_sid, timeout=req.wait_seconds, poll_interval=5.0)
        if ok:
            result = DEVICE_MANAGER.execute("echo alive; uptime", session_id=temp_sid)
            return {"status": "success", "host": req.host, "reconnected": True, "output": result[:500]}
        return {"status": "error", "host": req.host, "reconnected": False,
                "message": f"Device did not come back within {req.wait_seconds}s"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


@router.post("/batch-run", summary="Run a command across a device group concurrently")
async def tool_batch_run(req: BatchRunRequest):
    """Fans out a command to all devices in a group with rolling batches and
    fail-fast protection. Credentials are resolved from the vault."""
    from tools import _run_on_one_device
    from concurrent.futures import ThreadPoolExecutor, as_completed

    group = GROUP_MANAGER.get(req.group_id)
    if not group:
        raise HTTPException(404, f"Device group '{req.group_id}' not found")
    specs = GROUP_MANAGER.resolve_credentials(group)
    ready = [s for s in specs if s.get("username")]
    if not ready:
        raise HTTPException(400, "No devices in group have resolvable credentials")

    audit.record(session_id=None, device=f"batch:{group.name}",
                 command=f"<api batch_run {req.command[:80]} on {len(ready)} devices>", source="api")

    all_results = []
    aborted = False
    for wave_start in range(0, len(ready), req.batch_size):
        wave = ready[wave_start:wave_start + req.batch_size]
        wave_results = []
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {pool.submit(_run_on_one_device, spec, req.command): spec for spec in wave}
            for fut in as_completed(futures):
                wave_results.append(fut.result())
        all_results.extend(wave_results)
        failures = sum(1 for r in wave_results if r["status"] != "success")
        if (failures / len(wave_results) * 100) > req.max_failure_pct and wave_start + req.batch_size < len(ready):
            aborted = True
            break

    succeeded = sum(1 for r in all_results if r["status"] == "success")
    failed = len(all_results) - succeeded
    return {
        "status": "success",
        "group": group.name,
        "devices_attempted": len(all_results),
        "succeeded": succeeded,
        "failed": failed,
        "aborted": aborted,
        "results": all_results,
    }


@router.post("/snapshot", summary="Capture a configuration baseline snapshot")
async def tool_snapshot(req: SnapshotRequest):
    """Captures ip addr, iptables, routes, mounts, services, etc. into a timestamped
    snapshot for later drift comparison."""
    from baseline import DEFAULT_PROBES
    temp_sid = f"ext_snap_{req.host}"
    try:
        DEVICE_MANAGER.connect_ssh(req.host, req.username, req.password, req.port, session_id=temp_sid)
        probes = {}
        for name, cmd in DEFAULT_PROBES.items():
            try:
                raw = DEVICE_MANAGER.execute(cmd, session_id=temp_sid)
                # Strip the execute() wrapper
                lines = raw.split("\n")
                cleaned = [l for l in lines if not l.startswith(("Exit Status:", "OUTPUT:", "---"))]
                probes[name] = "\n".join(cleaned).strip()[:5000]
            except Exception:
                probes[name] = "<error>"
        snapshot = BASELINE_MANAGER.save_snapshot(req.host, probes)
        return {"status": "success", "host": req.host, "snapshot": snapshot["timestamp"],
                "probes": list(probes.keys())}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


@router.post("/diff", summary="Compare two configuration snapshots")
async def tool_diff(req: DiffRequest):
    snaps = BASELINE_MANAGER.list_snapshots(req.host)
    if len(snaps) < 2:
        raise HTTPException(400, "Need at least 2 snapshots to diff")
    diff = BASELINE_MANAGER.diff(
        req.host,
        newer_ts=req.newer_timestamp or None,
        older_ts=req.older_timestamp or None,
    )
    if not diff:
        raise HTTPException(500, "Could not compute diff")
    return {"status": "success", "diff_text": BASELINE_MANAGER.format_diff(diff)}


# ---------------------------------------------------------------------------
# Knowledge base query (for external agents to consume accumulated cases)
# ---------------------------------------------------------------------------

kb_router = APIRouter(prefix="/kb", tags=["Knowledge Base"])


class SearchRequest(BaseModel):
    q: str = Field(..., description="Natural-language search query")


@router.post("/search", summary="Search the knowledge base for relevant cases", tags=["Knowledge Base"])
async def kb_search(q: str):
    """Simple text search across case titles, tags, and search_queries.
    Returns matching cases ranked by relevance."""
    import case_generator
    cases = case_generator.list_cases()
    q_lower = q.lower()
    scored = []
    for c in cases:
        score = 0
        title = c.get("title", "").lower()
        tags = [t.lower() for t in c.get("tags", [])]
        queries = [sq.lower() for sq in c.get("search_queries", [])]
        if q_lower in title:
            score += 10
        for t in tags:
            if q_lower in t or t in q_lower:
                score += 5
        for sq in queries:
            if q_lower in sq or sq in q_lower:
                score += 3
        # Also read the file content for a basic full-text match.
        try:
            with open(c["path"], "r", encoding="utf-8") as f:
                content = f.read().lower()
            if q_lower in content:
                score += 1
        except Exception:
            pass
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return {
        "status": "success",
        "query": q,
        "total_matches": len(scored),
        "cases": [c for _, c in scored[:10]],
    }


@router.get("/cases", summary="List all knowledge-base cases", tags=["Knowledge Base"])
async def kb_list_cases():
    import case_generator
    return {"status": "success", "cases": case_generator.list_cases()}


@router.get("/devices/{device_key}/profile", summary="Get a device's profile from memory", tags=["Knowledge Base"])
async def kb_device_profile(device_key: str):
    profile = DEVICE_PROFILE_MANAGER.get_profile(device_key)
    if not profile:
        raise HTTPException(404, "No profile found for this device")
    return {"status": "success", "profile": profile}