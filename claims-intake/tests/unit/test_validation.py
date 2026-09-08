"""Rule-engine tests written from the contract, before the rules exist.

Authority is `docs/api-contract.md` sections 4.1, 4.2 and 6.2, and the acceptance
criteria on WI-0142, WI-0151 and WI-0158. Each rule has one parametrized test with
named ids. Comparison rules cover both sides of the boundary and the boundary
itself.

`evaluate_notification` is the pure path: a notification and a policy, nothing
else. V-1, V-6, recording, and lookup-failure propagation go through
`submit_notification`, because those need the client or the repository.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from claims.models import (
    CLAIM_TYPE_VOCABULARY,
    ClaimType,
    NotificationRequest,
    Policy,
    RuleFailure,
)
from claims.policy_client import LookupFailureReason, PolicyLookupFailed, StubPolicyClient
from claims.repository import NotificationRepository
from claims.service import evaluate_notification, submit_notification


def notification(
    *,
    policy_number: str = "MOT-4471",
    loss_date: date = date(2026, 6, 15),
    claim_type: ClaimType = "collision",
    estimated_amount: Decimal = Decimal("1000.00"),
    description: str | None = "Rear ended at a junction.",
) -> NotificationRequest:
    """A well-formed notification. Each call returns a new instance."""
    return NotificationRequest(
        policy_number=policy_number,
        loss_date=loss_date,
        claim_type=claim_type,
        estimated_amount=estimated_amount,
        description=description,
    )


def policy(
    *,
    policy_number: str = "MOT-4471",
    product: str = "personal_auto_standard",
    effective_date: date = date(2026, 3, 1),
    expiry_date: date = date(2027, 2, 28),
    cancellation_date: date | None = None,
    limit: Decimal = Decimal("50000.00"),
    permitted_claim_types: tuple[ClaimType, ...] = CLAIM_TYPE_VOCABULARY,
) -> Policy:
    """A policy in force across the V-2/V-3 term used by the helpers above."""
    return Policy(
        policy_number=policy_number,
        product=product,
        effective_date=effective_date,
        expiry_date=expiry_date,
        cancellation_date=cancellation_date,
        limit=limit,
        permitted_claim_types=permitted_claim_types,
    )


# --- V-1 ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("policy_number", "expected_failure"),
    [
        pytest.param("MOT-4471", None, id="policy_exists"),
        pytest.param(
            "MOT-9999",
            RuleFailure(rule="V-1", code="POLICY_NOT_FOUND"),
            id="policy_absent",
        ),
        pytest.param(
            "mot-4471",
            RuleFailure(rule="V-1", code="POLICY_NOT_FOUND"),
            id="unnormalised_case_d1",
        ),
    ],
)
def test_v1_policy_must_exist_in_the_master(
    policy_client: StubPolicyClient,
    repository: NotificationRepository,
    policy_number: str,
    expected_failure: RuleFailure | None,
) -> None:
    """Section 4.2 V-1. Lookup is exact (determination D-1)."""
    outcome = submit_notification(
        notification(policy_number=policy_number), policy_client, repository
    )
    if expected_failure is None:
        assert outcome.accepted
        assert outcome.failure is None
        assert outcome.claim_reference is not None
        return
    assert outcome.accepted is False
    assert outcome.failure == expected_failure
    assert outcome.detail["policy_number"] == policy_number
    assert repository.find_matching(policy_number, date(2026, 6, 15), "collision") is None


# --- V-2 ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("loss_date", "expected_failure"),
    [
        pytest.param(
            date(2026, 2, 28),
            RuleFailure(rule="V-2", code="LOSS_BEFORE_INCEPTION"),
            id="before_inception",
        ),
        pytest.param(date(2026, 3, 1), None, id="on_inception_wi0142_ac3"),
        pytest.param(date(2026, 3, 2), None, id="after_inception"),
    ],
)
def test_v2_loss_date_against_inception(
    loss_date: date, expected_failure: RuleFailure | None
) -> None:
    """Section 4.2 V-2 is inclusive. WI-0142 AC-3: a loss on inception is covered."""
    result = evaluate_notification(
        notification(loss_date=loss_date),
        policy(effective_date=date(2026, 3, 1)),
    )
    assert result == expected_failure


# --- V-3 ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("loss_date", "expected_failure"),
    [
        pytest.param(date(2027, 2, 27), None, id="before_expiry"),
        pytest.param(date(2027, 2, 28), None, id="on_expiry"),
        pytest.param(
            date(2027, 3, 1),
            RuleFailure(rule="V-3", code="LOSS_AFTER_EXPIRY"),
            id="after_expiry",
        ),
    ],
)
def test_v3_loss_date_against_expiry(
    loss_date: date, expected_failure: RuleFailure | None
) -> None:
    """Section 4.2 V-3 is inclusive. A loss on the expiry date is covered."""
    result = evaluate_notification(
        notification(loss_date=loss_date),
        policy(expiry_date=date(2027, 2, 28)),
    )
    assert result == expected_failure


# --- V-4 ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("estimated_amount", "expected_failure"),
    [
        pytest.param(Decimal("49999.99"), None, id="below_limit"),
        pytest.param(Decimal("50000.00"), None, id="on_limit"),
        pytest.param(
            Decimal("50000.01"),
            RuleFailure(rule="V-4", code="AMOUNT_EXCEEDS_LIMIT"),
            id="above_limit",
        ),
    ],
)
def test_v4_estimated_amount_against_limit(
    estimated_amount: Decimal, expected_failure: RuleFailure | None
) -> None:
    """Section 4.2 V-4 is inclusive. An amount equal to the limit is within cover."""
    result = evaluate_notification(
        notification(estimated_amount=estimated_amount),
        policy(limit=Decimal("50000.00")),
    )
    assert result == expected_failure


# --- V-5 ----------------------------------------------------------------------

NAMED_PERILS: tuple[ClaimType, ...] = ("theft", "glass", "weather", "liability")


@pytest.mark.parametrize(
    ("claim_type", "expected_failure"),
    [
        pytest.param("theft", None, id="type_permitted"),
        pytest.param("glass", None, id="another_permitted_type"),
        pytest.param(
            "collision",
            RuleFailure(rule="V-5", code="TYPE_NOT_COVERED"),
            id="type_in_vocabulary_not_on_product",
        ),
    ],
)
def test_v5_claim_type_against_permitted_set(
    claim_type: ClaimType, expected_failure: RuleFailure | None
) -> None:
    """Section 4.2 V-5. Membership is the comparison; in-set and out-of-set both appear."""
    result = evaluate_notification(
        notification(claim_type=claim_type),
        policy(product="personal_auto_named_perils", permitted_claim_types=NAMED_PERILS),
    )
    assert result == expected_failure


# --- V-7 ----------------------------------------------------------------------

CANCELLATION = date(2026, 6, 15)


@pytest.mark.parametrize(
    ("loss_date", "cancellation_date", "expected_failure"),
    [
        pytest.param(
            date(2026, 6, 14),
            CANCELLATION,
            None,
            id="before_cancellation",
        ),
        pytest.param(
            date(2026, 6, 15),
            CANCELLATION,
            RuleFailure(rule="V-7", code="POLICY_CANCELLED"),
            id="on_cancellation_wi0158_ac2",
        ),
        pytest.param(
            date(2026, 6, 16),
            CANCELLATION,
            RuleFailure(rule="V-7", code="POLICY_CANCELLED"),
            id="after_cancellation",
        ),
        pytest.param(
            date(2026, 6, 15),
            None,
            None,
            id="cancellation_date_absent_wi0158_ac3",
        ),
    ],
)
def test_v7_loss_date_against_cancellation(
    loss_date: date,
    cancellation_date: date | None,
    expected_failure: RuleFailure | None,
) -> None:
    """Section 4.2 V-7 is strict. WI-0158 AC-2 and AC-3."""
    result = evaluate_notification(
        notification(loss_date=loss_date),
        policy(cancellation_date=cancellation_date),
    )
    assert result == expected_failure


# --- V-6 ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("second_policy_number", "second_loss_date", "second_claim_type", "expected_code"),
    [
        pytest.param(
            "MOT-4471",
            date(2026, 6, 15),
            "collision",
            "DUPLICATE_NOTIFICATION",
            id="all_three_fields_match",
        ),
        pytest.param(
            "MOT-4472",
            date(2026, 6, 15),
            "collision",
            None,
            id="policy_number_differs",
        ),
        pytest.param(
            "MOT-4471",
            date(2026, 6, 16),
            "collision",
            None,
            id="loss_date_differs",
        ),
        pytest.param(
            "MOT-4471",
            date(2026, 6, 15),
            "theft",
            None,
            id="claim_type_differs",
        ),
    ],
)
def test_v6_duplicate_is_the_three_field_match(
    policy_client: StubPolicyClient,
    repository: NotificationRepository,
    second_policy_number: str,
    second_loss_date: date,
    second_claim_type: ClaimType,
    expected_code: str | None,
) -> None:
    """Section 4.2 V-6 and WI-0151 AC-1. Match is policy_number, loss_date, claim_type."""
    first = submit_notification(notification(), policy_client, repository)
    assert first.accepted
    second = submit_notification(
        notification(
            policy_number=second_policy_number,
            loss_date=second_loss_date,
            claim_type=second_claim_type,
        ),
        policy_client,
        repository,
    )
    if expected_code is None:
        assert second.accepted
        assert second.failure is None
        return
    assert second.accepted is False
    assert second.failure == RuleFailure(rule="V-6", code="DUPLICATE_NOTIFICATION")
    assert second.detail["claim_reference"] == first.claim_reference
    assert second.detail["policy_number"] == "MOT-4471"
    assert second.detail["loss_date"] == date(2026, 6, 15)
    assert second.detail["claim_type"] == "collision"


def test_v6_a_rejected_notification_is_not_a_duplicate(
    policy_client: StubPolicyClient,
    repository: NotificationRepository,
) -> None:
    """WI-0151 AC-3. Nothing was recorded, so there is nothing to duplicate."""
    refused = submit_notification(
        notification(estimated_amount=Decimal("50000.01")),
        policy_client,
        repository,
    )
    assert refused.accepted is False
    assert refused.failure == RuleFailure(rule="V-4", code="AMOUNT_EXCEEDS_LIMIT")

    retry = submit_notification(
        notification(estimated_amount=Decimal("1000.00")),
        policy_client,
        repository,
    )
    assert retry.accepted
    assert retry.failure is None
    assert retry.claim_reference is not None


# --- Evaluation order (section 4.1) ------------------------------------------

def test_v7_is_reported_ahead_of_v3_when_both_fail() -> None:
    """WI-0158 AC-4 / section 4.1 stage 2 before stage 3."""
    result = evaluate_notification(
        notification(loss_date=date(2027, 3, 1)),
        policy(
            expiry_date=date(2027, 2, 28),
            cancellation_date=date(2026, 6, 15),
        ),
    )
    assert result == RuleFailure(rule="V-7", code="POLICY_CANCELLED")


def test_v2_is_reported_ahead_of_v4_when_both_fail() -> None:
    """Section 4.1 stage 3 before stage 4."""
    result = evaluate_notification(
        notification(loss_date=date(2026, 2, 28), estimated_amount=Decimal("50000.01")),
        policy(effective_date=date(2026, 3, 1), limit=Decimal("50000.00")),
    )
    assert result == RuleFailure(rule="V-2", code="LOSS_BEFORE_INCEPTION")


def test_v4_is_reported_ahead_of_v5_when_both_fail() -> None:
    """Section 4.1: within stage 4, ascending identifier order."""
    result = evaluate_notification(
        notification(claim_type="collision", estimated_amount=Decimal("50000.01")),
        policy(
            limit=Decimal("50000.00"),
            product="personal_auto_named_perils",
            permitted_claim_types=NAMED_PERILS,
        ),
    )
    assert result == RuleFailure(rule="V-4", code="AMOUNT_EXCEEDS_LIMIT")


def test_v1_short_circuits_so_inception_is_not_evaluated(
    policy_client: StubPolicyClient,
    repository: NotificationRepository,
) -> None:
    """WI-0142 AC-4 / section 4.1 stage 1. POLICY_NOT_FOUND, not LOSS_BEFORE_INCEPTION."""
    outcome = submit_notification(
        notification(policy_number="MOT-9999", loss_date=date(1999, 1, 1)),
        policy_client,
        repository,
    )
    assert outcome.accepted is False
    assert outcome.failure == RuleFailure(rule="V-1", code="POLICY_NOT_FOUND")


def test_v4_is_reported_ahead_of_v6_when_both_fail(
    policy_client: StubPolicyClient,
    repository: NotificationRepository,
) -> None:
    """Section 4.1 stage 4 before stage 5. V-6 is last and does not run if V-4 fails."""
    recorded = submit_notification(notification(), policy_client, repository)
    assert recorded.accepted
    outcome = submit_notification(
        notification(estimated_amount=Decimal("50000.01")),
        policy_client,
        repository,
    )
    assert outcome.accepted is False
    assert outcome.failure == RuleFailure(rule="V-4", code="AMOUNT_EXCEEDS_LIMIT")


# --- Dependency boundary ------------------------------------------------------

@pytest.mark.parametrize(
    "reason",
    [
        pytest.param("timeout", id="timeout"),
        pytest.param("unreachable", id="unreachable"),
        pytest.param("unparsable", id="unparsable"),
    ],
)
def test_policy_lookup_failed_propagates_with_reason_intact(
    repository: NotificationRepository,
    reason: LookupFailureReason,
) -> None:
    """Section 6.3. PolicyLookupFailed is not a rule outcome and is not caught."""
    client = StubPolicyClient(fail_with=reason)
    with pytest.raises(PolicyLookupFailed) as exc_info:
        submit_notification(notification(), client, repository)
    assert exc_info.value.reason == reason
    assert exc_info.value.policy_number == "MOT-4471"
    assert repository.find_matching("MOT-4471", date(2026, 6, 15), "collision") is None


def test_submit_records_only_when_every_rule_passed(
    policy_client: StubPolicyClient,
    repository: NotificationRepository,
) -> None:
    """Section 3. A refused notification is never recorded."""
    outcome = submit_notification(notification(), policy_client, repository)
    assert outcome.accepted
    matching = repository.find_matching("MOT-4471", date(2026, 6, 15), "collision")
    assert matching is not None
    assert matching.claim_reference == outcome.claim_reference
