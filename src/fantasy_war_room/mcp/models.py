from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class McpModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class McpError(McpModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class McpEnvelope(McpModel):
    schema_version: str = "1.0"
    status: Literal["ok", "error"]
    data: dict[str, Any] | None
    error: McpError | None
    provenance: dict[str, Any] | None = None
