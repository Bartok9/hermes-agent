"""Structured error model for the MemPalace Hermes plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MEMPALACE_CONFIG_INVALID = "MEMPALACE_CONFIG_INVALID"
MEMPALACE_QUERY_FAILED = "MEMPALACE_QUERY_FAILED"
MEMPALACE_TOOL_ERROR = "MEMPALACE_TOOL_ERROR"
MEMPALACE_BACKEND_ERROR = "MEMPALACE_BACKEND_ERROR"


@dataclass(slots=True)
class MemPalaceError(Exception):
    """Base structured exception for plugin failures."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class MemPalaceConfigError(MemPalaceError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(MEMPALACE_CONFIG_INVALID, message, details or {})


class MemPalaceBackendError(MemPalaceError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(MEMPALACE_BACKEND_ERROR, message, details or {})


class MemPalaceToolError(MemPalaceError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(MEMPALACE_TOOL_ERROR, message, details or {})
