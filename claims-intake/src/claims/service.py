"""Rule evaluation and notification submission.

This module owns the decision. It does not know it was reached over HTTP, which
is why it can be tested by calling a function with a typed object and asserting on
the result with no server running. It does not know where notifications are
stored either. It knows the rules.

`evaluate_policy_exists` ships written. It is the pattern every other rule
follows: take the notification and whatever it needs, decide, and return a
refusal or nothing. Nothing prints, nothing raises for an ordinary refusal, and
nothing reaches for a status code, because a status code is a fact about HTTP
and this module does not know about HTTP.

`evaluate_notification` takes a notification and a policy and returns
`RuleFailure | None`. It performs no I/O. `submit_notification` resolves the
policy, evaluates, checks for a duplicate, and records only if every rule
passed.
"""

from __future__ import annotations

import json

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from claims.models import NotificationRequest, Policy, RuleFailure
from claims.policy_client import PolicyClient, PolicyNotFound
from claims.repository import NotificationRepository

PolicyRule = Callable[[NotificationRequest, Policy], RuleFailure | None]


@dataclass(frozen=True)
class ValidationOutcome:
    """The result of submitting a notification.

    `accepted` is the only thing a caller has to branch on. When it is true,
    `claim_reference` is the reference that was issued. When it is false,
    `failure` names the rule and the contract code, and `detail` carries the
    values the decision was made on.

    There is no status code here. Contract section 6 maps a code to a status, and
    that mapping is applied at the HTTP boundary.
    """

    accepted: bool
    claim_reference: str | None = None
    failure: RuleFailure | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, claim_reference: str | None = None) -> ValidationOutcome:
        return cls(accepted=True, claim_reference=claim_reference)

    @classmethod
    def refused(cls, failure: RuleFailure, **detail: Any) -> ValidationOutcome:
        return cls(accepted=False, failure=failure, detail=detail)


def evaluate_policy_exists(
    notification: NotificationRequest,
    policy_client: PolicyClient,
) -> ValidationOutcome:
    """V-1. The policy must exist in the policy master.

    This rule is different from the others in one way that matters: it is the only
    one that reaches outside the service, so it is the only one that can fail for
    a reason that is not the caller's fault. `PolicyNotFound` is caught here and
    turned into an ordinary refusal, because a policy that does not exist is a
    fact about the caller's data. `PolicyLookupFailed` is deliberately not caught,
    because the caller did nothing wrong and the HTTP layer has to be able to tell
    the two apart. Contract section 6 fixes what each becomes.

    V-1 short circuits. Every other rule compares against a field on a policy, and
    if there is no policy there is nothing to compare against. Reporting
    LOSS_BEFORE_INCEPTION for a policy number that does not exist is not merely
    unhelpful, it is a false statement about the client's data (WI-0142, AC-4).
    """
    try:
        policy_client.get_policy(notification.policy_number)
    except PolicyNotFound:
        return ValidationOutcome.refused(
            RuleFailure(rule="V-1", code="POLICY_NOT_FOUND"),
            policy_number=notification.policy_number,
        )
    return ValidationOutcome.ok()


def evaluate_loss_after_inception(
    notification: NotificationRequest,
    policy: Policy,
) -> RuleFailure | None:
    """V-2. The loss must not precede policy inception.

    The boundary is stated in contract section 4.2 and in WI-0142 AC-3. A loss on
    the inception date is covered.
    """
    if notification.loss_date >= policy.effective_date:
        return None
    return RuleFailure(rule="V-2", code="LOSS_BEFORE_INCEPTION")


def evaluate_loss_before_expiry(
    notification: NotificationRequest,
    policy: Policy,
) -> RuleFailure | None:
    """V-3. The loss must not fall after the policy expiry date."""
    if notification.loss_date <= policy.expiry_date:
        return None
    return RuleFailure(rule="V-3", code="LOSS_AFTER_EXPIRY")


def evaluate_amount_within_limit(
    notification: NotificationRequest,
    policy: Policy,
) -> RuleFailure | None:
    """V-4. The estimated amount must not exceed the policy limit.

    An amount equal to the limit is within cover, per contract section 4.2.
    """
    if notification.estimated_amount <= policy.limit:
        return None
    return RuleFailure(rule="V-4", code="AMOUNT_EXCEEDS_LIMIT")


def evaluate_claim_type_covered(
    notification: NotificationRequest,
    policy: Policy,
) -> RuleFailure | None:
    """V-5. The claim type must be permitted on the policy's product.

    The claim type is already one of the five values in section 2.3, because a
    value outside the vocabulary never leaves stage 0 (determination D-2). This
    rule asks whether the product this policy is written on extends to that peril.
    """
    if notification.claim_type in policy.permitted_claim_types:
        return None
    return RuleFailure(rule="V-5", code="TYPE_NOT_COVERED")


