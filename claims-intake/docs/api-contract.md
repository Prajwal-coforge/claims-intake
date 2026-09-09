# Claims Intake Service: API Contract

Version 0.4. Owned by the claims intake team. Consumed by the claims portal team.

This document is the authority on what the service accepts, what it returns, and under what conditions it refuses. Where the code and this document disagree, the document is correct and the code is a defect.

Sections 1 through 3 are fixed. Do not edit them.

## 1. Purpose and scope

The claims intake service accepts a first notice of loss from the claims portal, validates it against the policy master and a table of business rules, and either records a notification and issues a claim reference or refuses the submission with a specific reason.

**In scope.** Accepting a notification, validating it, and recording it. Issuing a claim reference. Reporting the reason a notification was refused.

**Out of scope.** Adjusting, reserving, payment, and any decision about coverage beyond the rules in section 4. The service decides whether a notification is well formed and admissible. It does not decide whether the claim will be paid.

**The policy master is a dependency, not part of this service.** The service reads policy records from it and does not write to it. A policy that cannot be read is a condition this contract specifies, and it is specified separately from a policy that does not exist, because the two require different action from the caller.

**Compatibility.** Adding a field to a response is a compatible change and callers must ignore fields they do not recognize. Adding a new error code is a compatible change and callers must fall through to default handling for a code they do not recognize. Changing the meaning of an existing code, removing a field, or changing a status code for an existing condition is not compatible and does not happen without a version increment agreed with the portal team.

## 2. Request

### 2.1 Endpoint

```
POST /notifications
Content-Type: application/json
```

### 2.2 Body

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `policy_number` | string | yes | Identifier as held in the policy master. Not empty. |
| `loss_date` | string | yes | Calendar date, `YYYY-MM-DD`. |
| `claim_type` | string | yes | One of the values in 2.3. Not empty. |
| `estimated_amount` | decimal | yes | United States dollars, two decimal places. Greater than zero. |
| `description` | string | no | Free text. Absent and `null` are equivalent. |

The service rejects a body carrying a field not listed above. A misspelled field name is a defect in the caller's code, and accepting the payload with the field ignored would record a notification built from data the caller did not send.

### 2.3 Claim type vocabulary

`collision`, `theft`, `glass`, `liability`, `weather`.

Which of these are admissible on a given notification depends on the product the policy is written on. The vocabulary is fixed by this contract. The permitted subset is a property of the policy record and is evaluated by rule `V-5`.

### 2.4 Well formed against acceptable

A request that cannot be interpreted is refused with status `400`. This means the body was not valid JSON, a required field was absent, a field carried a value of the wrong type, or a field was present that this contract does not define. The caller's code is wrong.

A request that was interpreted and whose content is not admissible is refused with status `422`. The caller's data is wrong, and a person needs to see the reason.

This split is stated here once and holds without exception everywhere else in this document.

## 3. Success response

A notification that passes every rule in section 4 is recorded and the service responds:

```
201 Created
Content-Type: application/json

{
  "claim_reference": "CLM-2026-000317",
  "status": "recorded"
}
```

**`claim_reference`** matches the pattern `CLM-YYYY-NNNNNN`, where `YYYY` is the calendar year in which the notification was recorded and `NNNNNN` is a zero padded sequence. A claim reference is unique across all recorded notifications and is never reissued. It is the value the claims handler quotes and the value every downstream system keys on.

**`status`** is `recorded` on every success response this contract defines. It exists because the portal displays it and because a future state that is not `recorded` is foreseeable. Callers must not treat it as constant.

A refused notification is never recorded and no claim reference is issued. There is no partial outcome: either a notification exists with a reference, or nothing was written.

## 4. Validation

A request is evaluated against section 2 before any rule in 4.2. A request that is not well formed is refused with code `MALFORMED_REQUEST` and status `400`, identified as `2.4`. No rule in 4.2 is evaluated.

The following readings of section 2 are binding:

