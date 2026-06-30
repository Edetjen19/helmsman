# Design: Helmsman, a Devin × Superset remediation control plane

A control plane that turns a backlog of real Apache Superset tech-debt issues into verified pull
requests, using Devin v3 sessions as the worker primitive, with a closed self-healing loop against
the fork's real CI and an ops console.

- **Trigger (event-driven):** a policy/deprecation scanner opens labeled GitHub issues on the fork.
  An HMAC-verified `issues.labeled` webhook can enqueue them, and a level-triggered resync reconciles
  the labeled backlog against open PRs so lost webhooks are self-correcting. A scheduled scan is the
  "scan results" trigger.
- **Devin as the core primitive:** per-class playbooks, structured-output verdicts, tags for fleet
  correlation, a parallel fleet bounded by an ACU budget, and the message endpoint for self-heal.
- **The hero loop:** a PR opens, the verifier polls the fork's real CI, and if red the failing log is
  fed back into the same session to fix forward; on green a human approval gate squash-merges (never
  an agent auto-merging its own code).
- **Observability:** backlog burn-down, a live issue → session → PR fleet board, MTTR, throughput,
  outcomes, and real ACU per fix (`acus_consumed`).

Built on **Devin v3** (`cog_` service-user auth, `/v3/organizations/{org_id}`).

---

## 2. The issue portfolio

Real and verified against an Apache Superset checkout near tag 6.1.0. The scanner opens one labeled
issue per finding (`devin-remediate` + a class label).

