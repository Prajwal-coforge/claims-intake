# Agent decision log — Day 3

Two changes produced while building the rule engine. Each names what was produced,
what was decided, and the contract or acceptance criterion that made the decision.

## Accepted: `evaluate_notification` takes only a notification and a policy

**Produced.** A function `evaluate_notification(notification, policy) -> RuleFailure | None`
that walks `POLICY_RULES` and returns the first failure. The Day 2 stub took a
policy client and a repository and returned `ValidationOutcome`.

**Decision.** Keep the two-argument form. Do not restore the stub signature.

**Reason.** The C3 interface, and the Day 3 acceptance criterion, require that
`evaluate_notification` perform no I/O, write nothing, and take only a notification
and a policy. A client or repository argument would make a lookup failure and a
duplicate check indistinguishable from a policy-field comparison, and section 6.3
would no longer be able to tell `PolicyNotFound` (V-1, 422) from
`PolicyLookupFailed` (5xx, reason intact). Contract section 4.1 already places V-1
in stage 1 and V-6 in stage 5, which are lookups; stages 2–4 are the only rules
that belong in this function.

## Rejected: a second copy of V-6 next to the one in `submit_notification`

**Produced.** An `evaluate_not_duplicate(notification, repository)` helper that
called `find_matching` and returned `RuleFailure | None`, which
`submit_notification` then called before calling `find_matching` again to fill
`detail.claim_reference` (WI-0151 AC-2).

**Decision.** Remove the helper. V-6 stays as a single `find_matching` call in
`submit_notification` after `evaluate_notification` returns.

**Reason.** Two implementations of the same three-field match (WI-0151 AC-1) can
drift: a later change to one copy would make `evaluate_not_duplicate` and
`submit_notification` disagree about whether a notification is a duplicate, and
the caller would receive `DUPLICATE_NOTIFICATION` or a recorded claim depending
on which path ran. Section 4.2 states the match once, against recorded
notifications only. One query in `submit_notification` is that statement. The
second lookup also made `claim_reference` in `detail` depend on a separate read
that could, in a later repository, return `None` after the first read had already
decided it was a duplicate — which would omit the reference WI-0151 AC-2 requires.

## Gate observation (step 8)

Pull request: https://github.com/Prajwal-coforge/claims-intake/pull/2

Commit `087919f` added an unused `json` import so ruff would fail. GitHub Actions run
https://github.com/Prajwal-coforge/claims-intake/actions/runs/34242513453 concluded
**failure**. The `ruff` step failed the job. `mypy` and `pytest` were skipped, which
is the job failing rather than continuing.

What the merge box showed after that failure:

- Check `checks` was marked failed.
- `mergeStateStatus` was `UNSTABLE`.
- `mergeable` remained `MERGEABLE`.
- `GET /repos/Prajwal-coforge/claims-intake/branches/main/protection` returned 404
  `Branch not protected`.

The failing check was **marked**. It did **not** block the merge. That is a
repository-configuration finding, not a defect in `checks.yaml`. No branch
protection requires the check, so a red `checks` job does not prevent merging.
This is reported rather than worked around.
