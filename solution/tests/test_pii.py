import hashlib

import pytest
from cryptography.fernet import InvalidToken

from solution.db import PiiKeyMismatch, check_pii_fingerprint, connect, init_db
from solution.pii import ENV_VAR, PiiVault, load_vault

PHONE = "+919876543210"


def test_hash_is_stable_and_hex():
    v = PiiVault(b"key-one")
    h = v.phone_hash(PHONE)
    assert h == v.phone_hash(PHONE) == PiiVault(b"key-one").phone_hash(PHONE)
    assert len(h) == 64 and int(h, 16) >= 0
    assert v.phone_hash("+919876543211") != h


def test_hash_is_keyed():
    assert PiiVault(b"key-one").phone_hash(PHONE) != PiiVault(b"key-two").phone_hash(PHONE)
    assert PiiVault(b"key-one").phone_hash(PHONE) != hashlib.sha256(PHONE.encode()).hexdigest()


def test_derived_keys_are_distinct():
    v = PiiVault(b"key-one")
    assert len({v._hash_key, v._enc_key, v._fp_key}) == 3
    assert b"key-one" not in (v._hash_key, v._enc_key, v._fp_key)


def test_encrypt_roundtrip():
    v = PiiVault(b"key-one")
    blob = {"customer_name": "Kavya Sharma", "phone": PHONE, "other": "Ṣ₹ू"}
    token = v.encrypt(blob)
    assert v.decrypt(token) == blob
    assert b"Sharma" not in token and PHONE.encode() not in token
    assert v.decrypt(memoryview(token)) == blob   # sqlite may hand back memoryview/bytes


def test_encrypt_wrong_key_fails():
    token = PiiVault(b"key-one").encrypt({"a": "b"})
    with pytest.raises(InvalidToken):
        PiiVault(b"key-two").decrypt(token)


def test_fingerprint():
    assert PiiVault(b"key-one").fingerprint() == PiiVault(b"key-one").fingerprint()
    assert PiiVault(b"key-one").fingerprint() != PiiVault(b"key-two").fingerprint()
    assert len(PiiVault(b"key-one").fingerprint()) == 16


def test_fingerprint_mismatch_raises(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    check_pii_fingerprint(conn, PiiVault(b"key-one").fingerprint())
    check_pii_fingerprint(conn, PiiVault(b"key-one").fingerprint())
    with pytest.raises(PiiKeyMismatch):
        check_pii_fingerprint(conn, PiiVault(b"key-two").fingerprint())
    conn.close()


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        PiiVault(b"")


def test_load_vault_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, "env-key")
    kf = tmp_path / ".pii_key"
    v = load_vault(kf)
    assert v.fingerprint() == PiiVault(b"env-key").fingerprint()
    assert not kf.exists()


def test_load_vault_generates_then_reuses_key_file(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(ENV_VAR, raising=False)
    kf = tmp_path / ".pii_key"
    v1 = load_vault(kf)
    assert kf.exists()
    assert "WARNING" in capsys.readouterr().err
    v2 = load_vault(kf)
    assert v1.fingerprint() == v2.fingerprint()
    assert capsys.readouterr().err == ""   # no warning when the file already exists
