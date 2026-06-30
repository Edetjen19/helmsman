# Helmsman, a 5-minute walkthrough script

Discipline: the dashboard shows **real results** from the last remediation run (the three PRs +
backlog), loaded from a committed snapshot. `SIMULATE` is the dev/test Devin client (a mock used by
the tests and for dry-runs), not the board's data source. Never invoke the real Devin API live on
camera. Show the human approval gate, not auto-merge.

## Real artifacts to have open in tabs
- Three real PRs on the fork, one per class (#8 merged, closing issue #5; #10/#9 green at the human gate):
  - apispec / dependency-upgrade (hero): https://github.com/Edetjen19/superset/pull/8
  - datetime ×27 / deprecation-migration (semantics-preserving): https://github.com/Edetjen19/superset/pull/10
  - FE-lint / lint-graduation: https://github.com/Edetjen19/superset/pull/9
- The labeled backlog the scanner opened: https://github.com/Edetjen19/superset/issues?q=label%3Adevin-remediate
- The dashboard: http://localhost:8000 (real-results board)

## Before recording
```bash
cp .env.example .env                 # docker compose needs .env
docker compose up -d                 # web auto-loads the real-results snapshot on startup
```
The board shows the real last-run results immediately (no seeding, no creds). "Load results" reloads
the snapshot if needed.

## Arc

**0:00–0:40, What / business frame.** Deprecation and EOL debt accumulates faster than humans burn
it down. Each fix is individually boring, collectively expensive, and never prioritized over feature
work. The lever is an agent that owns each fix end to end. Frame as dependency and tech-debt
burn-down, not security (this checkout is already well patched).

**0:40–1:10, Real proof first (it ships).** Open **PR #8** on the fork:
`chore(deps): raise apispec version cap to <7.0.0`. Show the green check-runs, including
`unit-tests (current)` **success** (not skipped), and the Devin session's `remediated: true` verdict.
Mention there are **three** real green PRs, one per class (#8, #10, #9). This is real, merged-ready
code on a real CI gate.

**1:10–2:40, The ops board (real results).** Switch to the dashboard. Everything on it is real,
loaded from the last run: **#8 merged** (closing issue #5) and **#10/#9 at awaiting_merge** with real
Devin session links and real PRs; the activity ledger shows the real events (including apispec's
`ci_red → self_heal ×2 → verified_green`); the backlog shows #3/#4 still **open** and #6 **deferred** (a
deliberate policy choice on the high-risk EOL bump, not an agent refusal). Cost reads **measured
0.0 ACU**, **1 merged**. Each gate row is a **"Review PR →"** link out to GitHub, where a human reviews
the diff and merges — the dashboard never merges code. This is observability of *real* remediation, not a replay.

**2:40–3:30, Why an agent, not a codemod (the real hero, deep).** Two co-heroes:
- **apispec #8:** a `sed` bumps the cap; only an agent recompiled and then found and fixed **5
  OpenAPI spec assertions across 5 unit + integration test files** that the bump broke (apispec 6.7+
  adds `additionalProperties: false`).
- **datetime ×27 #10:** the semantics-preserving migration. Show the actual token:
  `datetime.now(tz=timezone.utc).replace(tzinfo=None)`. A blind `sed` to an aware `datetime.now(
  timezone.utc)` would raise on the naive DB comparisons; the agent kept each site naive where it is
  compared to naive datetimes. That is the "why not a codemod" proof.

**3:30–4:20, How / architecture + the two traps.** Walk the reconciler loop (level-triggered
desired-vs-actual, dedupe + ACU budget, the `devin_client` real+SIMULATE behind one interface), then
the two correctness traps:
- **Change-aware false-green guard** ("how do you trust the green?"): the verifier classifies the
  PR's changed files and requires the job *relevant to the change* to have actually run, not skip
  (python change → `unit-tests (current)`; frontend-only → a frontend job). This is the deepest
  engineering piece; the FE PR #9 exercises it for real.
- **finished != success:** derive success from `structured_output.remediated` + `pull_requests`,
  never status alone.
- **Self-heal, stated honestly:** on apispec #8 real CI went red and the self-heal engaged (2
  attempts) before green (`data/m4.db`: `ci_red → self_heal → ci_red → self_heal → verified_green`);
  we then found and fixed an over-eager-heal bug (heal once per commit, only after Devin idles). On
  #9 and #10 real CI also went red first, but Devin self-corrected its own CI and the control plane
  correctly did **not** heal (`heal_attempts=0`), the fixed gating doing its job. So we do not claim
  our message caused green vs Devin self-correcting; either way each loop closed on real CI: red → green.

**4:20–5:00, What the control plane adds + what's next.** It is not the patch (Devin writes that);
it is **governance** (per-session ACU cap + global budget + dedupe so a re-delivered event never
double-spends), **trustable verification** (the change-aware false-green guard), the **human
approval gate** (squash-merge into the fork, never auto-merge), **structured-output observability**
(a machine-readable verdict per session), and **backlog-scale throughput** (the fleet, vs running
one session by hand). The self-heal is a safety net; that Devin did not need it on two of three is
itself a finding. Next: wire real scanners (CodeQL/Snyk/Dependabot), graduate auto-merge per class
once trust is earned, mature the knowledge flywheel, schedule autonomous nibbling, multi-repo.

## Discipline (keep it honest)
- The dashboard shows real last-run results; the three PRs are real. SIMULATE is the test/mock Devin
  client, not the board's data source.
- Show the approval gate, not auto-merge. Auto-merge is an earned, per-class maturity setting (and
  you cannot self-merge into `apache/superset` anyway; the loop closes on the fork).
- #6 is a deliberate **deferral** (a policy choice on the high-risk EOL bump), not an agent refusal.
- ACU is real `acus_consumed`. These three runs measured 0.0 ACU on this org, so present the cost
  panel as "measured, came back 0," not a curve through two points.
- Do not run the real Devin API live on camera.

## Re-running the real pipeline (off camera)
See README "The real pipeline". `scripts/run_remediation.py --issue N` drives one bounded session to
the gate and never merges; `scripts/probe_b.py` re-confirms the message-resume at ≤3 ACU.