def evaluate_policy_not_cancelled(
    notification: NotificationRequest,
    policy: Policy,
) -> RuleFailure | None:
    """V-7. Cover must not have been ended by cancellation before the loss.

    The comparison is strict: earlier than `cancellation_date` passes, equal
    fails, later fails (WI-0158 AC-2). A `None` cancellation date is the only
    representation of "not cancelled" and this rule does not apply to it
    (WI-0158 AC-3).
    """
    cancellation_date = policy.cancellation_date
    if cancellation_date is None or notification.loss_date < cancellation_date:
        return None
    return RuleFailure(rule="V-7", code="POLICY_CANCELLED")


def _detail_for(
    failure: RuleFailure,
    notification: NotificationRequest,
    policy: Policy,
) -> dict[str, Any]:
    """Operands named in contract section 6.2 for the refusing rule."""
    if failure.rule == "V-2":
        return {
            "policy_number": notification.policy_number,
            "loss_date": notification.loss_date,
            "effective_date": policy.effective_date,
        }
    if failure.rule == "V-3":
        return {
            "policy_number": notification.policy_number,
            "loss_date": notification.loss_date,
            "expiry_date": policy.expiry_date,
        }
    if failure.rule == "V-4":
        return {
            "policy_number": notification.policy_number,
            "estimated_amount": notification.estimated_amount,
            "limit": policy.limit,
        }
    if failure.rule == "V-5":
        return {
            "policy_number": notification.policy_number,
            "claim_type": notification.claim_type,
            "permitted_claim_types": policy.permitted_claim_types,
            "product": policy.product,
        }
    if failure.rule == "V-7":
        return {
            "policy_number": notification.policy_number,
            "loss_date": notification.loss_date,
            "cancellation_date": policy.cancellation_date,
        }
    return {}


# Contract section 4.1. These are the rules that are pure functions of a
# notification and a policy. They run in stage order, not identifier order:
# stage 2 (V-7), stage 3 (V-2, V-3), stage 4 (V-4, V-5).
#
# V-1 is not here because it is a lookup against the policy master, not a
# comparison on a policy field. V-6 is not here because it is a lookup against
# the repository. Putting V-6 in this table would force evaluate_notification to
# take a store, which would mix deciding with recording and would break the
# C3 interface: evaluate_notification(notification, policy) -> RuleFailure | None.
POLICY_RULES: tuple[PolicyRule, ...] = (
    evaluate_policy_not_cancelled,
    evaluate_loss_after_inception,
    evaluate_loss_before_expiry,
    evaluate_amount_within_limit,
    evaluate_claim_type_covered,
)


def evaluate_notification(
    notification: NotificationRequest,
    policy: Policy,
) -> RuleFailure | None:
    """Evaluate the policy-field rules and return the first failure, or None.

    A notification can violate several rules at once and the caller sees one
    reason, so the order this function evaluates in is a caller-visible behavior.
    It is fixed by contract section 4.1 and by nothing else. If you find yourself
    choosing an order here, the contract is incomplete and the fix belongs there.
    """
    for rule in POLICY_RULES:
        failure = rule(notification, policy)
        if failure is not None:
            return failure
    return None


def submit_notification(
    notification: NotificationRequest,
    policy_client: PolicyClient,
    repository: NotificationRepository,
) -> ValidationOutcome:
    """Validate, and record only if every rule passed.

    Nothing is written before the decision is made. A notification is either
    recorded with a claim reference or it does not exist, and there is no state in
    between for a later reader to interpret.

    `PolicyNotFound` is V-1. `PolicyLookupFailed` is not caught: it is not a rule
    outcome, and the HTTP layer must see its `reason` intact (section 6.3).
    """
    existence = evaluate_policy_exists(notification, policy_client)
    if not existence.accepted:
        return existence

    # V-1 already proved the master holds this policy. This second read supplies
    # the fields stages 2 through 4 compare against. A client that makes a network
    # call should cache; that is the client's concern, not the rules'.
    policy = Policy.from_record(policy_client.get_policy(notification.policy_number))

    failure = evaluate_notification(notification, policy)
    if failure is not None:
        return ValidationOutcome.refused(
            failure, **_detail_for(failure, notification, policy)
        )

    # Stage 5. Kept here so POLICY_RULES stays free of the repository.
    existing = repository.find_matching(
        notification.policy_number,
        notification.loss_date,
        notification.claim_type,
    )
    if existing is not None:
        return ValidationOutcome.refused(
            RuleFailure(rule="V-6", code="DUPLICATE_NOTIFICATION"),
            policy_number=notification.policy_number,
            loss_date=notification.loss_date,
            claim_type=notification.claim_type,
            claim_reference=existing.claim_reference,
        )

    recorded = repository.record(notification)
    return ValidationOutcome.ok(recorded.claim_reference)
