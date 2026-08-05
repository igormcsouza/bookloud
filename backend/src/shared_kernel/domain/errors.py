"""Framework-agnostic domain errors.

Each subclass carries the exact HTTP status the interface layer should
return, so behavior can move out of ``src/main.py`` and into contexts'
domain/application layers without changing the API's error contract.
``src/main.py`` registers one global handler for ``DomainError`` that maps
it to ``(detail, status_code)`` — route handlers just raise, they don't need
their own try/except.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain/application errors."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    """The requested aggregate/entity does not exist."""

    status_code = 404


class ConflictError(DomainError):
    """The action is not legal given the aggregate's current state."""

    status_code = 409


class ValidationError(DomainError):
    """Input failed a domain invariant (blank required field, malformed
    value, etc). Distinct from ``ConflictError``: this is about the shape of
    the input itself, not the aggregate's current state."""

    status_code = 400