- `policy_number` is matched to the policy master by exact string equality, including case. `mot-4471` and `MOT-4471` are different identifiers.
- `policy_number` must be a non-empty string. An empty string cannot be interpreted as the identifier this contract defines.
- `claim_type` must be one of the values listed in section 2.3. A value outside that vocabulary cannot be interpreted. Rule `V-5` is not reached.
- `estimated_amount` must carry exactly two decimal places. A scale other than two cannot be interpreted as the field this contract defines.
- `estimated_amount` must be greater than zero. A zero or negative amount cannot be interpreted as the field this contract defines.

### 4.1 Evaluation order

Rules are not evaluated in ascending identifier order. Cancellation is identifier `V-7` and is evaluated immediately after `V-2`, because a cancelled policy retains its original `expiry_date` in the policy master. Reporting `LOSS_AFTER_EXPIRY` for a cancelled policy would send the handler to the wrong system (WI-0158, AC-4).

The evaluation sequence is:

`V-1`, `V-2`, `V-7`, `V-3`, `V-4`, `V-5`, `V-6`.

`V-1` short circuits: if it fails, no rule that reads a policy field is evaluated (WI-0142, AC-4).

Evaluation proceeds through the sequence above and stops at the first rule that fails. The caller receives that rule's code and status only. No other rule that would also have failed is reported or implied, even where more than one condition in section 4.2 is false.

`V-6` is last so a duplicate of an already-recorded notification is reported as `DUPLICATE_NOTIFICATION` / 409, not as a later rule that would also have failed.

### 4.2 Rule table


| ID  | Condition                                                                             | Code                     | Status |
| --- | ------------------------------------------------------------------------------------- | ------------------------ | ------ |
| V-1 | `policy_number` exists in the policy master                                           | `POLICY_NOT_FOUND`       | 422    |
| V-2 | `loss_date` >= policy `effective_date`                                                | `LOSS_BEFORE_INCEPTION`  | 422    |
| V-3 | `loss_date` <= policy `expiry_date`                                                   | `LOSS_AFTER_EXPIRY`      | 422    |
| V-4 | `estimated_amount` <= policy `limit`                                                  | `AMOUNT_EXCEEDS_LIMIT`   | 422    |
| V-5 | `claim_type` permitted on the policy's product                                        | `TYPE_NOT_COVERED`       | 422    |
| V-6 | `policy_number` == `existing.policy_number` AND `loss_date` == `existing.loss_date` AND `claim_type` == `existing.claim_type` is false for every recorded notification | `DUPLICATE_NOTIFICATION` | 409    |
| V-7 | policy `cancellation_date` is null OR `loss_date` < policy `cancellation_date`        | `POLICY_CANCELLED`       | 422    |

A loss on the cancellation date is not covered. Cancellation takes effect at the start of `cancellation_date` (WI-0158, AC-2). Where `cancellation_date` is null the policy was not cancelled and `V-7` passes (WI-0158, AC-3).

A refused submission is never recorded. A later notification matching a refused submission is not a duplicate (WI-0151, AC-3).

Boundaries are inclusive as written. A loss on the inception date is covered (WI-0142, AC-3). An amount equal to the limit is within cover.

## 5. Error envelope

Refusal shape:

```
{
  "code": "",
  "message": "",
  "detail": {}
}
```

`code` is a stable promise. Changing the meaning of an existing code is not compatible.

`message` is a human-readable sentence. Callers must not parse it, match on it, or persist it as logic. The same `code` may carry a different `message` in a later revision.

`detail` is an object. Its keys differ by `code`. 

Keys a caller may rely on:


| `code`                      | Keys in `detail`                                        |
| --------------------------- | ------------------------------------------------------- |
| `MALFORMED_REQUEST`   | `field`, `issue`                                        |
| `POLICY_NOT_FOUND`          | `policy_number`                                         |
| `LOSS_BEFORE_INCEPTION`     | `loss_date`, `effective_date`                           |
| `LOSS_AFTER_EXPIRY`         | `loss_date`, `expiry_date`                              |
| `AMOUNT_EXCEEDS_LIMIT`      | `estimated_amount`, `limit`                             |
| `TYPE_NOT_COVERED`          | `claim_type`, `permitted_claim_types`                   |
| `DUPLICATE_NOTIFICATION`    | `claim_reference` of the existing recorded notification |
| `POLICY_CANCELLED`          | `loss_date`, `cancellation_date`                        |
| `POLICY_MASTER_TIMEOUT`     | `dependency`, `reason`                                  |
| `POLICY_MASTER_UNREACHABLE` | `dependency`, `reason`                                  |
| `POLICY_MASTER_UNPARSABLE`  | `dependency`, `reason`                                  |

