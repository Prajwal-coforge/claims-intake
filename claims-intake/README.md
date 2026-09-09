# Claims Intake Service

The claims intake service accepts a first notice of loss from the claims portal,
validates it against the policy master and the rule table in `docs/api-contract.md`,
and either records the notification and issues a claim reference or refuses with a
specific reason.

It does not decide whether a claim will be paid. It decides whether a notification
is well formed and admissible.

## Where things are

| Path | What it holds |
| --- | --- |
| `docs/api-contract.md` | What the service accepts, returns, and refuses. The authority. |
| `docs/requirements-brief.md` | Work items and their acceptance criteria. |
| `docs/payload-triage.md` | Day 1 classification of the edge payloads. |
| `data/` | Synthetic policies and notification payloads. |
| `src/claims/` | The service. `api/routes.py` is the HTTP surface. |
| `tests/` | Unit tests call functions. Integration tests go through HTTP. |
| `Dockerfile` | Image that runs the service. |

## Confirm you are in the container

You are already inside a Linux container. Dependencies are installed. Do not install
anything. If a tool is missing, that is a defect in the image, not a step for you.

```
uname -sm     # Linux aarch64 (or similar)
pwd           # /workspaces/claims-intake
```

## Run the service

From this directory:

```
uv run uvicorn claims.api.routes:app --host 0.0.0.0 --port 8000
```

The only endpoint this contract defines is:

```
POST /notifications
Content-Type: application/json
```

A payload that passes every rule returns `201` with a `claim_reference`. A payload
that fails a rule or cannot be parsed returns the error envelope in contract
section 5, with the status in section 6.

Example, using a payload from `data/fnol_valid.json`:

```
curl -sS -X POST http://127.0.0.1:8000/notifications \
  -H 'Content-Type: application/json' \
  -d '{"policy_number":"MOT-4471","loss_date":"2026-04-02","claim_type":"collision","estimated_amount":"4200.00","description":"Rear ended at a junction."}'
```

## Run the tests

```
uv run pytest
uv run ruff check .
uv run mypy
```

Unit tests live under `tests/unit/`. HTTP tests live under `tests/integration/`.
They do not share state: a fixture that leaked a recorded notification into the
next test would make the suite order-dependent.

## Build the image

```
docker buildx build --platform linux/amd64 -t claims-intake:latest .
```

Then run it:

```
docker run --rm -p 8000:8000 claims-intake:latest
```

### Why `--platform linux/amd64`

The machine you are sitting at and the machine that will run this image are not
the same computer, and they may not even share an instruction set.

This Codespace (and many of our laptops) is `aarch64`: ARM. The place we deploy
— CI runners, a lab VM, most cloud instances — is `linux/amd64`: 64-bit x86.
Docker's default is to build for **the host**. On an ARM machine that produces
an ARM image. That image will not start on an amd64 server; the kernel will
refuse to execute it.

`--platform linux/amd64` names the **target**, not the host. It tells buildx
to emit an image the deployment architecture can run, even when we build it
on ARM. The flag is not a performance hint and it is not optional on this
project. Leave it off and you will only find out when the image reaches a
machine that is not the one you built on.

## Data

Everything in `data/` is synthetic and was authored for this program. It contains
no real client data and no named clients.
