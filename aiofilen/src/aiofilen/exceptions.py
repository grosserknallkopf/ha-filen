"""Exceptions raised by aiofilen.

The hierarchy separates what a caller has to do about an error:
- FilenAuthError: ask the user to log in again (reauth).
- FilenAccountError: the account cannot be used (block); logging in again does not help.
- FilenConnectionError: temporary, try again later.
- everything else: a concrete problem with this operation.
"""


class FilenError(Exception):
    """Base class for all Filen errors."""


class FilenConnectionError(FilenError):
    """The API could not be reached, timed out or answered with a server error."""


class FilenRateLimitError(FilenConnectionError):
    """Too many requests (HTTP 429)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class FilenAuthError(FilenError):
    """Credentials or API key were rejected; a new login is needed."""


class FilenTwoFactorRequired(FilenAuthError):
    """The account has 2FA enabled and no (or a wrong) TOTP code was given."""


class FilenAccountError(FilenError):
    """The account cannot be used for this, a new login does not change that."""


class FilenAccountBlocked(FilenAccountError):
    """The account is blocked or locked after too many failed logins."""


class FilenQuotaError(FilenError):
    """Storage full or file larger than the account allows."""


class FilenNotFoundError(FilenError):
    """The requested file or folder does not exist."""


class FilenConflictError(FilenError):
    """A file or folder with that name already exists."""


class FilenIntegrityError(FilenError):
    """Downloaded data does not match the stored hash or auth tag."""


class FilenStreamError(FilenError):
    """The caller's source stream failed during an upload (see __cause__)."""


class FilenCryptoError(FilenError):
    """Data could not be decrypted with any available key, or has an unsupported format."""