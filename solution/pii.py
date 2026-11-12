"""PII protection: keyed phone hash (match key), Fernet blobs for the rest, key fingerprint.

One master key; three independent keys are derived from it with distinct HKDF labels, so the
hash key, the encryption key and the fingerprint key never coincide.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENV_VAR = "LEDGER_PII_KEY"
DEFAULT_KEY_FILE = Path(__file__).with_name(".pii_key")

_LABEL_HASH = b"agentic-ledger/v1/phone-hash"
_LABEL_ENCRYPT = b"agentic-ledger/v1/pii-encrypt"
_LABEL_FINGERPRINT = b"agentic-ledger/v1/key-fingerprint"


def _derive(master: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=label).derive(master)


class PiiVault:
    def __init__(self, master_key: bytes):
        if not master_key:
            raise ValueError("PII master key must not be empty")
        self._hash_key = _derive(master_key, _LABEL_HASH)
        self._enc_key = _derive(master_key, _LABEL_ENCRYPT)
        self._fp_key = _derive(master_key, _LABEL_FINGERPRINT)
        self._fernet = Fernet(base64.urlsafe_b64encode(self._enc_key))

    def phone_hash(self, canonical_phone: str) -> str:
        """Deterministic keyed hash (hex) of '+91XXXXXXXXXX'. Same input and key -> same output."""
        return hmac.new(self._hash_key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()

    def encrypt(self, data: dict) -> bytes:
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(payload)

    def decrypt(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(bytes(token)).decode("utf-8"))

    def fingerprint(self) -> str:
        """Short identifier of the key, safe to store: reveals nothing usable about it."""
        return hmac.new(self._fp_key, b"fingerprint", hashlib.sha256).hexdigest()[:16]


def load_vault(key_file: Path | None = None) -> PiiVault:
    """Key from LEDGER_PII_KEY, else from the key file (generated with a warning if missing)."""
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return PiiVault(env.encode("utf-8"))
    path = Path(key_file) if key_file is not None else DEFAULT_KEY_FILE
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError(f"PII key file {path} is empty")
        return PiiVault(text.encode("utf-8"))
    key = secrets.token_hex(32)
    path.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"WARNING: {ENV_VAR} is not set; generated a new PII key at {path}. "
          f"Keep it: without it this database's PII cannot be matched or decrypted.", file=sys.stderr)
    return PiiVault(key.encode("utf-8"))
