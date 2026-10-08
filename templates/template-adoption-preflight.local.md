# Template Adoption Preflight

Local-only. Copy this file to
`docs/approval/template-adoption-preflight.local.md` before the first
implementation after adopting or refreshing this template. Do not commit the
copied file unless the project owner explicitly approves tracking this specific
artifact.

Use this gate to prove the template is correctly adapted to the project before
production-impacting work starts.

## Snapshot

- Date:
- Branch or workspace state:
- Template source or commit:
- Project goal reviewed: `docs/GOAL.md`
- Reviewer:

## Required Checks

- [ ] The project `README.md` is user-facing and does not link to local `/docs`
      files.
- [ ] Local `docs/GOAL.md` states the durable overall project goal and does not
      contain unresolved template placeholder text.
- [ ] `docs/workflows/DEVELOPMENT.md` documents the real branch model and full
      local validation gate, including dependency, license, and supply-chain
      checks or not-applicable reasons when relevant.
- [ ] `docs/workflows/ARCHITECTURE.md` documents real folder ownership, module
      boundaries, naming rules, dependency direction, and abstraction boundaries.
- [ ] `docs/workflows/TESTING.md` documents real test layers, test placement,
      coverage expectations, test commands, documentation-validation semantics,
      and test not-applicable reasons.
- [ ] `docs/workflows/RELEASE.md` documents the real CI or equivalent release
      gate, promotion path, packaging rules, smoke validation, and rollback path.
- [ ] `docs/workflows/AI_ASSISTANT.md` matches the project's risk profile,
      evidence rules, stop conditions, and review-subagent rules.
- [ ] `docs/references/README.md` exists and defines safe local reference-cache
      rules.
- [ ] `.gitignore` keeps `PLAN.md`, project-level assistant instruction files
      such as `AGENTS.md`, `AGENTS.override.md`, and `.clinerules`, local
      `/docs`, secrets, runtime state, generated state, and local approval
      evidence out of commits unless a specific artifact is explicitly approved
      for tracking.
- [ ] Ignored-file-aware discovery confirms no project-level assistant
      instruction files such as `AGENTS.md`, `AGENTS.override.md`, or
      `.clinerules` exist.
- [ ] Local artifact checks confirm copied `/docs` content and `PLAN.md` are not
      tracked unless explicitly approved.
- [ ] Validation commands are real project commands or marked not applicable with
      a reason.
- [ ] Any unresolved adoption risk has an owner, disposition, and follow-up path.
- [ ] The template adoption diff has been reviewed before committing tracked
      changes.

## Evidence

Commands, file references, or review notes used to verify the required checks:

- Pending.

## Decision

- Status: pending
- Blockers:
- Follow-ups:
- Approved tracked docs artifacts, if any:
