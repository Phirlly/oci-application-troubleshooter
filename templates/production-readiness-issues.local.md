# Production Readiness Issues

Local-only. Do not commit unless explicitly approved for tracking. Copy this
file to `docs/approval/production-readiness-issues.local.md` only when a
production-readiness review or multi-issue remediation effort needs durable local
tracking outside `PLAN.md`.

Do not include secrets, credentials, customer data, live inventory,
infrastructure state, logs, runtime state, tokens, signing secrets, private
keys, response URLs, or raw approval evidence.

This ledger tracks verified production-readiness issues and candidate signals.
`PLAN.md` remains the active current-activity plan for one issue or task.
This template owns the ledger's status values, gate values, lifecycle fields,
evidence fields, and disposition rules.

## Status Values

- `candidate`: signal only; not a verified finding.
- `verified`: verified from current files, docs, tests, references, or
  authoritative sources, not yet active.
- `active`: the one issue currently represented by `PLAN.md`.
- `done`: fixed, reviewed, validated, and recorded with resolution evidence.
- `blocked`: cannot proceed without external input or environment change.
- `superseded`: replaced by another issue or implementation path.
- `invalid`: re-verification showed the signal is not an issue.

## Lifecycle Rules

Update the ledger whenever an issue status or disposition changes. When a
candidate is verified, move it from `Candidate Signals` into
`Active / Verified Issues` with evidence and `Status: verified`. When an issue
becomes the current `PLAN.md` activity, set `Status: active` and keep exactly
one active issue. When an issue reaches `done`, `blocked`, `superseded`, or
`invalid`, move it to `Closed / Dispositioned Issues` with disposition evidence
and validation or blocker notes.

## Gate Values

- `blocks-prod`: must be resolved before production promotion.
- `blocks-uat`: must be resolved before UAT or pre-production validation.
- `hardening`: improves production posture but does not block promotion alone.
- `follow-up`: track after the current production-readiness pass.

## Snapshot

- Date:
- Branch or workspace state:
- Local project goal reviewed: `docs/GOAL.md`
- Active plan:
- Review scope:
- Workflow rule: reverify every issue from current files, tests, docs,
  reference notes, local SDK/provider docs, or authoritative sources before
  implementation.

## Index

Add rows only for real candidate, verified, active, or dispositioned issues.
Keep the table empty until project evidence exists.

| ID | Status | Gate | Title | Verified | Disposition |
| --- | --- | --- | --- | --- | --- |

## Active / Verified Issues

Leave this section empty until an issue is verified from current project
evidence. When recording a real issue, use this shape and replace every
placeholder with project-specific evidence:

```text
### <issue-id>: <verified issue title>

Status: <verified|active>

Gate: <blocks-prod|blocks-uat|hardening|follow-up>

Verified: <YYYY-MM-DD>

Evidence:

- <current file, doc, test, command handle, reference note, or authoritative
  source proving the issue>

Impact: <one sentence describing user, operator, data, security, reliability, or
production risk>

Done criteria:

- <observable criterion>
- <behavior or validation criterion>
- <review or release criterion>

Active plan: <none, or local PLAN.md when this is the one active issue>

Disposition: pending until moved to `Closed / Dispositioned Issues`

Validation: <pending, command evidence, review evidence, or skipped validation
with reason>

Follow-ups: <none, or approved follow-up handle>
```

## Candidate Signals

Candidates are not verified findings. Do not assign implementation steps,
severity, or acceptance criteria until re-verification confirms the issue from
current files, tests, docs, reference notes, local SDK/provider docs, or
authoritative sources.

| Candidate | Last observed | Signal | Reverify from | Disposition |
| --- | --- | --- | --- | --- |

## Closed / Dispositioned Issues

Move issues here only after they reach `done`, `blocked`, `superseded`, or
`invalid`. Each entry must include the disposition evidence, validation or
blocker note, and any approved follow-up handle. Do not use this section as a
backlog.

Leave this section empty until a real issue is fixed, blocked, superseded, or
invalid. When closing a real issue, use this shape and replace every placeholder
with project-specific evidence:

```text
### <issue-id>: <dispositioned issue title>

Status: <done|blocked|superseded|invalid>

Gate: <blocks-prod|blocks-uat|hardening|follow-up>

Verified: <YYYY-MM-DD>

Evidence:

- <short evidence summary>

Disposition: <fixed, blocked, superseded, or invalid with a short evidence
handle>

Validation: <targeted validation, full gate, review, blocker evidence,
invalid-signal evidence, or skipped validation with reason>
```
