"""Data models for aiofilen."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


@dataclass(slots=True)
class Credentials:
    """What a client needs to persist after login.

    The API key is long-lived (Filen has no refresh endpoint); master keys
    decrypt everything, the private key is needed for v3 name hashes.
    """

    api_key: str = field(repr=False)
    master_keys: list[str] = field(repr=False)
    auth_version: int
    email: str
    base_folder_uuid: str
    private_key: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key,
            "master_keys": self.master_keys,
            "auth_version": self.auth_version,
            "email": self.email,
            "base_folder_uuid": self.base_folder_uuid,
            "private_key": self.private_key,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Credentials:
        return cls(
            api_key=data["api_key"],
            master_keys=list(data["master_keys"]),
            auth_version=int(data["auth_version"]),
            email=data["email"],
            base_folder_uuid=data["base_folder_uuid"],
            private_key=data.get("private_key"),
        )


@dataclass(slots=True, frozen=True)
class AuthInfo:
    """Result of /v3/auth/info: how to derive the login keys."""

    salt: str
    auth_version: int
    user_id: int


@dataclass(slots=True, frozen=True)
class User:
    """Account data from /v3/user/info."""

    user_id: int
    email: str
    max_storage: int
    storage_used: int
    base_folder_uuid: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> User:
        return cls(
            user_id=int(data["id"]),
            email=data["email"],
            max_storage=int(data["maxStorage"]),
            storage_used=int(data["storageUsed"]),
            base_folder_uuid=data["baseFolderUUID"],
        )


@dataclass(slots=True, frozen=True)
class DriveFolder:
    """A folder in Filen, with its decrypted name."""

    uuid: str
    name: str
    parent_uuid: str | None


@dataclass(slots=True, frozen=True)
class DriveFile:
    """A file in Filen, with decrypted metadata.

    `key` encrypts the chunk data, `version` is the file encryption version
    (2 or 3). `hash` is the SHA-512 of the plaintext, if the uploader stored it.
    """

    uuid: str
    name: str
    size: int
    mime: str
    key: str = field(repr=False)
    chunks: int
    bucket: str
    region: str
    parent_uuid: str
    version: int
    last_modified: int | None
    creation: int | None
    hash: str | None
    rm: str
    timestamp: int | None

    @classmethod
    def from_api(cls, data: dict[str, Any], metadata: dict[str, Any]) -> DriveFile:
        size = int(data.get("size") or 0)
        return cls(
            uuid=data["uuid"],
            name=metadata.get("name") or "",
            size=size,
            mime=metadata.get("mime") or "application/octet-stream",
            key=metadata.get("key") or "",
            chunks=int(data.get("chunks") or 0),
            bucket=data.get("bucket") or "",
            region=data.get("region") or "",
            parent_uuid=data.get("parent") or "",
            version=int(data.get("version") or 2),
            last_modified=metadata.get("lastModified"),
            creation=metadata.get("creation"),
            hash=metadata.get("hash"),
            rm=data.get("rm") or "",
            timestamp=data.get("timestamp"),
        )

    def metadata_json(self) -> str:
        """The metadata JSON as stored (encrypted) in Filen."""
        data: dict[str, Any] = {
            "name": self.name,
            "size": self.size,
            "mime": self.mime,
            "key": self.key,
            "lastModified": self.last_modified,
        }
        if self.creation is not None:
            data["creation"] = self.creation
        if self.hash is not None:
            data["hash"] = self.hash
        return json.dumps(data)