"""Tests for vault.py: encryption roundtrip and sanitization."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
from vault import CredentialVault


class TestVaultEncryption:
    """AES-GCM credential encryption roundtrip."""

    def setup_method(self):
        self.vault = CredentialVault()

    def test_store_and_resolve(self):
        self.vault.store("10.0.0.1", "ssh", {"host": "10.0.0.1", "username": "root"}, "s3cr3t")
        assert self.vault.resolve("10.0.0.1") == "s3cr3t"

    def test_resolve_missing_device(self):
        assert self.vault.resolve("10.0.0.999") is None

    def test_store_without_secret(self):
        self.vault.store("10.0.0.2", "ssh", {"host": "10.0.0.2"})
        assert self.vault.resolve("10.0.0.2") is None
        entry = self.vault.get("10.0.0.2")
        assert entry["has_secret"] is False

    def test_get_returns_sanitized(self):
        self.vault.store("10.0.0.3", "ssh", {"host": "10.0.0.3"}, "password123")
        entry = self.vault.get("10.0.0.3")
        assert "secret_enc" not in entry
        assert entry["has_secret"] is True
        assert entry["device_key"] == "10.0.0.3"

    def test_list_returns_sanitized(self):
        self.vault.store("10.0.0.4", "ssh", {"host": "10.0.0.4"}, "pwd")
        entries = self.vault.list()
        for e in entries:
            assert "secret_enc" not in e

    def test_delete(self):
        self.vault.store("10.0.0.5", "ssh", {"host": "10.0.0.5"}, "pwd")
        assert self.vault.delete("10.0.0.5") is True
        assert self.vault.resolve("10.0.0.5") is None

    def test_delete_missing(self):
        assert self.vault.delete("10.0.0.999") is False

    def test_plaintext_not_in_storage(self):
        """Critical security test: verify neither the DB nor any JSON file
        contains the plaintext password."""
        self.vault.store("10.0.0.6", "ssh", {"host": "10.0.0.6"}, "ultrasecret123")
        # Check SQLite database
        import sqlite3
        db_path = os.path.join("data", "app.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT secret_enc FROM device_credentials").fetchall()
            conn.close()
            for row in rows:
                assert "ultrasecret123" not in (row[0] or ""), "Plaintext found in DB!"
        # Also check legacy JSON file if it exists
        vault_path = os.path.join("data", "vault", "vault.json")
        if os.path.exists(vault_path):
            with open(vault_path, "r", encoding="utf-8") as f:
                raw = f.read()
            assert "ultrasecret123" not in raw