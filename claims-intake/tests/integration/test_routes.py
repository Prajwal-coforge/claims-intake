"""HTTP integration tests for POST /notifications.

These tests go through the ASGI app. They do not call submit_notification.
Each test installs a fresh policy client and repository so the suite cannot
depend on run order.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from claims.api.routes import app, get_policy_client, get_repository
from claims.policy_client import LookupFailureReason, StubPolicyClient
from claims.repository import NotificationRepository
from tests.payloads import payload

_CLAIM_REFERENCE = re.compile(r"^CLM-\d{4}-\d{6}$")

INVALID_CASES: tuple[tuple[str, int, str], ...] = (
    ("INVALID-01", 422, "POLICY_NOT_FOUND"),
    ("INVALID-02", 422, "LOSS_BEFORE_INCEPTION"),
    ("INVALID-03", 422, "LOSS_AFTER_EXPIRY"),
    ("INVALID-04", 422, "AMOUNT_EXCEEDS_LIMIT"),
    ("INVALID-05", 422, "TYPE_NOT_COVERED"),
    ("INVALID-07", 422, "POLICY_CANCELLED"),
)

LOOKUP_CASES: tuple[tuple[LookupFailureReason, int, str], ...] = (
    ("timeout", 504, "POLICY_MASTER_TIMEOUT"),
    ("unreachable", 503, "POLICY_MASTER_UNAVAILABLE"),
    ("unparsable", 502, "POLICY_MASTER_INVALID_RESPONSE"),
)


@pytest.fixture
def http() -> Generator[TestClient, None, None]:
    """A client bound to a fresh store and a working policy master."""
    repository = NotificationRepository()
    policy_client = StubPolicyClient()
    app.dependency_overrides[get_policy_client] = lambda: policy_client
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_valid_notification_returns_201_and_claim_reference(http: TestClient) -> None:
    response = http.post("/notifications", json=payload("VALID-01"))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "recorded"
    assert _CLAIM_REFERENCE.fullmatch(body["claim_reference"])


@pytest.mark.parametrize(
    ("payload_id", "status", "code"),
    [pytest.param(*row, id=row[0]) for row in INVALID_CASES],
)
def test_each_rule_refusal_returns_contract_status_and_code(
    http: TestClient, payload_id: str, status: int, code: str
) -> None:
    response = http.post("/notifications", json=payload(payload_id))
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["detail"], dict)
    if code == "POLICY_NOT_FOUND":
        assert body["detail"]["policy_number"] == payload(payload_id)["policy_number"]
    if code == "LOSS_BEFORE_INCEPTION":
        assert "loss_date" in body["detail"]
        assert "effective_date" in body["detail"]
    if code == "LOSS_AFTER_EXPIRY":
        assert "loss_date" in body["detail"]
        assert "expiry_date" in body["detail"]
    if code == "AMOUNT_EXCEEDS_LIMIT":
        assert "estimated_amount" in body["detail"]
        assert "limit" in body["detail"]
    if code == "TYPE_NOT_COVERED":
        assert "claim_type" in body["detail"]
        assert "permitted_claim_types" in body["detail"]
        assert "product" in body["detail"]
    if code == "POLICY_CANCELLED":
        assert "cancellation_date" in body["detail"]


def test_duplicate_notification_returns_409_with_existing_reference(
    http: TestClient,
) -> None:
    first = http.post("/notifications", json=payload("VALID-01"))
    assert first.status_code == 201
    existing = first.json()["claim_reference"]
    second = http.post("/notifications", json=payload("INVALID-06"))
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "DUPLICATE_NOTIFICATION"
    assert body["detail"]["claim_reference"] == existing
    assert body["detail"]["policy_number"] == "MOT-4471"
    assert body["detail"]["loss_date"] == "2026-04-02"
    assert body["detail"]["claim_type"] == "collision"


def test_missing_required_field_returns_400_not_a_rule_code(http: TestClient) -> None:
    response = http.post("/notifications", json=payload("EDGE-08"))
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "SCHEMA_INVALID"
    assert "violations" in body["detail"]
    fields = {item["field"] for item in body["detail"]["violations"]}
    assert "estimated_amount" in fields


def test_extra_field_is_rejected_not_ignored(http: TestClient) -> None:
    body: dict[str, Any] = payload("VALID-01")
    body["policy_holder_name"] = "not in the contract"
    response = http.post("/notifications", json=body)
    assert response.status_code == 400
    payload_body = response.json()
    assert payload_body["code"] == "SCHEMA_INVALID"
    assert payload_body["code"] != "recorded"


def test_malformed_json_returns_400(http: TestClient) -> None:
    response = http.post(
        "/notifications",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_JSON"
    assert response.json()["detail"] == {}


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [pytest.param(*row, id=row[0]) for row in LOOKUP_CASES],
)
def test_policy_lookup_failed_returns_distinct_5xx(
    reason: LookupFailureReason, status: int, code: str
) -> None:
    repository = NotificationRepository()
    policy_client = StubPolicyClient(fail_with=reason)
    app.dependency_overrides[get_policy_client] = lambda: policy_client
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as client:
        response = client.post("/notifications", json=payload("VALID-01"))
    app.dependency_overrides.clear()
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert body["detail"]["reason"] == reason
    assert body["detail"]["dependency"] == "policy_master"
    assert "correlation_id" in body["detail"]
    assert isinstance(body["detail"]["retryable"], bool)
    # A lookup failure is not an absent policy.
    assert body["code"] != "POLICY_NOT_FOUND"
    assert response.status_code >= 500


def test_policy_not_found_is_422_not_a_dependency_failure(http: TestClient) -> None:
    response = http.post("/notifications", json=payload("INVALID-01"))
    assert response.status_code == 422
    assert response.json()["code"] == "POLICY_NOT_FOUND"
    assert "reason" not in response.json()["detail"]
