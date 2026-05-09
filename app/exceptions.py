"""
exceptions.py – BIMPruef shared exception hierarchy

All application-level exceptions inherit from BIMPruefError so callers
can catch the base class when they do not care about the specific subtype,
and FastAPI exception handlers can be registered against it.
"""


class BIMPruefError(Exception):
    """Base class for all application-defined exceptions."""


class AuthError(BIMPruefError):
    """Raised when authentication or authorisation fails."""


class NotFoundError(BIMPruefError):
    """Raised when a requested resource does not exist."""


class ValidationError(BIMPruefError):
    """Raised when input data fails validation."""


class StorageError(BIMPruefError):
    """Raised when a file-system or storage operation fails."""


class ConflictError(BIMPruefError):
    """Raised when an operation conflicts with existing state (e.g. duplicate e-mail)."""
