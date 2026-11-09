"""Errors shared across layers: config problems at startup, API errors at request time."""

from __future__ import annotations


class ConfigError(ValueError):
    """Bad settings or device file; the message is a sentence a person can act on."""


class ApiError(Exception):
    """An error that reaches the UI as {"code", "message"} with an HTTP status."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        extra: dict[str, object] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}
        self.retry_after = retry_after
