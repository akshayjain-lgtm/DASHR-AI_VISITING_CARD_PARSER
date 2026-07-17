---
name: verify
description: Drive DASHR AI end-to-end in a real browser (Playwright) to verify a change actually works, not just that it typechecks/passes unit tests.
---

# DASHR AI — browser verification recipe

## Prerequisites
- Dev stack already running (use the `launch-website` skill if not): Next.js on `:3000`, FastAPI on `:8000`.
- The FastAPI dev server is often started **without `--reload`** — if you edited backend code, check `ps aux | grep uvicorn` and restart it manually, or your changes won't be live. The Next.js dev server has Fast Refresh and picks up frontend edits automatically.

## Getting a headless browser
Playwright's Chromium is already cached at `~/.cache/ms-playwright/` in this environment (confirm with `ls ~/.cache/ms-playwright/`). The repo itself has no Playwright dependency, so install it standalone in the scratchpad rather than touching `apps/web/package.json`:

```bash
mkdir -p <scratchpad>/pw-verify && cd <scratchpad>/pw-verify
npm init -y >/dev/null
npm install playwright@1.61.1 --no-save   # fast — reuses the cached browser, no re-download
```

Then write a plain Node script requiring `playwright` and run it with `node script.js`. Take `page.screenshot({ path, fullPage: true })` at each meaningful step — screenshots are your evidence, read them back with the Read tool afterward.

## Auth flow gotchas (relevant to any test that signs up/logs in)
- Signup form fields are matched by placeholder text: `"Akshay Jain"` (name), `"Thermax Limited"` (company name), `"9876543210"` (phone, no `+91` prefix — that's a separate fixed span), `"you@company.com"` (email), `"••••••••"` (password).
- OTP in dev is **always the static code `1234`** (`ConsoleOtpProvider`/`app/core/security.py::generate_otp_code`) — verify-otp field placeholder is `"1234"`.
- After verify-otp, the app redirects to `/dashboard` — `page.waitForURL('**/dashboard')` is the reliable success signal.
- Invite accept links are logged to the FastAPI process's stdout by `ConsoleInviteEmailProvider` (`[INVITE] ... : http://localhost:3000/login?invite=<token>`) — if the server was launched via `nohup ... > /tmp/uvicorn.log`, grep that file for the token rather than trying to intercept the network response (the invite API deliberately never returns the raw token).

## Multi-user flows
Use a separate `browser.newContext()` per user (admin vs. invited teammate, etc.) — contexts have independent cookie jars, so you can hold two authenticated sessions open simultaneously in one script and interleave actions between them (e.g. admin deactivates a user in context A, then reload context B to observe the session die).

## Known pre-existing gaps (don't re-report these as new bugs)
- No page in this app auto-redirects to `/login` on a 401 from a background fetch — every page just shows an inline error message. This is a deliberate/consistent (if debatable) pattern across `wallet`, `upload`, and `settings` pages, not a bug specific to one feature.