For `MALFORMED_REQUEST`, `issue` is always a string. `field` is always present: the name of the field that could not be interpreted, or `null` when the body is not valid JSON and no field can be named. Callers must accept `null`.

### 5.1 Rule failure

```
422 Unprocessable Entity
Content-Type: application/json
{
  "code": "LOSS_BEFORE_INCEPTION",
  "message": "The loss date precedes the policy effective date.",
  "detail": {
    "loss_date": "2026-02-20",
    "effective_date": "2026-03-15"
  }
}
```

`detail` carries the two dates the rule compared. The portal can show a person which input was wrong.

### 5.2 Uninterpretable request

```
400 Bad Request
Content-Type: application/json

{
  "code": "MALFORMED_REQUEST",
  "message": "The request could not be interpreted.",
  "detail": {
    "field": "estimated_amount",
    "issue": "required_field_absent"
  }
}
```

`detail` names the field the parser could not interpret and why. When the body is not valid JSON, `field` is `null` and `issue` is `body_not_json`. It does not carry policy dates. No rule in 4.2 ran.

### 5.3 Policy master failure

```
504 Gateway Timeout
Content-Type: application/json
{
  "code": "POLICY_MASTER_TIMEOUT",
  "message": "The policy master did not respond in time.",
  "detail": {
    "dependency": "policy_master",
    "reason": "timeout"
  }
}
```

`detail` names the dependency and the reason it failed. It does not carry fields from the caller's payload. The request was not wrong; the same request may succeed later.

## 6. Status code mapping

Every failure this service can produce maps to exactly one `code` and exactly one status. Two codes never mean the same condition.


| Code                        | Status | Condition                                                                                                                                                                   |
| --------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MALFORMED_REQUEST`   | 400    | The request could not be interpreted. Section 2.4, including a `claim_type` outside the vocabulary, an empty `policy_number`, and an `estimated_amount` that does not have exactly two decimal places or is not greater than zero. |
| `POLICY_NOT_FOUND`          | 422    | The policy master answered and holds no policy with that `policy_number`. This is the caller's data.                                                                        |
| `LOSS_BEFORE_INCEPTION`     | 422    | Rule `V-2` failed.                                                                                                                                                          |
| `POLICY_CANCELLED`          | 422    | Rule `V-7` failed.                                                                                                                                                          |
| `LOSS_AFTER_EXPIRY`         | 422    | Rule `V-3` failed.                                                                                                                                                          |
| `AMOUNT_EXCEEDS_LIMIT`      | 422    | Rule `V-4` failed.                                                                                                                                                          |
| `TYPE_NOT_COVERED`          | 422    | Rule `V-5` failed.                                                                                                                                                          |
| `DUPLICATE_NOTIFICATION`    | 409    | Rule `V-6` failed.                                                                                                                                                          |
| `POLICY_MASTER_TIMEOUT`     | 504    | The policy master did not answer before the lookup timed out. The caller did nothing wrong.                                                                                 |
| `POLICY_MASTER_UNREACHABLE` | 503    | The policy master could not be reached. The caller did nothing wrong.                                                                                                       |
| `POLICY_MASTER_UNPARSABLE`  | 502    | The policy master returned a body this service could not parse. The caller did nothing wrong.                                                                               |


The policy master boundary is four conditions. No match is `POLICY_NOT_FOUND` and 422, because the master answered and the identifier is wrong. Timeout, unreachable, and unparsable are 5xx, because the payload is not at fault. They are three codes and three statuses because they are three different operational failures: no answer in time, no connection, and an unusable answer.