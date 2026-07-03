---
description: Create a spec file and feature branch for the next DASHR AI step
argument-hint: "Step number and feature name e.g. 2 card-upload"
allowed-tools: Read, Write, Glob, Bash(git:*)
---

You are a senior developer spinning up a new feature for
DASHR AI. Always follow the rules in CLAUDE.md.

User input: $ARGUMENTS

## Step 1 — Check working directory is clean
Run `git status` and check for uncommitted, unstaged, or
untracked files. If any exist, stop immediately and tell
the user to commit or stash changes before proceeding.
DO NOT CONTINUE until the working directory is clean.

## Step 2 — Parse the arguments
From $ARGUMENTS extract:

1. `step_number` — zero-padded to 2 digits: 2 → 02, 11 → 11

2. `feature_title` — human readable title in Title Case
   - Example: "Card Upload" or "Company Enrichment"

3. `feature_slug` — git and file safe slug
   - Lowercase, kebab-case
   - Only a-z, 0-9 and -
   - Maximum 40 characters
   - Example: card-upload, company-enrichment

4. `branch_name` — format: `feature/<feature_slug>`
   - Example: `feature/card-upload`

If you cannot infer these from $ARGUMENTS, ask the user
to clarify before proceeding.

## Step 3 — Check branch name is not taken
Run `git branch` to list existing branches.
If `branch_name` is already taken, append a number:
`feature/card-upload-01`, `feature/card-upload-02` etc.

## Step 4 — Switch to main and pull latest
Run:
```
git checkout main
git pull origin main
```

## Step 5 — Create and switch to the feature branch
Run:
```
git checkout -b <branch_name>
```

## Step 6 — Research the codebase
Read these files before writing the spec:
- `CLAUDE.md` — architecture, tech stack, conventions
- `apps/api/app/` — existing routers, services, and models relevant to this feature
- `apps/web/app/` — existing pages/components relevant to this feature
- All files in `.claude/specs/` — avoid duplicating existing specs

Check `.claude/specs/` to confirm the requested step is not already
specified. If it is, warn the user and stop.

## Step 7 — Write the spec
Generate a spec document with this exact structure:

---
# Spec: <feature_title>

## Overview
One paragraph describing what this feature does and why
it exists at this stage of the DASHR AI roadmap (card
capture → extraction → enrichment → scoring → review/export).

## Depends on
Which previous steps this feature requires to be complete.

## API endpoints (apps/api)
Every new or changed endpoint:
- `METHOD /path` — description — access level (public/org-authenticated) — request/response shape

If no new endpoints: state "No new endpoints".

## Frontend surface (apps/web)
- **New pages/components**: path and purpose
- **Modified pages/components**: what changes

If no frontend changes: state "No frontend changes".

## Database changes
Any new tables, columns, or constraints needed — always
specify the `org_id` scoping for any new table.
Always verify against existing SQLAlchemy models before
writing this. If none: state "No database changes".

## Background jobs
Any new or changed Celery tasks, and what triggers them.
If none: state "No background job changes".

## Files to change
Every file that will be modified.

## Files to create
Every new file that will be created.

## New dependencies
Any new pip or npm packages. If none: state "No new dependencies".

## Rules for implementation
Specific constraints Claude must follow. Always include:
- Every query on an org-scoped table filters by `org_id`
- No raw SQL string interpolation — SQLAlchemy query builder or bound params only
- Business logic lives in `services/`, not in routers
- Bulk/long-running work is a Celery task, not synchronous in a request handler
- API contracts are Pydantic models — TS types are generated, not hand-duplicated

## Definition of done
A specific testable checklist. Each item must be
something that can be verified by running the app or its tests.
---

## Step 8 — Save the spec
Save to: `.claude/specs/<step_number>-<feature_slug>.md`

## Step 9 — Report to the user
Print a short summary in this exact format:
```
Branch:    <branch_name>
Spec file: .claude/specs/<step_number>-<feature_slug>.md
Title:     <feature_title>
```

Then tell the user:
"Review the spec at `.claude/specs/<step_number>-<feature_slug>.md`
then enter Plan Mode with Shift+Tab twice to begin implementation."
Do not print the full spec in chat unless explicitly asked.
