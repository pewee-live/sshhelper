"""Encrypted credential vault.

Stores device connection credentials at rest, encrypted with AES-GCM under a
master key. The master key is read from the VAULT_MASTER_KEY env var; if absent
it is generated once and persisted to data/vault/.master_key (so the first run
works out of the box, and operators can rotate it later).

The vault never returns plaintext over the API -- it exposes only metadata for
listing, and a resolve() helper used internally by the connection manager to
fetch the secret at connect/reconnect time.
"""
import os
import json
import base64
import hashlib
import secrets
from datetime import datetime
from typing import Optional, Dict, Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

_VAULT_DIR = "data/vault"
_VAULT_FILE = os.path.join(_VAULT_DIR, "vault.json")
_MASTER_KEY_FILE = os.path.join(_VAULT_DIR, ".master_key")
_PBKDF2_SALT = b"ssh-helper-vault-salt"  # fixed salt; secrecy comes from the master key
_PBKDF2_ITERS = 200_000


def _derive_key(master_material: bytes) -> bytes:
    """Derive a 256-bit AES key from raw master material via PBKDF2."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_PBKDF2_SALT, iterations=_PBKDF2_ITERS)
    return kdf.derive(master_material)


def _load_master_key() -> bytes:
    """Return the 32-byte master key, generating + persisting one if needed."""
    os.makedirs(_VAULT_DIR, exist_ok=True)
    env_key = os.getenv("VAULT_MASTER_KEY")
    if env_key:
        return _derive_key(env_key.encode("utf-8"))
    if os.path.exists(_MASTER_KEY_FILE):
        with open(_MASTER_KEY_FILE, "rb") as f:
            raw = f.read().strip()
            if raw:
                return _derive_key(raw)
    # Generate a new random master and pin it.
    raw = secrets.token_bytes(32)
    with open(_MASTER_KEY_FILE, "wb") as f:
        f.write(raw)
    try:
        os.chmod(_MASTER_KEY_FILE, 0o600)
    except Exception:
        pass  # Windows ignores chmod; fine.
    return _derive_key(raw)


class CredentialVault:
    """AES-GCM encrypted credential store, keyed by a stable device key."""

    def __init__(self):
        self._key = _load_master_key()
        self._aesgcm = AESGCM(self._key)
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if os.path.exists(_VAULT_FILE):
            try:
                with open(_VAULT_FILE, "r", encoding="utf-8") as f:
                    self._entries = json.load(f)
            except Exception:
                self._entries = {}

    def _save(self):
        os.makedirs(_VAULT_DIR, exist_ok=True)
        with open(_VAULT_FILE, "w", encoding="utf-8") as f:
            json.dump(self._entries, f, ensure_ascii=False, indent=2)

    def _encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def _decrypt(self, blob: str) -> str:
        raw = base64.b64decode(blob)
        nonce, ct = raw[:12], raw[12:]
        return self._aesgcm.decrypt(nonce, ct, None).decode("utf-8")

    def store(self, device_key: str, conn_type: str, params: dict, secret: Optional[str] = None) -> dict:
        """Persist a credential entry. `secret` (e.g. SSH password) is AES-GCM
        encrypted before storage. Uses SQLite for atomic, concurrent-safe writes."""
        if not device_key:
            return None
        secret_enc = self._encrypt(secret) if secret else None
        try:
            from db import db
            db.store_credential(device_key, conn_type, params, secret_enc)
        except Exception:
            # Fallback to legacy JSON storage.
            entry = {
                "device_key": device_key, "conn_type": conn_type,
                "params": params, "secret_enc": secret_enc,
                "has_secret": bool(secret), "updated_at": datetime.now().isoformat(),
            }
            self._entries[device_key] = entry
            self._save()
        return {
            "device_key": device_key, "conn_type": conn_type, "params": params,
            "has_secret": bool(secret), "updated_at": datetime.now().isoformat(),
        }

    def resolve(self, device_key: str) -> Optional[str]:
        """Return the decrypted secret for a device, or None."""
        secret_enc = None
        try:
            from db import db
            row = db.get_credential(device_key)
            if row:
                secret_enc = row.get("secret_enc")
        except Exception:
            pass
        if not secret_enc:
            entry = self._entries.get(device_key, {})
            secret_enc = entry.get("secret_enc")
        if not secret_enc:
            return None
        try:
            return self._decrypt(secret_enc)
        except Exception as e:
            print(f"[vault] decrypt failed for {device_key}: {e}")
            return None

    def get(self, device_key: str) -> Optional[dict]:
        try:
            from db import db
            row = db.get_credential(device_key)
            if row:
                return self._sanitize(row)
        except Exception:
            pass
        entry = self._entries.get(device_key)
        return self._sanitize(entry) if entry else None

    def list(self):
        try:
            from db import db
            rows = db.list_credentials()
            if rows:
                return rows
        except Exception:
            pass
        return [self._sanitize(e) for e in self._entries.values()]

    def delete(self, device_key: str) -> bool:
        try:
            from db import db
            if db.delete_credential(device_key):
                return True
        except Exception:
            pass
        if device_key in self._entries:
            del self._entries[device_key]
            self._save()
            return True
        return False

    @staticmethod
    def _sanitize(entry: Dict[str, Any]) -> dict:
        """Strip ciphertext so nothing sensitive leaks into API responses."""
        return {
            "device_key": entry.get("device_key"),
            "conn_type": entry.get("conn_type"),
            "params": entry.get("params", {}),
            "has_secret": entry.get("has_secret", False),
            "updated_at": entry.get("updated_at"),
        }


VAULT = CredentialVault()