# Tool comparison: Cursor vs the coding agent

The bounded task I actually did in Cursor, myself, was writing the README
platform paragraph: why `docker buildx build --platform linux/amd64` exists,
in words a new joiner could follow. The coding agent had already produced
`routes.py`, `test_routes.py`, and the Dockerfile on three worktrees. I used
Cursor to read those diffs, check them against contract section 6, and write
the README explanation without restating the command.

## What each tool made easy

The agent was fast at mechanical mapping. Given the section 6 table, it filled
`CODE_STATUS`, the three `PolicyLookupFailed` reasons, and a parametrized
integration test per rule without losing a row. Splitting that work across
worktrees so `routes.py` and `test_routes.py` never shared a file was also
natural for it: the file list was the prompt.

Cursor made the README task easier because the failure mode is voice, not
coverage. The rubric rejects a platform note that only restates the flag. I
needed to sit with the host-vs-target distinction (this Codespace is aarch64;
deployment is amd64; Docker defaults to the host) and write it so someone who
has never heard of QEMU still understands why their image would not start on
the server. That is editing until a stranger could follow it, which is slower
and better done in the editor than by regenerating a whole file.

## What each tool made awkward

The agent will happily write a README that sounds complete and still fails the
rubric, because “complete” for it is a list of commands. It also cannot tell
you whether merge is blocked without you watching the GitHub UI; on Day 3 we
had to observe `UNSTABLE` vs `MERGEABLE` ourselves.

Cursor is awkward when the work is a large mapping table. Clicking through
seven rule rows and three lookup reasons by hand is how a row gets inverted
(`timeout` as 503). The editor does not keep you honest against section 6; the
tests do, and generating those tests is the agent’s job.

## What I would take where

I would take **table-shaped implementation and tests** (status maps, one case
per contract row, worktree-split files that do not overlap) to the agent, with
the contract pasted into the prompt and a hard file list.

I would take **explanations a stranger has to follow, and review against the
contract** — README voice, “what is absent,” blocking comments that cite a
section — to Cursor. Those tasks fail by sounding right while missing the
reason, and the person who has to be accountable for the paragraph should
write it.
