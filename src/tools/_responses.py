"""Shared error-response seam for MCP tools.

Two types of failures cross this seam:

- ``ToolInputError`` — bad input rejected before any database round-trip
  (range violation, table not on allowlist, malformed token, etc.).
- ``FabricQueryError`` — surfaced by ``FabricDatabase`` when the underlying
  pyodbc call fails.

``error_envelope`` is the single place that turns either into the on-wire
``ErrorResponse`` JSON the client receives. Centralising it means new tools
do not re-derive the envelope shape and the structured-log shape stays
consistent across the codebase.
"""

from __future__ import annotations

import logging

from src.database import FabricQueryError
from src.models import ErrorResponse

logger = logging.getLogger("fabric_mcp.tools")


class ToolInputError(Exception):
    """Structured exception for input rejected at the tool boundary.

    Mirrors the surface of ``FabricQueryError`` so a single ``except`` clause
    + one envelope helper handles both. ``details`` is optional and surfaces
    user-facing remediation hints (e.g. the actual allowlist contents).
    """

    def __init__(self, *, code: str, message: str, details: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def error_envelope(exc: ToolInputError | FabricQueryError, *, tool: str) -> str:
    """Convert a tool/database error into the on-wire ErrorResponse JSON.

    Also emits a structured ``logger.warning`` / ``logger.error`` carrying
    the tool name and error code so the production log shape is uniform
    across every failure path.
    """
    if isinstance(exc, FabricQueryError):
        logger.error(
            "%s failed: %s",
            tool,
            exc.code,
            extra={"tool": tool, "error_code": exc.code, "sqlstate": exc.sqlstate},
        )
    else:
        logger.warning(
            "%s rejected input: %s",
            tool,
            exc.code,
            extra={"tool": tool, "error_code": exc.code},
        )
    return ErrorResponse(
        code=exc.code,
        message=exc.message,
        details=exc.details,
    ).model_dump_json()
