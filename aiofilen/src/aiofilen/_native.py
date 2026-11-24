"""Lazy access to the native Filen SDK bridge and error translation.

The bridge is a compiled extension (`filen-sdk-native`). Importing it at module
import time would make the integration fail to load before Home Assistant has
installed its requirements; more importantly, the UniFFI Python bindings want
the running event loop, which only exists once Home Assistant is starting. So
everything goes through :func:`load`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .exceptions import (
    FilenAccountBlocked,
    FilenAuthError,
    FilenConflictError,
    FilenConnectionError,
    FilenCryptoError,
    FilenError,
    FilenIntegrityError,
    FilenNotFoundError,
    FilenQuotaError,
    FilenRateLimitError,
    FilenStreamError,
    FilenTwoFactorRequired,
)

_MODULE: Any = None


class NativeUnavailable(RuntimeError):
    """The compiled `filen-sdk-native` package is not installed."""


def load() -> Any:
    """Import and configure the native module, once."""
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    try:
        import filen_sdk_native as module  # noqa: PLC0415
    except ImportError as err:  # pragma: no cover - only on a broken install
        raise NativeUnavailable(
            "The Filen native SDK is not installed. Install filen-sdk-native "
            "for this platform and restart Home Assistant."
        ) from err
    setter = getattr(module, "uniffi_set_event_loop", None)
    if setter is None:
        # `__all__` in the generated package does not re-export the helper;
        # the implementation module still has it.
        implementation = getattr(module, "filen_sdk_native", None)
        setter = getattr(implementation, "uniffi_set_event_loop", None)
    if setter is not None:
        try:
            setter(asyncio.get_running_loop())
        except RuntimeError:
            pass  # imported from a worker thread; async calls set it later
    _MODULE = module
    return module


def loaded() -> Any:
    """The native module if it has been loaded, else None."""
    return _MODULE


def credentials_to_native(credentials: Any) -> Any:
    """Convert an aiofilen Credentials object to the native record."""
    native = load()
    return native.Credentials(
        api_key=credentials.api_key,
        master_keys=list(credentials.master_keys),
        auth_version=credentials.auth_version,
        email=credentials.email,
        base_folder_uuid=credentials.base_folder_uuid,
        user_id=credentials.user_id,
        private_key=credentials.private_key,
    )


def credentials_fields(value: Any) -> dict[str, Any]:
    """Fields of the native Credentials record."""
    return {
        "api_key": value.api_key,
        "master_keys": list(value.master_keys),
        "auth_version": value.auth_version,
        "email": value.email,
        "base_folder_uuid": value.base_folder_uuid,
        "user_id": value.user_id,
        "private_key": value.private_key,
    }


def user_fields(value: Any) -> dict[str, Any]:
    """Fields of the native User record."""
    return {
        "user_id": value.user_id,
        "email": value.email,
        "max_storage": value.max_storage,
        "storage_used": value.storage_used,
        "base_folder_uuid": value.base_folder_uuid,
    }


def file_fields(value: Any) -> dict[str, Any]:
    """Fields of the native DriveFile record."""
    return {
        "uuid": value.uuid,
        "name": value.name,
        "size": value.size,
        "mime": value.mime,
        "key": value.key,
        "chunks": value.chunks,
        "bucket": value.bucket,
        "region": value.region,
        "parent_uuid": value.parent_uuid,
        "version": value.version,
        "last_modified": value.last_modified,
        "creation": value.creation,
        "hash": None,
        "rm": "",
        "timestamp": value.timestamp,
    }


def folder_fields(value: Any) -> dict[str, Any]:
    """Fields of the native DriveFolder record."""
    return {
        "uuid": value.uuid,
        "name": value.name,
        "parent_uuid": value.parent_uuid,
    }


def translate_error(err: BaseException) -> FilenError:
    """Map a native SdkError (or an unexpected exception) to the library error."""
    if isinstance(err, FilenError):
        return err
    native = loaded()
    if native is not None and isinstance(err, native.SdkError):
        message = str(err)
        error_type = getattr(native.SdkError, "Auth", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenAuthError(message)
        error_type = getattr(native.SdkError, "TwoFactor", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenTwoFactorRequired(message)
        error_type = getattr(native.SdkError, "Blocked", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenAccountBlocked(message)
        error_type = getattr(native.SdkError, "Connection", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenConnectionError(message)
        error_type = getattr(native.SdkError, "RateLimit", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenRateLimitError(message)
        error_type = getattr(native.SdkError, "Quota", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenQuotaError(message)
        error_type = getattr(native.SdkError, "NotFound", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenNotFoundError(message)
        error_type = getattr(native.SdkError, "Conflict", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenConflictError(message)
        error_type = getattr(native.SdkError, "Integrity", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenIntegrityError(message)
        error_type = getattr(native.SdkError, "Crypto", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenCryptoError(message)
        error_type = getattr(native.SdkError, "Stream", None)
        if error_type is not None and isinstance(err, error_type):
            return FilenStreamError(message)
        return FilenError(message)
    return FilenError(f"{type(err).__name__}: {err}")
