"""HTTP surface for the claims intake service.

This layer does three things and no more: it parses the request, it calls the
service, and it maps the outcome to a status code. It holds no rule logic. A rule
that appears here is a rule the service layer cannot be tested for.

Day 4 lab. Implement against `docs/api-contract.md` sections 5 and 6.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

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
    "MALFORMED_REQUEST": 400,
    "POLICY_NOT_FOUND": 422,
    "LOSS_BEFORE_INCEPTION": 422,
    "LOSS_AFTER_EXPIRY": 422,
    "AMOUNT_EXCEEDS_LIMIT": 422,
    "TYPE_NOT_COVERED": 422,
    "DUPLICATE_NOTIFICATION": 409,
    "POLICY_CANCELLED": 422,
    "POLICY_MASTER_TIMEOUT": 504,
    "POLICY_MASTER_UNREACHABLE": 503,
    "POLICY_MASTER_UNPARSABLE": 502,
}

LOOKUP_CODE: dict[LookupFailureReason, ErrorCode] = {
    "timeout": "POLICY_MASTER_TIMEOUT",
    "unreachable": "POLICY_MASTER_UNREACHABLE",
    "unparsable": "POLICY_MASTER_UNPARSABLE",
}


def get_policy_client() -> PolicyClient:
    """The policy master this process reads. Tests override this dependency."""
    return _default_policy_client


def get_repository() -> NotificationRepository:
    """The in-memory store this process writes. Tests override this dependency."""
    return _default_repository


PolicyClientDep = Annotated[PolicyClient, Depends(get_policy_client)]
RepositoryDep = Annotated[NotificationRepository, Depends(get_repository)]


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


def _envelope(code: ErrorCode, message: str, detail: Mapping[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=CODE_STATUS[code],
        content={"code": code, "message": message, "detail": _detail_json(detail)},
    )


def _malformed_request_detail(error: ValidationError) -> dict[str, Any]:
    """Section 5.2: `field` is the one that could not be interpreted, `issue` why.

    Only the first violation is reported. Section 2.4 refuses the whole request
    on the first problem found; it does not accumulate every field at fault.
    """
    errors = error.errors()
    if not errors:
        return {"field": None, "issue": "uninterpretable"}
    err = errors[0]
    loc = err.get("loc", ())
    field = str(loc[0]) if loc else None
    err_type = str(err.get("type", ""))
    if err_type == "missing":
        issue = "required_field_absent"
    elif err_type == "extra_forbidden":
        issue = "unknown_field"
    else:
        issue = err_type
    return {"field": field, "issue": issue}


@app.post("/notifications")
async def post_notification(
    request: Request,
    policy_client: PolicyClientDep,
    repository: RepositoryDep,
) -> JSONResponse:
    raw = await request.body()
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return _envelope(
            "MALFORMED_REQUEST",
            "The request could not be interpreted.",
            {"field": None, "issue": "body_not_json"},
        )

    try:
        notification = NotificationRequest.model_validate(parsed)
    except ValidationError as error:
        return _envelope(
            "MALFORMED_REQUEST",
            "The request could not be interpreted.",
            _malformed_request_detail(error),
        )

    try:
        outcome = submit_notification(notification, policy_client, repository)
    except PolicyLookupFailed as error:
        code = LOOKUP_CODE[error.reason]
        return _envelope(
            code,
            "The policy master did not produce a usable answer.",
            {"dependency": "policy_master", "reason": error.reason},
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
    assert outcome.failure is not None
    code = outcome.failure.code
    assert code in CODE_STATUS
    return _envelope(code, "The notification was refused.", outcome.detail)
