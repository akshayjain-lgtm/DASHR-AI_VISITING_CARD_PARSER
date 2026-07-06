---
description: Writes and runs tests for a specific DASHR AI feature. Pass the spec name as argument e.g. /test-feature 05-enrichment-service
allowed-tools: Bash(pytest), Bash(cd apps/api && pytest*), Bash(cd apps/web && npx vitest*), Bash(cd 

apps/web && npx playwright*)
---

Run the full testing pipeline for the feature specified
in $ARGUMENTS.

If no argument is provided, stop immediately and say:
"Please provide a spec name. Usage: /test-feature
<spec-name> e.g. /test-feature 05-enrichment-service"

If `.claude/specs/$ARGUMENTS.md` does not exist, stop
immediately and say:
"Spec file not found at .claude/specs/$ARGUMENTS.md.
Please check the spec name and try again."

---

## Step 1: Write Tests

Invoke the **dashr-test-writer** subagent with the
following context:

- Spec file to base tests on:
  `.claude/specs/$ARGUMENTS.md`
- Source files to read for structure:
  - `apps/api/app/` (routers, services, models) for backend features
  - `apps/web/app/` and `apps/web/components/` for frontend features
- Output test file to create:
  - Backend: `apps/api/tests/test_$ARGUMENTS.py`
  - Frontend: `apps/web/__tests__/$ARGUMENTS.test.tsx` or `apps/web/e2e/$ARGUMENTS.spec.ts`
- Instruction: Write tests based on what the spec says
  the feature SHOULD do. Do NOT derive test logic from
  reading the implementation. Cover happy paths, edge
  cases, auth guards, tenant-isolation, validation errors,
  and DB side effects. Mock all external OCR/enrichment
  provider calls.

Wait for dashr-test-writer to fully complete and
confirm the test file has been written before
proceeding to Step 2.

---

## Step 2: Run Tests

Once dashr-test-writer has finished, invoke the
**dashr-test-runner** subagent with the following
context:

- Test file to execute (from Step 1)
- Spec file for context:
  `.claude/specs/$ARGUMENTS.md`
- Source files to analyze against when diagnosing
  failures: `apps/api/app/` and/or `apps/web/`
- Run command:
  - Backend: `cd apps/api && pytest tests/test_$ARGUMENTS.py -v`
  - Frontend unit: `cd apps/web && npx vitest run $ARGUMENTS.test.tsx`
  - Frontend e2e: `cd apps/web && npx playwright test e2e/$ARGUMENTS.spec.ts`
- Instruction: Run ONLY the specified test file. Do
  NOT run the full test suite. Analyze any failures by
  cross-referencing the test code, the spec, and the
  source files. Classify each failure as a bug or a
  missing feature. Treat any tenant-isolation test
  failure as high severity.

---

## Handoff Rules

- Do NOT start Step 2 until Step 1 is fully complete
- Do NOT attempt to fix any code regardless of what
  the test results show
- Do NOT run any tests beyond the file(s) written in Step 1
- If dashr-test-writer reports it could not write
  the test file, stop and report the reason — do NOT
  proceed to Step 2

---

## Final Output

After both subagents complete, produce a combined
summary:

### Testing Pipeline Report — $ARGUMENTS

**Step 1 — Tests Written**
- List each test written with a one-line description
  of which spec requirement it validates

**Step 2 — Test Results**
- Mirror the dashr-test-runner's structured report

**Verdict**
One of:
- ✅ Ready for code review — all tests pass
- ❌ Needs fixes — list the failing tests and their root causes
