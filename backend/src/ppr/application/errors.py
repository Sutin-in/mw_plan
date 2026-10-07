"""Errors raised by application services; ``code`` is stable for API mapping."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    """The request conflicts with the current state (e.g. plan year not DRAFT)."""


class InvalidInputError(ServiceError):
    """The request refers to unknown/inactive data or breaks an input rule."""


class AuthenticationFailedError(ServiceError):
    pass


class UpstreamUnavailableError(ServiceError):
    """HOSxP (or another upstream) could not be reached."""


class ForbiddenError(ServiceError):
    """The caller is authenticated and holds the permission, but not for this record."""
