"""Live smoke test for the persistent-session MCP workflow.

Connects once through the in-memory MCP client, runs several read-only
commands on the SAME SSH session, prints status, then disconnects. This is the
Phase-1 acceptance test: "connect once, run many commands".

Usage:
    python scripts/mcp_smoke.py --host 192.168.0.142 --username radxa

Password (only if the device has no key/vault auth): set the SMOKE_PASSWORD
environment variable so the secret never appears in argv or logs.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import Client  # noqa: E402

import mcp_server  # noqa: E402


DEFAULT_COMMANDS = [
    "uname -a",
    "hostname",
    "uptime",
    "cat /etc/os-release | head -n 3",
    "free -m | head -n 3",
    "df -h / | tail -n 1",
]


def _text(result) -> str:
    """Extract the text payload from a FastMCP CallToolResult."""
    parts = [getattr(block, "text", str(block))
             for block in (getattr(result, "content", None) or [])]
    if not parts:
        data = getattr(result, "data", None)
        parts = [str(data) if data is not None else str(result)]
    return "\n".join(parts).strip()


async def run_smoke(host: str, username: str, port: int, commands: list,
                    timeout: int, password_env: str) -> int:
    password = os.environ.get(password_env, "")
    async with Client(mcp_server.mcp) as client:
        connected = _text(await client.call_tool("connect", {
            "host": host, "username": username, "password": password,
            "port": port, "verify": True,
        }))
        print("=== connect ===")
        print(connected)
        if connected.startswith("Error"):
            print("\nSmoke test FAILED at connect. If the device needs a "
                  f"password, set ${password_env} and retry.")
            return 2

        failures = 0
        for command in commands:
            result = _text(await client.call_tool(
                "run", {"command": command, "timeout": timeout}
            ))
            print(f"\n=== run: {command} ===")
            print(result)
            if "completed exit=0" not in result:
                failures += 1

        print("\n=== status ===")
        print(_text(await client.call_tool("status", {})))
        print("\n=== disconnect ===")
        print(_text(await client.call_tool("disconnect", {})))

        if failures:
            print(f"\nSmoke test finished with {failures} command(s) not "
                  "completing cleanly.")
            return 3
        print("\nSmoke test PASSED: one connection, "
              f"{len(commands)} commands reused the same session.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="device IP/hostname")
    parser.add_argument("--username", default="root")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--password-env", default="SMOKE_PASSWORD",
                        help="env var holding the SSH password (optional)")
    parser.add_argument("commands", nargs="*", default=None,
                        help="optional commands instead of the defaults")
    args = parser.parse_args()
    commands = args.commands or DEFAULT_COMMANDS
    return asyncio.run(run_smoke(
        args.host, args.username, args.port, commands,
        args.timeout, args.password_env,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
