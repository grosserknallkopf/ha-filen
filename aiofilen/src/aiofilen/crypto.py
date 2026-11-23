"""Client-side cryptography for Filen.

Ported from the official TypeScript SDK (FilenCloudDienste/filen-sdk-ts, MIT):
- login keys: PBKDF2-SHA512 (auth version 2) or Argon2id (auth version 3)
- metadata: AES-256-GCM, format "002" (PBKDF2-transformed key) or "003" (hex key)
- file data: AES-256-GCM per 1 MiB chunk, nonce prefixed, auth tag appended
- name hashes: SHA-512 (v2) or HMAC-SHA256 with an HKDF key (v3)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .exceptions import FilenCryptoError

# The SDK picks random string characters from this alphabet (byte % 64).
_RANDOM_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

LOGIN_ITERATIONS = 200_000
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536
ARGON2_PARALLELISM = 4


def random_string(length: int) -> str:
    """Random string from the SDK's alphabet; 32 chars double as a 32-byte AES key."""
    return "".join(secrets.choice(_RANDOM_CHARSET) for _ in range(length))


def random_hex(length: int) -> str:
    """Random hex string of `length` characters (length must be even)."""
    return secrets.token_bytes(length // 2).hex()


def derive_login_keys(
    raw_password: str, auth_version: int, salt: str
) -> tuple[str, str]:
    """Return (master key or DEK encryption key, password to send to /v3/login).

    The raw password never leaves this function's caller.
    """
    if auth_version == 2:
        derived = hashlib.pbkdf2_hmac(
            "sha512", raw_password.encode(), salt.encode(), LOGIN_ITERATIONS, 64
        ).hex()
        master_key = derived[: len(derived) // 2]
        password = hashlib.sha512(derived[len(derived) // 2 :].encode()).hexdigest()
        return master_key, password
    if auth_version == 3:
        derived = hash_secret_raw(
            secret=raw_password.encode(),
            salt=bytes.fromhex(salt),
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=64,
            type=Type.ID,
        ).hex()
        # First half encrypts the DEK, second half is the login password (not re-hashed).
        return derived[: len(derived) // 2], derived[len(derived) // 2 :]
    raise FilenCryptoError(f"unsupported auth version {auth_version}")


def _metadata_key(key: str, version: int) -> bytes:
    if version == 2:
        # The SDK transforms the (already derived) key once more with PBKDF2.
        return hashlib.pbkdf2_hmac("sha512", key.encode(), key.encode(), 1, 32)
    if version == 3:
        if len(key) != 64:
            raise FilenCryptoError("metadata version 3 needs a 64 character hex key")
        return bytes.fromhex(key)
    raise FilenCryptoError(f"unsupported metadata version {version}")


def encrypt_metadata(metadata: str, key: str, version: int) -> str:
    """Encrypt a small string (file metadata JSON, folder name, ...).

    Version 2 ("002") transforms the key with PBKDF2, version 3 ("003") uses
    the 64 character hex key directly. Official clients decrypt both.
    """
    key_bytes = _metadata_key(key, version)
    if version == 2:
        iv_str = random_string(12)
        iv = iv_str.encode()
        prefix = "002" + iv_str
    else:
        iv = secrets.token_bytes(12)
        prefix = "003" + iv.hex()
    encrypted = AESGCM(key_bytes).encrypt(iv, metadata.encode(), None)
    return prefix + base64.b64encode(encrypted).decode()


def decrypt_metadata_with_key(encrypted: str, key: str) -> str:
    """Decrypt one metadata string with one key; raises on wrong key or format."""
    if encrypted.startswith("U2FsdGVk"):
        raise FilenCryptoError("legacy CryptoJS metadata is not supported")
    version = encrypted[:3]
    if version == "002":
        iv = encrypted[3:15].encode()
        raw = base64.b64decode(encrypted[15:])
        key_bytes = _metadata_key(key, 2)
    elif version == "003":
        iv = bytes.fromhex(encrypted[3:27])
        raw = base64.b64decode(encrypted[27:])
        key_bytes = _metadata_key(key, 3)
    else:
        raise FilenCryptoError(f"unsupported metadata version {version!r}")
    try:
        return AESGCM(key_bytes).decrypt(iv, raw, None).decode()
    except (InvalidTag, ValueError, UnicodeDecodeError) as err:
        raise FilenCryptoError("metadata decryption failed") from err


def decrypt_metadata(encrypted: str, keys: list[str]) -> str:
    """Try every master key, newest first (password changes add keys)."""
    for key in reversed(keys):
        try:
            return decrypt_metadata_with_key(encrypted, key)
        except FilenCryptoError:
            continue
    raise FilenCryptoError("no master key could decrypt the metadata")


def _data_key(key: str, version: int) -> bytes:
    if version == 2:
        return key.encode()
    if version == 3:
        if len(key) != 64:
            raise FilenCryptoError("data version 3 needs a 64 character hex key")
        return bytes.fromhex(key)
    raise FilenCryptoError(f"unsupported file encryption version {version}")


def encrypt_chunk(data: bytes, key: str, version: int) -> bytes:
    """Encrypt one file chunk: nonce (12 bytes) + ciphertext + auth tag (16 bytes)."""
    iv = secrets.token_bytes(12)
    return iv + AESGCM(_data_key(key, version)).encrypt(iv, data, None)


def decrypt_chunk(data: bytes, key: str, version: int) -> bytes:
    """Decrypt one file chunk; a bad auth tag raises FilenCryptoError."""
    if len(data) < 12 + 16:
        raise FilenCryptoError("encrypted chunk is too short")
    iv, raw = data[:12], data[12:]
    try:
        return AESGCM(_data_key(key, version)).decrypt(iv, raw, None)
    except InvalidTag as err:
        raise FilenCryptoError("chunk decryption failed (wrong key or corrupt data)") from err


def hash_name(name: str, auth_version: int, hmac_key: bytes | None) -> str:
    """The nameHashed value the API uses for /dir/exists and /file/exists.

    v1/v2: SHA-1 over the hex-encoded SHA-512 of the lowercased name (the
    SDK's hashFn — not a plain SHA-512). v3: HMAC-SHA256 with the HKDF key.
    """
    if auth_version in (1, 2):
        inner = hashlib.sha512(name.lower().encode()).hexdigest()
        return hashlib.sha1(inner.encode()).hexdigest()  # noqa: S324 - Filen's format
    if hmac_key is None:
        raise FilenCryptoError("auth version 3 needs an HMAC key for name hashes")
    return hmac.new(hmac_key, name.lower().encode(), hashlib.sha256).hexdigest()


def hmac_key_from_private_key(private_key_b64: str) -> bytes:
    """HKDF-SHA256 over the DER private key, as the TypeScript SDK does."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"hmac-sha256-key",
    ).derive(base64.b64decode(private_key_b64))


def generate_key_pair() -> tuple[str, str]:
    """RSA-4096 keypair as base64 DER (public SPKI, private PKCS8), like the SDK."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    public_der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    private_der = private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return base64.b64encode(public_der).decode(), base64.b64encode(private_der).decode()