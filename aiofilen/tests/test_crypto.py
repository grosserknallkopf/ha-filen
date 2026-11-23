"""Tests for the Filen cryptography (formats ported from filen-sdk-ts)."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from aiofilen import crypto
from aiofilen.exceptions import FilenCryptoError


def test_derive_login_keys_v2():
    master_key, password = crypto.derive_login_keys("hunter2", 2, "somesalt")
    derived = hashlib.pbkdf2_hmac("sha512", b"hunter2", b"somesalt", 200000, 64).hex()
    assert master_key == derived[:64]
    assert password == hashlib.sha512(derived[64:].encode()).hexdigest()
    assert len(master_key) == 64
    assert len(password) == 128


def test_derive_login_keys_v3():
    from argon2.low_level import Type, hash_secret_raw

    salt = bytes.fromhex("ab" * 16)
    kek, password = crypto.derive_login_keys("hunter2", 3, salt.hex())
    derived = hash_secret_raw(
        secret=b"hunter2",
        salt=salt,
        time_cost=3,
        memory_cost=65536,
        parallelism=4,
        hash_len=64,
        type=Type.ID,
    ).hex()
    assert kek == derived[:64]
    assert password == derived[64:]  # v3 does not re-hash the login password


def test_derive_login_keys_rejects_v1():
    with pytest.raises(FilenCryptoError):
        crypto.derive_login_keys("x", 1, "salt")


@pytest.mark.parametrize("version", [2, 3])
def test_metadata_roundtrip(version):
    key = crypto.random_hex(64) if version == 3 else crypto.random_string(32)
    text = '{"name": "Backups", "size": 123}'
    encrypted = crypto.encrypt_metadata(text, key, version)
    assert encrypted.startswith("002" if version == 2 else "003")
    assert crypto.decrypt_metadata_with_key(encrypted, key) == text
    assert crypto.decrypt_metadata(encrypted, ["old-key", key]) == text


def test_metadata_v2_format():
    key = crypto.random_string(32)
    encrypted = crypto.encrypt_metadata("hello", key, 2)
    # "002" + 12 char nonce + base64(ciphertext + 16 byte tag)
    iv = encrypted[3:15]
    assert len(iv) == 12
    raw = base64.b64decode(encrypted[15:])
    assert len(raw) == len("hello") + 16
    derived = hashlib.pbkdf2_hmac("sha512", key.encode(), key.encode(), 1, 32)
    assert len(derived) == 32


def test_metadata_v3_format():
    key = crypto.random_hex(64)
    encrypted = crypto.encrypt_metadata("hello", key, 3)
    # "003" + 24 hex chars (12 byte nonce) + base64(ciphertext + tag)
    iv = bytes.fromhex(encrypted[3:27])
    assert len(iv) == 12
    raw = base64.b64decode(encrypted[27:])
    assert len(raw) == len("hello") + 16


def test_metadata_wrong_key():
    key = crypto.random_string(32)
    encrypted = crypto.encrypt_metadata("secret", key, 2)
    with pytest.raises(FilenCryptoError):
        crypto.decrypt_metadata(encrypted, [crypto.random_string(32)])


def test_metadata_legacy_unsupported():
    with pytest.raises(FilenCryptoError):
        crypto.decrypt_metadata_with_key("U2FsdGVkX1whatever", "key")


@pytest.mark.parametrize("version", [2, 3])
def test_chunk_roundtrip(version):
    key = crypto.random_hex(64) if version == 3 else crypto.random_string(32)
    data = bytes(range(256)) * 100
    encrypted = crypto.encrypt_chunk(data, key, version)
    assert len(encrypted) == 12 + len(data) + 16
    assert crypto.decrypt_chunk(encrypted, key, version) == data


def test_chunk_tamper_detected():
    key = crypto.random_string(32)
    encrypted = bytearray(crypto.encrypt_chunk(b"data", key, 2))
    encrypted[20] ^= 0xFF
    with pytest.raises(FilenCryptoError):
        crypto.decrypt_chunk(bytes(encrypted), key, 2)


def test_chunk_wrong_version_key():
    with pytest.raises(FilenCryptoError):
        crypto.encrypt_chunk(b"x", "too-short", 3)


def test_hash_name_v2():
    # official test vectors from filen-sdk-go (sha1 over the hex sha512)
    assert crypto.hash_name("abc", 2, None) == "5c5a4ad792911a5a58741e16257f62b664aa2df3"
    assert crypto.hash_name("cde", 2, None) == "dc4237084f19afa9eb668edcbc39b5da51f63273"
    assert crypto.hash_name("ABC", 2, None) == crypto.hash_name("abc", 2, None)


def test_hash_name_v3():
    private_key = base64.b64encode(b"pretend-der-bytes").decode()
    hmac_key = crypto.hmac_key_from_private_key(private_key)
    assert len(hmac_key) == 32
    expected = hmac.new(hmac_key, b"foo.txt", hashlib.sha256).hexdigest()
    assert crypto.hash_name("Foo.TXT", 3, hmac_key) == expected
    with pytest.raises(FilenCryptoError):
        crypto.hash_name("foo", 3, None)


def test_random_string_charset():
    value = crypto.random_string(64)
    assert len(value) == 64
    assert set(value) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_generate_key_pair():
    from cryptography.hazmat.primitives.serialization import (
        load_der_private_key,
        load_der_public_key,
    )

    public, private = crypto.generate_key_pair()
    load_der_public_key(base64.b64decode(public))
    load_der_private_key(base64.b64decode(private), password=None)