from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    SUCCESS = 0
    UNEXPECTED = 1
    INVALID_INPUT = 2
    CONFIGURATION = 3
    PROVIDER = 4
    NOT_FOUND = 5


class FwrError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        exit_code: ExitCode,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details


class InputError(FwrError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, ExitCode.INVALID_INPUT, details)


class DataIntegrityError(FwrError):
    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__("player_identity_conflict", message, ExitCode.INVALID_INPUT, details)


class ConfigurationError(FwrError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, ExitCode.CONFIGURATION, details)


class ProviderError(FwrError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("provider_error", message, ExitCode.PROVIDER, details)


class NotFoundError(FwrError):
    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        code: str = "not_found",
    ) -> None:
        super().__init__(code, message, ExitCode.NOT_FOUND, details)


class DatabaseBusyError(FwrError):
    def __init__(self, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "database_busy",
            "The local database is busy; retry after the other Fantasy War Room process finishes",
            ExitCode.UNEXPECTED,
            details,
        )