| # | Issue | Evidence | Class | Fix approach |
|---|-------|----------|-------|--------------|
| 1 | **apispec cap blocks upgrade** | `requirements/base.in` caps `apispec<6.7.0`, resolving 6.6.1 | dependency-upgrade | Raise the cap, recompile, then fix the OpenAPI spec assertions that break (the part a script can't do). |
| 2 | **`datetime.utcnow()` deprecated, 27 sites** | 27 occurrences, 21 in `commands/report/execute.py`, plus `utils/cache.py`, `utils/dates.py`, `daos/log.py`, the prune commands | deprecation-migration | → `datetime.now(timezone.utc)` **preserving naive-vs-aware semantics per site** (some values are compared to naive datetimes; a blind sed introduces aware/naive comparison bugs). |
| 3 | **`datetime.utcfromtimestamp()`, 2 sites** | `models/helpers.py`, `daos/query.py` | deprecation-migration | → `datetime.fromtimestamp(x, tz=timezone.utc)` preserving epoch semantics. |
| 4 | **SQLAlchemy 1.4 legacy `Query.get()`, 7 production sites** | e.g. `security/manager.py`, `cli/export_example.py`, `commands/dataset/duplicate.py` (the 2 hits under `migrations/versions/` are excluded, out of lint scope) | deprecation-migration | → `session.get(Model, id)`. |
| 5 | **EOL pins** | `pandas==2.1.4`, `Flask==2.3.3`, `numpy==1.26.4` | dependency-upgrade (higher risk) | Flag; treat pandas/numpy as roadmap (real breakage risk, do not auto-merge blind). |
| 6 | **Frontend lint graduation** | `import/no-duplicates` warn→error, oxlint react plugin pinned 17.0.2 vs React 18 | lint-graduation | oxlint autofix + dedup. |

The Python merge gate (`unit-tests-required`) runs `pytest ./tests/common ./tests/unit_tests` on
in-memory SQLite, so the real regression signal runs inside Devin's own sandbox. `superset/sql/` and
`superset/semantic_layers/` enforce 100% coverage; avoid touching those.

---

## 3. PR conventions the fork enforces

- **Title must match Conventional Commits** (`pr-lint.yml`): `^(build|chore|ci|docs|feat|fix|perf|refactor|style|test|other)(\(.+\))?(\!)?:\s.+`. Squash-merge only, so the title becomes the commit message.
- **`pre-commit` must pass** (`pre-commit (current)`), running on changed files. It **fails if the tree is dirty after autofixes**, so the agent must commit the autofixed result.
- **No DCO/sign-off gate** in CI (`required_signatures: false`).
- **New source files need the ASF Apache-2.0 license header** (RAT check). None of the target fixes add files.
- **Banned-API traps** (fail pre-commit if reintroduced): `import json`/`simplejson` are banned in favor of `superset.utils.json`; `make_url` is blacklisted. The playbooks warn about these.

---

## 4.2. Empirical probes

**Probe A, does Superset CI run on a same-fork PR?** Confirmed. A throwaway PR registered ~28
check-runs; the change-detector tripped `python=true`; gating contexts (`pre-commit (current)`,
`lint-check`, `test-sqlite`, `test-postgres (current)`, `test-mysql`, `dependency-review`) started;
and **0 checks were `action_required`** (no fork-approval gate). The self-heal loop has a real CI
signal to react to.

The subtlety: `superset-python-unittest.yml` runs `unit-tests` only if the change-detector sets
`python=true`. `unit-tests-required` is an **always-running anchor that passes when unit-tests
succeeded OR was skipped**, so a PR that doesn't trip the detector goes **falsely green**. The
verifier must confirm the job *relevant to the change* actually ran (see §6.2 / the change-aware
false-green guard).

**Probe B, can you message a session after it goes idle?** Confirmed (~0 ACU). A throwaway session
reached `running / waiting_for_user`, `POST .../messages` was accepted, and `status` flipped back to
`running` (working): the session resumed and acted on the message. The message endpoint takes the
bare-hex `session_id` as-is. Fallback for a non-resumable `exit`/`error` session: open a new bounded
session scoped to "fix CI on PR #N."

---

## 5. Architecture

```
schedule ─▶ scanner ─(opens labeled issues)─▶ GitHub fork
                                                  │ issues.labeled webhook (HMAC)  ┌─ resync (level-triggered)
                                                  ▼                                │
                                            ingest (FastAPI)  ──enqueue──▶  store (SQLite, WAL) ◀── reconciler (worker)
                                            + HMAC + dashboard                                      │ create/poll/message (Devin v3)
                                                  ▲                                                 ▼
                                            dashboard ◀──────────────────────────────────  Devin cloud session ─▶ opens PR on the fork
                                                                                                  │
                                                                          verifier: PR ▶ head_sha ▶ check-runs ▶ false-green guard ▶ self-heal
```

### 5.1 Components
- **`ingest` (FastAPI + uvicorn):** the GitHub webhook (verify `X-Hub-Signature-256` HMAC-SHA256
  over the raw body), normalize the event, write a `queued` remediation row. Also serves the
  dashboard and the human approval gate. Rejects oversized or bad-signature payloads.
- **`scanner`:** pure-Python rules that grep the read-only clone for the portfolio patterns and open
  labeled issues. Runnable on demand and on a schedule.
- **`reconciler` (worker):** the heart. A level-triggered loop reads desired state (open labeled
  issues) and actual state (Devin sessions + real CI) and closes the gap. Owns the FSM, dedupe,
  budget, polling, self-heal, and verification.
- **`devin_client`:** a thin v3 wrapper behind an interface, with a `SIMULATE` implementation that
  replays a deterministic session lifecycle (no network, no ACU) for all dev/test/demo.
- **`store` (SQLite, WAL):** restart-safe. Tables: `remediations`, `sessions`, `events`,
  `metrics_snapshots`. The reconciler reads state from here every tick, so a restart resumes cleanly.
- **`verifier`:** resolve `pull_requests[0].pr_url` → PR number → `head_sha` → poll check-runs. Apply
  the change-aware false-green guard. Derive success.
- **`dashboard` (server-rendered + htmx):** the ops console (§7). htmx is vendored locally so the
  board works offline.

Runs as **two processes** in docker-compose: `web` (ingest + dashboard) and `worker` (reconciler).

### 5.2 State machine (per remediation)
```
queued → dispatched → pr_opened → verifying ─┬─ green → awaiting_merge → merged
                                             ├─ red → healing → verifying   (cap 2 heal attempts)
                                             ├─ refused            (structured_output.refused)
                                             ├─ needs_human        (heal cap hit / waiting_for_user)
                                             └─ failed | expired   (error / ACU limit / no PR)
```

### 5.3 Safety & correctness primitives
- **Local dedupe is mandatory.** v3 has no server-side idempotency, so the store enforces one
  remediation per `(issue_id, spec_hash)`; at most one active session per remediation. A re-delivered
  webhook or a resync tick never double-spawns a paid session.
- **`max_acu_limit` on every create** + a **global ACU budget ceiling**. Dispatch stops before
  worst-case spend could exceed the ceiling.
- **Derive success from `structured_output` + `pull_requests`, never status alone.** A terminal
  session with no PR or `remediated==false` is a failure.
- **`SIMULATE=true` everywhere except a rehearsed real run.** Tests mock the client (respx).
- **Secrets only in `.env`** (gitignored); the client factory refuses to build a real client without
  a `cog_` key.

---

## 6. Devin v3 API contract

Base `https://api.devin.ai/v3`, `Authorization: Bearer cog_...`. Org-scoped calls are
`/v3/organizations/{org_id}/...`.

| Purpose | Call |
|---|---|
| Verify key (zero ACU) | `GET /v3/self` |
| Create session | `POST /v3/organizations/{org_id}/sessions` |
| Poll session | `GET /v3/organizations/{org_id}/sessions/{session_id}` |
| Self-heal message (auto-resumes a suspended session) | `POST /v3/organizations/{org_id}/sessions/{id}/messages` |
| List / tag / terminate | `GET`/`POST`/`DELETE .../sessions...` |
| Per-session ACU | `acus_consumed` on the session; daily rollup via `.../consumption/daily/sessions/{id}` |
| Playbooks / knowledge / schedules | `POST .../playbooks` / `.../knowledge/notes` / `.../schedules` |

### 6.1 `SessionCreateRequest`
`prompt` (required) plus: `max_acu_limit`, `tags` (correlation keys), `title`, `repos`, an optional
`playbook_id`, `structured_output_schema` (§6.3), and `resumable: true` (default, needed for
self-heal). `devin_mode` defaults to `normal` (`fast` is ~2x speed / 4x cost). v3 has **no
`idempotent` param**, local dedupe is mandatory.

### 6.2 Status model & success derivation
- `status` ∈ `new | claimed | running | exit | error | suspended | resuming`.
- `status_detail` (running) ∈ `working | waiting_for_user | waiting_for_approval | finished | …`.
- **Success** = (`status==exit` OR `status_detail=="finished"`) AND `structured_output.remediated`
  AND `pull_requests[0].pr_url`.
- **The change-aware false-green guard:** when all check-runs are complete, the verifier classifies
  the PR's changed files and requires the gating job *relevant to the change* to have actually run
  with conclusion `success`: a python change must run `unit-tests (current)`; a frontend-only change
  must run a frontend job (the python tests legitimately skip); docs-only requires no test gate.
  Only `failure / timed_out / action_required / startup_failure` count as RED; `cancelled / stale /
  skipped / neutral` are non-failing.

### 6.3 Structured output schema (passed on every create)
```json
{
  "type": "object",
  "properties": {
    "remediated": {"type": "boolean"}, "refused": {"type": "boolean"},
    "refusal_reason": {"type": "string"}, "change_class": {"type": "string"},
    "root_cause": {"type": "string"}, "files_changed": {"type": "array", "items": {"type": "string"}},
    "tests_added": {"type": "boolean"}, "verification_ran": {"type": "string"},
    "pr_url": {"type": "string"}, "residual_risk": {"type": "string"}
  },
  "required": ["remediated", "refused", "root_cause", "pr_url"]
}
```

### 6.4 Self-heal
On red CI, pull the failing job's log tail and message the session ("CI failed on PR #N. Failing
check: … Log tail: … Fix forward and push to the same branch."). The control plane heals **only when
the session is not actively working** (it must not fight Devin's own CI loop) and **only once per
head sha** (a per-commit cooldown). Cap heal attempts at 2, then → `needs_human`. Fallback for a
non-resumable session: a new bounded session.

---

## 7. Observability

The ops console answers "is this working?" at a glance:
- **Backlog burn-down:** open `devin-remediate` issues over time vs. remediated count.
- **Fleet board:** one row per remediation → issue link, session, `status`/`status_detail`, PR + CI
  state, real `acus_consumed`, heal attempts.
- **Throughput / MTTR:** PRs opened & merged; median time from labeled → green PR.
- **Cost:** ACU per resolved PR from `acus_consumed`, an ACU-vs-budget gauge, and `$/PR` at a
  **stated** rate (labeled as illustrative in SIMULATE; measured for live runs).
- **Outcomes:** success / failed / refused / needs-human counts; self-heal rate.

A `structlog` audit trail sits behind the dashboard; logs alone are not the deliverable.

---

## 9. Risk register

| Risk | Mitigation |
|------|------------|
| Self-heal channel may not accept messages post-PR | Probe B confirmed: messaging an idle session resumes it; `resumable:true` default; fallback = a new bounded session. |
| Control plane fights Devin's own CI loop | Self-heal only after the session idles, and once per head sha (per-commit cooldown). Tested. |
| CI false-green from a skipped job | The change-aware false-green guard requires the change-relevant job to have actually run, not skip. |
| `finished` treated as success | Derive success from `structured_output.remediated` + `pull_requests`. |
| Double-spend on a re-delivered webhook | Local dedupe on `(issue_id, spec_hash)`; v3 has no server idempotency. |
| Generated fix trips banned-API/pre-commit | Playbooks warn: use `superset.utils.json`, avoid `make_url`, commit the autofixed output. |
| Cost number looks fake | Use real `acus_consumed`; label the $/ACU rate as a stated assumption. |

---

## Known limitations & next steps

Non-blocking robustness gaps. Some were addressed (marked DONE); the rest are intentionally deferred.

**Done:**
- **Check-run classification.** Only `failure / timed_out / action_required / startup_failure` are
  RED; `cancelled / stale / skipped / neutral` are non-failing (avoids spurious heals).
- **Change-aware false-green guard.** Requires the change-relevant gating job to have run (§6.2).
- **Self-heal gating.** Heal only after the session idles, once per head sha (no fighting Devin's CI).

**Next steps:**
- **Double-spend crash window.** A crash between `create_session` and the DB write could orphan a
  paid session and re-dispatch. Fix: write a `pending` row keyed on `(issue_id, spec_hash)` before
  `create_session`. And don't retry non-idempotent POSTs (`/sessions`, `/messages`) on 5xx; reconcile
  by tag instead. (Local dedupe already prevents the steady-state double-spawn.)
- **Stuck in VERIFYING.** Add a poll-count/wall-clock timeout in the reconciler → `NEEDS_HUMAN` if a
  required check never completes.
- **Hardcoded path default.** `SUPERSET_CLONE` defaults to a sibling checkout; make it fully
  env-required if preferred.
- **Burn-down `open_count`.** It counts terminal-but-unmerged states as open; define open as
  in-flight states only, or render resolved-vs-merged separately.
