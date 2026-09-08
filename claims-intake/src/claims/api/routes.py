"""HTTP surface for the claims intake service.

This layer does three things and no more: it parses the request, it calls the
service, and it maps the outcome to a status code. It holds no rule logic. A rule
that appears here is a rule the service layer cannot be tested for.

Day 4 lab. Implement against `docs/api-contract.md` sections 5 and 6.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from claims.models import ErrorCode, NotificationRequest
from claims.policy_client import (
    LookupFailureReason,
    PolicyClient,
    PolicyLookupFailed,
    StubPolicyClient,
)
from claims.repository import NotificationRepository
from claims.service import ValidationOutcome, submit_notification

app = FastAPI(title="Claims Intake Service")

# Process-wide defaults for a running service. Tests replace these via
# FastAPI dependency_overrides so each case gets a fresh store and, when
# needed, a client that raises PolicyLookupFailed.
_default_policy_client: PolicyClient = StubPolicyClient()
_default_repository = NotificationRepository()

CODE_STATUS: dict[ErrorCode, int] = {
    "MALFORMED_JSON": 400,
    "SCHEMA_INVALID": 400,
    "POLICY_NOT_FOUND": 422,
    "LOSS_BEFORE_INCEPTION": 422,
    "LOSS_AFTER_EXPIRY": 422,
    "AMOUNT_EXCEEDS_LIMIT": 422,
    "TYPE_NOT_COVERED": 422,
    "DUPLICATE_NOTIFICATION": 409,
    "POLICY_CANCELLED": 422,
    "POLICY_MASTER_TIMEOUT": 504,
    "POLICY_MASTER_UNAVAILABLE": 503,
    "POLICY_MASTER_INVALID_RESPONSE": 502,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "METHOD_NOT_ALLOWED": 405,
    "NOT_FOUND": 404,
    "INTERNAL_ERROR": 500,
}

LOOKUP_OUTCOME: dict[LookupFailureReason, tuple[ErrorCode, int, bool]] = {
    "timeout": ("POLICY_MASTER_TIMEOUT", 504, True),
    "unreachable": ("POLICY_MASTER_UNAVAILABLE", 503, True),
    "unparsable": ("POLICY_MASTER_INVALID_RESPONSE", 502, False),
}


def get_policy_client() -> PolicyClient:
    """The policy master this process reads. Tests override this dependency."""
    return _default_policy_client


def get_repository() -> NotificationRepository:
    """The in-memory store this process writes. Tests override this dependency."""
    return _default_repository


def _jsonable(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _detail_json(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _jsonable(value) for key, value in detail.items()}


def _envelope(
    code: ErrorCode,
    message: str,
    detail: Mapping[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=CODE_STATUS[code],
        content={"code": code, "message": message, "detail": _detail_json(detail)},
        headers=headers,
    )


def _violations_from(error: ValidationError) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for item in error.errors():
        loc = item.get("loc", ())
        field = ".".join(str(part) for part in loc) or "(root)"
        violations.append({"field": field, "problem": str(item.get("msg", "invalid"))})
    return violations


def _is_json_content_type(content_type: str | None) -> bool:
    if content_type is None:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    if exc.status_code == 405:
        return _envelope(
            "METHOD_NOT_ALLOWED",
            "Method not allowed on this path.",
            {"method": request.method, "allowed": ["POST"]},
            headers={"Allow": "POST"},
        )
    if exc.status_code == 404:
        return _envelope(
            "NOT_FOUND",
            "No resource exists at this path.",
            {},
        )
    return _envelope(
        "INTERNAL_ERROR",
        "An unexpected error occurred.",
        {"correlation_id": uuid.uuid4().hex},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    if isinstance(exc, StarletteHTTPException):
        return await http_exception_handler(request, exc)
    return _envelope(
        "INTERNAL_ERROR",
        "An unexpected error occurred.",
        {"correlation_id": uuid.uuid4().hex},
    )


@app.post("/notifications")
async def post_notification(
    request: Request,
    policy_client: PolicyClient = Depends(get_policy_client),
    repository: NotificationRepository = Depends(get_repository),
) -> JSONResponse:
    content_type = request.headers.get("content-type")
    if not _is_json_content_type(content_type):
        return _envelope(
            "UNSUPPORTED_MEDIA_TYPE",
            "Content-Type must be application/json.",
            {"received": content_type, "expected": "application/json"},
        )

    raw = await request.body()
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return _envelope(
            "MALFORMED_JSON",
            "The request body is not valid JSON.",
            {},
        )
    if not isinstance(parsed, dict):
        return _envelope(
            "MALFORMED_JSON",
            "The request body must be a JSON object.",
            {},
        )

    try:
        notification = NotificationRequest.model_validate(parsed)
    except ValidationError as error:
        violations = _violations_from(error)
        return _envelope(
            "SCHEMA_INVALID",
            "The request could not be interpreted.",
            {"violations": violations},
        )

    try:
        outcome = submit_notification(notification, policy_client, repository)
    except PolicyLookupFailed as error:
        code, _status, retryable = LOOKUP_OUTCOME[error.reason]
        return _envelope(
            code,
            "The policy master did not produce a usable answer.",
            {
                "dependency": "policy_master",
                "reason": error.reason,
                "retryable": retryable,
                "correlation_id": uuid.uuid4().hex,
            },
        )

    if outcome.accepted:
        return JSONResponse(
            status_code=201,
            content={
                "claim_reference": outcome.claim_reference,
                "status": "recorded",
            },
        )
    return _refusal(outcome)


def _refusal(outcome: ValidationOutcome) -> JSONResponse:
    if outcome.failure is None:
        return _envelope(
            "INTERNAL_ERROR",
            "An unexpected error occurred.",
            {"correlation_id": uuid.uuid4().hex},
        )
    code = outcome.failure.code
    if code not in CODE_STATUS:
        return _envelope(
            "INTERNAL_ERROR",
            "An unexpected error occurred.",
            {"correlation_id": uuid.uuid4().hex},
        )
    return _envelope(
        code,
        "The notification was refused.",
        outcome.detail,
    )
