# 08 — Workflows as durable steps over SQLite-persisted steps

**Status:** Explored (design sketch). Grounded in the repairai implementation and spikes.
**Context:** a green-field app inspired by celld (see the [README](README.md)). celld's C8 engine is the reference being replaced, and nothing is migrated.
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Reference:** `~/projects/repairai`:
- `design-exploration.md` §11 (Durable execution), §12 (streaming), §15 (outbox pattern);
- `repairai-routing-agent/src/{session.rs,store.rs,tx.rs,turn.rs}`;
- `repairai-workflow/` (the typed binding to today's engine);
- `spikes/workflow-rust/`.

**Builds on:** [06 — Cron as a library](06-cron-as-library.md), the same pattern: app library over cell SQLite and alarms. **Related:** [02 — SSE](02-sse-instead-of-websockets.md) (event log), [07](07-drop-kv-queues-r2-assets-containers.md) (the reduced service set).

---

## 1. Premise

**Today's C8 Workflows** is a platform engine:
- Each instance is a cell of a reserved class (`__Workflow.<script>`).
- The engine is about 1,350 lines of harness JavaScript, built on cell storage and alarms.
- It provides `step.do` / `sleep` / `sleepUntil` / `waitForEvent`, retries, pause/resume/restart/terminate, and 30-day retention.

The baseline report rates it **Beta**, for these reasons:
- **Replay determinism is not checked.** A same-kind step mismatch, for example after a redeploy that reorders steps, silently returns the wrong memoized value.
- **A timed-out attempt keeps running** alongside its retry.
- **A crash mid-callback re-runs the attempt without counting it** as a retry.
- Pending non-step awaits fail the instance after 60 s, and step results are capped at 1 MiB.

**What repairai found** (`design-exploration.md` §11):
- It compared **A: the Workflows engine** with **B: its own library in the entity object**.
- It decided on a hybrid on 2026-10-12. Each agent turn is a Workflows-engine instance (A). The `Session` object holds the B-style code that must be atomic with its state: idempotent submit with a `client_turn_id UNIQUE`, the turn queue, the event log, a watchdog alarm, and `transactionSync`.
- It listed "a `ctx.run` journal inside entity objects, and the outbox" as the next B-only step.

**Decision recorded:** in the new app, workflows are **durable steps persisted in SQLite tables inside the cell**. That is repairai's variant B, as an **app library** over cells and alarms, not a platform engine.

**Why B wins once Cloudflare compatibility is gone:**
- **Atomicity.** The step journal and the entity's business state live in **one SQLite database and commit in one transaction**. The engine keeps them in separate cells.
- **Streaming and locality.** A run executes in the cell that owns its stream and event log, so progress goes straight to subscribers (doc 02), with no side channel.
- **Limits are yours.** No 60-second non-step rule; the size cap is chosen by the library (§3.10).
- **Portability.** It needs only cell SQLite plus alarms, so it runs on today's V8 celld, on the wasm design (doc 01), and on Cloudflare Durable Objects.
- **No platform engine** to maintain or bind (the repairai binding exists only because workers-rs has no Workflows support).

## 2. Goals and non-goals

**Goals**
- A `ctx.run(name, opts, fn)` journal, with durable sleep, durable event waits, retries, timeouts, and status/admin, persisted in the cell's own SQLite.
- Fix the three C8 correctness gaps:
  - determinism and drift detection;
  - real cancellation of timed-out attempts;
  - attempts counted before they run.
- One transaction for a local step's journal row and its business-state writes.
- Exactly-once *effects* across cells, through idempotency keys plus the outbox (repairai §15). The underlying execution stays at-least-once.

**Non-goals**
- Keeping the Cloudflare Workflows API (`WorkflowEntrypoint`, the `workflows` config key, `env.WF.create`).
- A central orchestration service (Restate- or Temporal-style server). Each cell owns its runs (repairai: "without a Restate server").

---

## 3. Design

### 3.1 Where a run lives

| Case | Run lives in | Notes |
|---|---|---|
| The workflow belongs to an entity (a session turn, an order, a job) | **That entity's cell** (default) | Journal and business state commit together. One writer per entity serializes its runs. |
| No natural entity (an ad-hoc job or batch) | **A dedicated run cell**, named by run ID | The same library, in a cell whose only state is the run. Equivalent to today's one-cell-per-instance. |
| Many concurrent runs for one hot entity | Split: the entity cell plus per-run cells, coordinated through the outbox | Avoids one writer becoming the bottleneck |

### 3.2 Tables, in the cell's SQLite

The schema follows repairai's `store.rs` conventions: status columns, `attempt` counters, a client idempotency key that is `UNIQUE`, and an append-only event log.

```sql
-- One row per workflow invocation (run).
CREATE TABLE _runs (
  run_id        TEXT PRIMARY KEY,
  client_key    TEXT UNIQUE,              -- idempotent submit (repairai: client_turn_id)
  kind          TEXT NOT NULL,            -- handler name
  code_version  TEXT NOT NULL,            -- drift detection (3.6)
  input         BLOB NOT NULL,
  status        TEXT NOT NULL,            -- queued|running|sleeping|waiting|paused|done|failed|cancelled
  next_wake_at  INTEGER,                  -- ms; the alarm multiplexer reads MIN() of this (3.5)
  lease_gen     INTEGER NOT NULL DEFAULT 0, -- single-driver guard (3.4)
  result        BLOB, error TEXT,
  created_at    INTEGER NOT NULL, updated_at INTEGER NOT NULL, finished_at INTEGER
);
CREATE INDEX _runs_wake ON _runs(next_wake_at) WHERE next_wake_at IS NOT NULL;

-- The step journal: one row per completed (or permanently failed) step.
CREATE TABLE _steps (
  run_id     TEXT NOT NULL,
  step_key   TEXT NOT NULL,               -- explicit name (+ occurrence for loops), 3.6
  seq        INTEGER NOT NULL,            -- order of first execution, for drift checks
  kind       TEXT NOT NULL,               -- run|sleep|event|call
  status     TEXT NOT NULL,               -- in_progress|done|failed
  attempts   INTEGER NOT NULL DEFAULT 0,  -- incremented and committed BEFORE an attempt (3.3)
  next_retry_at INTEGER,
  result     BLOB, error TEXT,
  started_at INTEGER, finished_at INTEGER,
  PRIMARY KEY (run_id, step_key)
);

-- Durable events for wait_for_event; buffered when they arrive early.
CREATE TABLE _run_events (
  run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL,
  payload BLOB NOT NULL, consumed_by TEXT, received_at INTEGER NOT NULL,
  PRIMARY KEY (run_id, seq)
);

-- Outbox for effects on other cells / services (repairai §15).
CREATE TABLE _outbox (
  seq INTEGER PRIMARY KEY, run_id TEXT, step_key TEXT,
  target TEXT NOT NULL, body BLOB NOT NULL, idem_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER
);

-- Callee side: idempotent handler results (key -> result).
CREATE TABLE _idem (key TEXT PRIMARY KEY, result BLOB NOT NULL, at INTEGER NOT NULL);
```

### 3.3 Steps: `ctx.run`

```rust
let plan: Plan = ctx.run("plan", StepOpts::default(), |tx| async move {
    // tx: synchronous SQL handle in the SAME transaction as the journal row
    let p = make_plan(&tx)?;          // business writes allowed here
    Ok(p)
}).await?;

let reply: Reply = ctx.run("model", StepOpts::retries(3).backoff_exp(2_000).timeout(600_000),
    |_tx| async move { call_llm(idem_key!()).await }   // external effect: at-least-once
).await?;
```

**Execution of one step:**
1. **Memoized?** If `_steps` has the key with `status = done`, return the stored result without running anything.
2. **Count the attempt first.** In one committed transaction:
   - `attempts = attempts + 1`, `status = in_progress`;
   - fail permanently if `attempts > limit`.

   A crash during the attempt is therefore counted. This fixes C8's uncounted-crash-retry gap.
3. **Run the attempt.**
   - **Local steps** (only `tx` writes, no external I/O) run synchronously inside a transaction with the journal update, so business state and journal commit together: repairai's `transaction_sync` pattern (`tx.rs`).
   - **External steps** (HTTP, LLM, another cell) run outside the transaction, receive an **idempotency key** derived from `(run_id, step_key)`, and commit their result afterwards.
   - Before a side effect that must happen only once, call `storage.sync()`, so the counted attempt is durable first (repairai §11, durability detail).
4. **Timeouts cancel for real.** In Rust, dropping the attempt's future cancels it, so a timed-out attempt stops instead of running on in the background. This fixes C8's timed-out-attempt gap. A JS port can cancel cooperatively with an `AbortSignal` passed to the callback. In the wasm design (doc 01), per-call CPU budgets add a hard stop.
5. **Failure.** Set `next_retry_at` from the backoff, set the run's `next_wake_at`, and return "suspend". The alarm resumes the run.

**Limits.**
- A cell's SQLite caps one value at **2.2 MB** (`SQLITE_LIMIT_LENGTH`, `storage.rs:848`).
- The library default caps results at **1 MiB**, configurable.
- Bigger results go into the app's own tables or external storage, and the step returns a reference.

### 3.4 Driving a run: replay with a single driver

**Replay model,** as in the Cloudflare engine and repairai's spike: `drive(run_id)` calls the handler from the top. Completed steps return memoized values. The first incomplete step either runs, or suspends the run (a sleep, an event wait, or a retry backoff).

**Single driver.** Durable Object awaits interleave: only `blockConcurrencyWhile` and transactions block other events (report C1). So `drive` takes a **per-run lease** before awaiting anything:
- it bumps `_runs.lease_gen` and remembers the value;
- every journal write checks `lease_gen = remembered` in its SQL.

A concurrent or stale driver therefore cannot commit. This generalizes repairai's "mark running before the await" (`session.rs:226-227`).

**Code outside steps must be deterministic.** It re-runs on every replay. The library helps:
- `ctx.now()` and `ctx.random()` are journaled values;
- debug builds detect divergence (§3.6).

### 3.5 Timers, the alarm multiplexer, and wake triggers

A cell has **one** alarm (C2), and many runs and steps may be waiting.

**Multiplexing:**
- After each drive, set the cell alarm to `SELECT MIN(next_wake_at) FROM _runs` (and the outbox's `MIN(next_at)`).
- On `alarm()`, drive every run whose `next_wake_at <= now`, in bounded batches (the `logic/sweep.rs` idea), then re-arm.

**`ctx.sleep(name, dur)` / `sleep_until`** write a `_steps` row of `kind = sleep` with an absolute wake time. A replay never extends it, as in today's engine.

**What triggers a drive** (from repairai §11, "What triggers a re-run"):
- the alarm: a timer, a retry, or the watchdog;
- an event arriving (§3.7);
- a new submit;
- a cell start: a watchdog scan of runs in `running`, `sleeping` or `waiting` status.

**Watchdog.** repairai's watchdog alarm (`TURN_WATCHDOG_SECS`) becomes a library feature. It re-drives runs that are stuck in `running` past a deadline, for example after a crash between commit and re-arm.

### 3.6 Step identity, determinism and code drift

This is the main C8 gap to fix.

- **Explicit step keys.** A step key is the name the developer gives, plus an occurrence index only inside loops (`ctx.run(("fetch", i), …)`). Names must be unique within a run, and the library checks this at runtime.
- **Kind and order checks.** When a replay reaches a journaled step whose `kind` differs, or reaches a new step before an existing `seq`, the run fails with a **drift error**. It never silently returns another step's value.
- **`code_version` per run.** Each run records the handler version it started on. On deploy:
  - **compatible handlers** declare that they accept older versions;
  - **incompatible changes** use a **patch marker**, `if ctx.patched("v2-split-plan") { … } else { … }`, recorded in the journal. This is the same idea as Temporal's patching, so old runs keep their old path and new runs take the new one;
  - **or drain:** pause new submits of that kind until in-flight runs finish.
- **Debug builds** replay each completed run once more against its journal to detect nondeterminism early.

### 3.7 Events: `ctx.wait_for_event`

- `send_event(run_id, type, payload)` is a request to the cell. It appends to `_run_events` (buffered if early) and drives the run.
- `ctx.wait_for_event(name, type, timeout)` consumes the first matching unconsumed event. It sets `consumed_by = step_key` **in the same transaction** as the step's journal row, so an event is consumed exactly once. Otherwise the run suspends with a timeout wake.

### 3.8 Calls between cells and services: exactly-once effects

Following repairai §11 "Without a Restate server" and §15:
- **Caller side.** `ctx.call(target, body)` writes an `_outbox` row with a deterministic `idem_key = (run_id, step_key)` in the step's transaction. Delivery is driven by the alarm with backoff, and the step completes when the callee acknowledges.
- **Callee side.** A wrapper, `idempotent(key, || handler)`, stores `key → result` in `_idem`. A retried call returns the stored result.
- **Guarantee.** Execution is at-least-once, but effects are exactly-once wherever both sides use keys.
- **Receivers ignore duplicates** by `(source, seq)`.

### 3.9 Status, admin and retention

- **API:** `status(run_id)`, `list(filter, cursor)`, `pause`, `resume`, `terminate`, and `restart(from_step)`. Restart deletes the journal rows from that step onwards, in a transaction, and bumps `lease_gen`.
- **Retention.** Finished runs are deleted after N days (configurable; today's engine uses 30) by an alarm-driven sweep in bounded batches.
- **Observability.**
  - Every run, step and attempt transition appends to the cell's **event log**. That is the same log doc 02 streams over SSE, so progress streaming comes free, as in repairai's `progress` events.
  - Spans carry `run_id` and `step_key`.

### 3.10 Library form and languages

- **Rust crate `celld-steps`**, the primary implementation. It is the natural home in the wasm design (doc 01).
- **JS/TS implementation**, optional, only if the app supports JS guests or a V8 runtime. It is one spec with two implementations, the trade-off repairai noted ("one spec, two implementations to keep in sync"). A shared conformance test suite drives both.
- **Portability note:** the library also runs on Durable Object runtimes such as celld's V8 runtime (with workers-rs) or Cloudflare. There it needs synchronous SQL and `transactionSync`; repairai's `tx.rs` (41 lines) shows the binding. That makes repairai a ready test bed.

### 3.11 What changes compared with today's engine (C8)

| Property | C8 platform engine | Durable steps library |
|---|---|---|
| Atomic with entity state | No (a separate cell) | **Yes** (one SQLite transaction) |
| Determinism and drift | Unchecked; silent wrong memo on a same-kind mismatch | **Checked:** kind and order checks, code version, patch markers |
| Timed-out attempt | Keeps running | **Cancelled** (future dropped, or `AbortSignal`) |
| Crash mid-attempt | Re-run, not counted | **Counted:** the attempt is committed first |
| Non-step work limit | 60 s, then the instance fails | None. Only steps are durable; the rest is ordinary code. |
| Step result size | 1 MiB | Configurable, up to the 2.2 MB SQLite value limit, or by reference |
| Streaming progress | Needs a side channel | Native: the run's own cell holds the event log |
| Retries, timers, events | Built in | Built in, in the library |
| Admin | `status`/`pause`/`resume`/`restart`/`terminate`, 30-day retention | The same, as library API |
| Portability | celld engine or Cloudflare Workflows | Any Durable Object runtime (celld V8, celld wasm, Cloudflare) |
| Platform code | ~1.45k production lines in celld | **0**. It uses only SQLite, transactions and alarms. |

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| C8 Workflows | Not built in the platform: no reserved `__Workflow.<script>` class, harness engine, workflow injection or aliasing (celld's `bootstrap.rs`), `workflows` deploy config, or test hooks |
| C2 Alarms | Unchanged. The library multiplexes many timers onto the cell's single alarm (§3.5). |
| C1 Durable Objects | Unchanged. The library uses `ctx.storage.sql`, `transactionSync` and `storage.sync()`. |
| D1–D9 engine | Unchanged. Runs are ordinary cell data and ordinary cell activations. |
| E1 deploy | No `workflows` config key |
| Doc 02 (SSE) | Run, step and attempt transitions go to the cell's event log, which SSE streams |
| Doc 06 (cron) | Cron jobs can submit runs (`client_key = job + scheduled_time`) for durable multi-step scheduled work |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

Code lines, measured against the baseline report's line map; estimates are marked *(est.)*. Today's C8 is:
- **1,454 production:** `harness.js` 1,351, `bootstrap.rs` 80, `deploy.rs` 9, `fleet.rs` 9, `engine_api.rs` 5;
- **83 tools:** `deploy.rs`;
- **141 test:** 117 gated (`js.rs` 74, `bootstrap.rs` 30, `engine_api.rs` 13) and 24 ungated (`main.rs`);
- **42 examples, 63 docs.**

**Sizing reference: repairai today.** Measured code lines, excluding comments and blanks:
- `store.rs` 211 (turn and event tables, idempotent submit, statuses);
- `tx.rs` 33 (`transactionSync` binding);
- about 100 lines of dispatch and watchdog in `session.rs`;
- the engine binding `repairai-workflow` 212.

That's under about 560 lines, and the Workflows engine still does the journal, retries, sleeps and events. A generic steps library replaces the engine **and** the binding, so its **core** should land in the same order of magnitude. Earlier drafts of this doc sized every optional feature at once (+1.25k to +2.4k); the table below separates the core from optional extensions.

| | Platform production | `celld-steps` library (app side) | Tools | Test | Examples | Docs |
|---|--:|--:|--:|--:|--:|--:|
| **Not built in the platform: C8 engine** | **−1,454** | – | **−83** | **−141** | −42 | −63 |
| **Core:** run and step tables, `ctx.run` replay with memoized results, attempt-first counting, retry/backoff and timeout, the lease guard, `sleep` plus the alarm multiplexer, `wait_for_event`, the watchdog | – | **+300 to +600** *(est.)* | – | +300 to +600 *(est.)* | +40 to +80 *(est.)* | +40 to +80 *(est.)* |
| Extension: drift detection (kind and order checks, code version, patch markers) | – | +100 to +200 *(est.)* | – | +100 to +200 | – | +10 to +20 |
| Extension: outbox, idempotent-callee wrapper, `ctx.call` | – | +150 to +300 *(est.)* | – | +100 to +200 | – | +10 to +20 |
| Extension: status/admin API (pause, resume, terminate, restart from step) and retention sweep | – | +100 to +200 *(est.)* | – | +50 to +100 | – | +10 to +20 |
| **Net, platform** | **−1,454** | – | **−83** | −141 | – | – |
| **Net, library: core only** | – | **≈ +300 to +600** | – | ≈ +0.3k to +0.6k | | |
| **Net, library: core + all extensions** | – | **≈ +650 to +1,300** | – | ≈ +0.55k to +1.1k | | |
| **Net, platform + library** | **Core only: ≈ −0.85k to −1.15k. With all extensions: ≈ −0.15k to −0.8k.** | | | ≈ +0.15k to +1.0k | ≈ 0 to +0.04k | ≈ −0.02k to +0.08k |
| Optional: JS/TS implementation, only if JS guests are supported | – | roughly the same size again *(est.)* | – | +shared suite | – | – |

**What the numbers mean.**
- The platform needs no workflow engine: celld's −1.45k production lines, the reserved class, and the deploy config are not built.
- The library **core** is a few hundred lines, comparable to repairai's existing B-style code plus binding. It is smaller than celld's engine because it needs no Cloudflare API validation and no reserved class plumbing, and because it is one transactional module over the cell's SQLite.
- Add the extensions only when a use case needs them. Drift detection should come first, because it fixes the most dangerous C8 gap.
- repairai's engine binding (`repairai-workflow`, 212 code lines) and its `entry.mjs` shim become unnecessary once its turns use the library.

### 5.2 Effect on the other docs
- **Doc 01** counted the C8 harness JS (1,351 lines) as "rewritten or dropped". With this doc it is **dropped**: no host-side Workflows interface is needed.
- **Area C after docs 05, 07 and 08:** C1 Durable Objects and C2 Alarms remain as platform features. C3/C11 become dynamic cells (doc 04). C9 cron (doc 06) and C8 workflows (this doc) become libraries.

---

## 6. Risks and open questions

**Risks**
1. **Hot entities.** Runs that live in their entity's cell share that cell's single writer and its 64-request admission cap. Long or numerous runs on one entity need the per-run-cell split (§3.1).
2. **Determinism discipline.** Replay still needs deterministic non-step code. The drift checks catch divergence; they don't prevent it.
3. **Two implementations** if a JS/TS version ships. Mitigate with one conformance suite.

**Open questions**
- Default step-key style: explicit names only (recommended), or Cloudflare-style name plus call count for compatibility?
- Should `ctx.call` delivery be synchronous-with-retry, or outbox-only?
- What is the default retention, and should finished runs be archived to the bucket before deletion?
- Is a JS/TS implementation needed at all? It depends on whether the app supports JS guests (doc 01 §3.9).

## 7. Build order

1. **Build `celld-steps`** (Rust): the tables (§3.2), `ctx.run` with attempt-first counting and cancellation (§3.3), the lease guard and replay (§3.4), the alarm multiplexer, sleeps and watchdog (§3.5).
2. **Add** drift detection (§3.6), events (§3.7), the outbox and idempotent calls (§3.8), and status, admin and retention (§3.9).
3. **Validate** in repairai, a ready test bed on celld's V8 runtime: move the `AgentTurn` workflow into the `Session` cell as library runs, which drops the `entry.mjs` / `WorkflowEntrypoint` shim and the `repairai-workflow` binding. Re-run repairai's crash test (SIGKILL during the model step resumes as attempt 2).
4. **Optionally,** add a JS/TS implementation if the app supports JS guests.

## 8. Next steps

- [ ] Write the `celld-steps` spec: tables (§3.2), step semantics (§3.3), the lease guard (§3.4), the alarm multiplexer (§3.5), drift rules (§3.6).
- [ ] Prototype in repairai: the `AgentTurn` model step as a `ctx.run` inside `Session`, atomic with `turns`/`events` updates. Reuse the repairai crash test (SIGKILL during the model step resumes as attempt 2).
- [ ] Decide the step-key style and the patch-marker API.
- [ ] Define the conformance suite shared by the Rust library and any JS port.
