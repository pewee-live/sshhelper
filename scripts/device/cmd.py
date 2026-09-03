"""Run one shell command on a device using vault-resolved credentials.

Convenience CLI for local diagnostics and ad-hoc operations:
    python scripts/device/cmd.py --device 192.168.0.142 "uname -a"
    python scripts/device/cmd.py --host 192.168.1.100 --user root "uptime"

--device reads the device profile from data/devices/<key>.json to resolve
host. Use --host/--user for boards not yet in the profile.
"""
import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

import paramiko  # noqa: E402
from vault import VAULT  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a command on a device via SSH (vault credentials).")
    parser.add_argument("--device", help="device key from data/devices/")
    parser.add_argument("--host", help="SSH host (overrides device profile)")
    parser.add_argument("--user", help="SSH username (overrides device profile)")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--timeout", type=int, default=30,
                        help="command timeout in seconds (default 30)")
    parser.add_argument("command", help="shell command to run")
    args = parser.parse_args()

    host, user = args.host, args.user
    if args.device:
        host = host or args.device
        user = user or "root"
    if not host or not user:
        print("Error: --device or --host/--user is required", file=sys.stderr)
        return 1

    command = args.command
    if command == "-":
        command = sys.stdin.read()

    password = os.environ.get("BOARD_PASSWORD") or VAULT.resolve(host)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, port=args.port,
                       username=user, password=password, timeout=10)
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(15)
        stdin, stdout, stderr = client.exec_command(command,
                                                    get_pty=True,
                                                    timeout=args.timeout)
        output = stdout.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        print(output.rstrip())
        return status
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
