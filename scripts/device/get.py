"""Download a file from a device using vault-resolved SSH credentials."""
import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

import paramiko  # noqa: E402
from vault import VAULT  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SFTP get a file from a device (vault credentials).")
    parser.add_argument("--device", help="device key from data/devices/")
    parser.add_argument("--host", help="SSH host (overrides device profile)")
    parser.add_argument("--user", help="SSH username (overrides device profile)")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("remote")
    parser.add_argument("local")
    args = parser.parse_args()

    host, user = args.host, args.user
    if args.device:
        host = host or args.device
        user = user or "root"
    if not host or not user:
        print("Error: --device or --host/--user is required", file=sys.stderr)
        return 1

    password = os.environ.get("BOARD_PASSWORD") or VAULT.resolve(host)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, port=args.port,
                   username=user, password=password, timeout=10)
    try:
        sftp = client.open_sftp()
        sftp.get(args.remote, args.local)
        sftp.close()
    finally:
        client.close()
    print(f"downloaded {args.remote} -> {args.local}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
