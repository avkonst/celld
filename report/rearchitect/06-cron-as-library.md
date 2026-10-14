# 06 — Cron Triggers as an app library on top of alarms

**Status:** Explored (design sketch)
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Builds on:** [01 — Wasm-only runtime](01-wasm-only-runtime.md) (cells as wasm components, durable alarms through `celld:alarm/schedule`, §3.7)

---

## 1. Premise

Cron Triggers (C9) are already **an application of alarms** (C2):
- Each script with `triggers.crons` gets one reserved cell (`.cron:<script>`).
- That cell's alarm is set to the next cron occurrence. When it fires, the cell runs the Worker's `scheduled()` handlers and re-arms the alarm.
- Fleet-wide "once per occurrence" comes from cell ownership: only the owner fires the alarm.

The only cron-specific logic is the **pure** parser and planner in `crates/logic/cron.rs`: `parse`, `matches`, `next_after`, `next_across`, `matching`, `plan`, `cron_rearm`. It does no I/O, and its only dependencies are two small helpers (`wake::civil_from_days`, `alarm::alarm_retry`). Everything else in C9 is platform plumbing:

| Plumbing | Where |
|---|---|
| A reserved class | `generation.rs`, `bootstrap.rs` |
| The `__cron_plan` op | `js.rs` |
| A harness class | `harness.js` |
| Boot-time arming on every node | `main.rs` `arm_cron_schedule` / `spawn_cron_arm` |
| Deploy-config validation | `deploy.rs` |

**This exploration asks: in the wasm design, should cron move out of the platform into a library the app links, keeping only the alarm primitive in celld?**

## 2. Goals and non-goals

**Goals**
- celld's platform exposes **alarms only**. Cron becomes app-level code built on them.
- Keep today's guarantees: one run per occurrence across the fleet, and a durable next-occurrence alarm.
- Improve on today's gaps, which the library makes cheap:
  - persisted retry state (today `_retry`/`_owed` are lost on an ownership move);
  - a configurable policy for slow handlers and catch-up (today a slow handler silently skips occurrences, and only one missed occurrence is caught up);
  - optional time zones.

**Non-goals**
- A `triggers.crons`-style platform config key. Schedules live in app code; there is no Cloudflare compatibility to keep (01).
- A distributed cron service across apps.

---

## 3. Design

### 3.1 Split of responsibilities

| Concern | Today (platform) | With the library |
|---|---|---|
| Parse and plan cron expressions | `crates/logic/cron.rs` (platform) | **The `celld-cron` crate, linked into the app** (the same code) |
| Durable timer | Alarm (C2) | Alarm (C2), unchanged: `celld:alarm/schedule` |
| Once per occurrence across the fleet | Cell ownership of `.cron:<script>` | Cell ownership of an **app-named scheduler cell**, unchanged |
| Initial arming after deploy | Every node calls `spawn_cron_arm` at boot and adoption | A **generic `on-deploy` lifecycle hook** (§3.3), not cron-specific |
| Handler dispatch | Harness class `CelldCronSchedule` calls `scheduled()` | The library's `on-alarm` dispatches to the app's registered handlers |
| Retry state | In isolate memory (lost on a move) | **Persisted in the scheduler cell's SQLite** |
| Schedule validation | Deploy time (`deploy.rs`) | Build or test time (crate unit tests) and on `on-deploy`. An invalid expression fails the hook and is reported (§3.5). |

### 3.2 The library: `celld-cron`

**Origin.** It is extracted from `crates/logic/cron.rs`, plus the two helpers it uses (`civil_from_days`, alarm backoff), as a standalone crate with no dependencies. It still compiles to wasm, because the logic is pure.

**The app-facing API** (sketch):

```rust
use celld_cron::{Schedule, Catchup, Overrun};

// One scheduler cell per app (or per tenant), e.g. "cron:main".
celld_cron::schedule! {
    cell: "cron:main",
    catchup: Catchup::One,            // None | One (today's behaviour) | All { max: 10 }
    overrun: Overrun::Skip,           // Skip (today) | Delay | Concurrent? (open question)
    jobs: [
        ("0 * * * *",   rebuild_report),
        ("*/5 * * * *", sweep_sessions),
        ("0 9 * * MON", weekly_digest, tz = "Europe/Berlin"),   // optional tz feature
    ],
}
```

The macro (or a builder API) generates the scheduler cell's `on-alarm` and `on-deploy` exports.

**Persisted state** in the scheduler cell's SQLite:

| Table | Contents |
|---|---|
| `_cron_schedule` | The schedule digest (a hash of the job list), so a new deployment's changed schedule is detected |
| `_cron_runs` | The last fired occurrence per job, the retry count, and owed retries |

**Handler contract.**
- A handler receives `scheduled_time` (the occurrence) and an attempt number.
- A job is retried with the existing alarm backoff.
- Failures beyond the limit are recorded and logged, and never block other jobs.

**Time zones** are an opt-in crate feature, using a tz database such as `chrono-tz`. Today's platform cron is UTC-only. Adding time zones now costs a crate feature, not a platform change.

### 3.3 Bootstrapping: the generic `on-deploy` hook

Something must arm the first alarm and re-plan after a schedule change. Today every node does it at boot and adoption (`main.rs:751-822`). A library can't run on its own. Options:

| Option | How | Verdict |
|---|---|---|
| **A. Generic `on-deploy` lifecycle hook** | The manifest lists cells to wake after each deployment adoption, e.g. `wake_on_deploy: ["cron:main"]`. The host calls their `on-deploy(version)` export **once per deployment version, fleet-wide**: the call goes to the cell's owner, and the cell records the version it handled in its SQLite, so repeated calls from several nodes are no-ops. | **Recommended.** Not cron-specific; also serves schema migrations, warm-ups and backfills. |
| B. Arm on first request | The app arms the schedule the first time any request arrives | Fragile: no traffic means no schedule |
| C. Keep a minimal cron hook in the platform | The platform knows "schedule cells" but not cron syntax | Leaves a cron-shaped concept in the platform |

**`on-deploy` in the library:**
1. Compare the schedule digest with `_cron_schedule`.
2. If it changed, re-plan with `next_across(jobs, now)` and replace the alarm.
3. If the alarm is overdue, apply the `catchup` policy.

This is the same arm-or-keep logic as today's `__cron_plan`, moved into the app.

### 3.4 Semantics, kept and changed

| Property | Today (C9) | Library |
|---|---|---|
| Once per occurrence across the fleet | Yes (ownership) | Yes (ownership of the scheduler cell) |
| Crash mid-handler | At-least-once (the alarm fires again) | At-least-once. Handlers should be idempotent, keyed by `(job, scheduled_time)`; the library exposes that key. |
| Slow handler overruns the next occurrence | Silently **skips** the intervening occurrences (the docs say "delays") | **Configurable:** `Skip` (today) or `Delay` (run the owed occurrence right after). Documented precisely. |
| Downtime catch-up | Exactly one missed occurrence | **Configurable:** `None`, `One`, or `All { max }` |
| Retry state across an ownership move | Lost (held in memory) | **Kept** (SQLite) |
| Time zones | UTC only | Optional |
| Validation timing | At deploy (refuses bad expressions) | At build or test, plus `on-deploy` (reported, see §3.5). Deploy no longer knows cron syntax. |

### 3.5 Failure reporting

- **An invalid expression or failed `on-deploy`.** The hook returns an error. The host logs it and surfaces it in `/state` and `celld diagnose` as a failed deploy hook for that cell. The alarm retry backs off and retries.
- **Open question:** should a failed `on-deploy` mark the deployment unhealthy, or block adoption? Today a bad expression can't reach a node at all, because deploy refuses it.

### 3.6 Language reach

- **Rust apps** link the crate directly.
- **Other guest languages** have two options:
  - **a component.** Ship `celld-cron` as a wasm component exporting `on-alarm` / `on-deploy`, which imports the app's job-dispatch interface. Compose it into the app with component-model tooling (e.g. `wac`). That keeps it language-neutral.
  - **a port.** About 450 lines of pure logic is small enough to port.

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| C9 Cron | Not built in the platform: the reserved class, `__cron_plan`, the harness class, boot arming, deploy validation. The logic moves to `celld-cron`. |
| C2 Alarms | Unchanged. Cron is one of its clients. |
| D9 Deployment | Gains the generic `wake_on_deploy` list and the `on-deploy` invocation, once per version per listed cell, through the owner. |
| D1 Core | No change. The scheduler cell is an ordinary cell. |
| E1 Deploy | Drops cron-config validation. Adds `wake_on_deploy` to the manifest. |
| E3/E4 Operations | `/state` and `celld diagnose` show the deploy-hook status per cell. |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

Code lines, measured against the baseline report's line map; estimates are marked *(est.)*. Today's C9: **615 production** (`logic/cron.rs` 416, `js.rs` 58, `harness.js` 58, `main.rs` 57, `generation.rs` 12, `bootstrap.rs` 11, `runtime.rs` 3), **27 tools** (`deploy.rs`), **7 test** (ungated), **14 examples**, **62 docs**.

| | Platform production | `celld-cron` crate (app library) | Tools | Test | Examples | Docs |
|---|--:|--:|--:|--:|--:|--:|
| **Removed from the platform:** reserved class, `__cron_plan` op, harness class, boot arming, generation and bootstrap wiring | **−199** | – | – | −7 | – | – |
| **Moved:** `logic/cron.rs` → `celld-cron` | **−416** | **+416** (plus about 20 for the two helpers) | – | – | – | – |
| Removed: deploy-time cron validation | – | – | **−27** | – | – | – |
| Added: generic `on-deploy` hook and `wake_on_deploy` manifest list in the host | **+100 to +250** *(est.)* | – | +20 to +50 *(est.)* | – | – | – |
| Added: library API (schedule macro or builder, SQLite state, catch-up and overrun policies, dispatch) | – | **+200 to +400** *(est.)* | – | – | – | – |
| Added: optional time-zone feature | – | +30 to +80 *(est.)*, plus a tz-database dependency | – | – | – | – |
| Added: tests (the crate is pure, so cheap to test thoroughly) | – | – | – | +300 to +600 *(est.)* | – | – |
| Example and docs (replace `examples/cron`, rewrite `cron-triggers.md`) | – | – | – | – | −14 → +30 to +50 | −62 → +60 to +100 |
| **Net, platform (celld)** | **≈ −365 to −515** | – | **≈ −7 to +23** | – | – | – |
| **Net, library** (moved + new) | – | **≈ +666 to +916** (416 moved + 250 to 500 new) | – | – | – | – |
| **Net, platform + library** | **≈ +150 to +550 overall.** The platform shrinks; app-side code grows. | | | ≈ +0.3k to +0.6k | ≈ +0.02k to +0.04k | ≈ 0 to +0.04k |

**What the numbers mean.**
- The **platform** loses all cron knowledge, about 0.4k to 0.5k lines net, after adding the generic hook. It keeps one primitive (alarms) plus one generic lifecycle hook.
- The **total** grows slightly, because the library gains policies the platform never had: persisted retries, configurable catch-up and overrun, optional time zones.
- The hook is reusable beyond cron, for migrations, warm-ups and backfills. Count it as platform infrastructure, not cron cost.

### 5.2 Effect on doc 01

Doc 01 counts the JS halves of the services as rewritten or dropped. For cron, that's the 58 harness lines. With this doc, cron needs **no** host-side WIT interface of its own: `celld:alarm/schedule` plus `on-deploy` is enough. Doc 01's host-implementation estimate shrinks slightly.

---

## 6. Risks and open questions

**Risks**
1. **Lost deploy-time validation.** A bad expression is caught at build or test time, or at `on-deploy`, not by `celld deploy`. Mitigation: the library validates in a `const`/build step where possible, and failed hooks are visible in `/state` and `diagnose`.
2. **Bootstrap dependency.** Schedules now depend on the generic `on-deploy` hook working reliably. It must be retried and idempotent, keyed by deployment version.
3. **Every app owns its scheduler cell.** Choosing a single scheduler cell versus one per tenant becomes an app design decision, with the usual hot-cell trade-off.

**Open questions**
- Should a failed `on-deploy` block adoption or only be reported (§3.5)?
- Should `Overrun::Concurrent` (start the next occurrence while one is still running) exist? Today's handlers are serialized.
- Should `celld-cron` live in the celld repo, versioned with the platform, or as a separate crate on crates.io?
- What is the default `catchup`: `One` (today's behaviour), or `None`?

## 7. Build order

1. The generic `on-deploy` hook and the `wake_on_deploy` manifest list, as platform infrastructure, with the per-version idempotency record.
2. `celld-cron`: start from celld's `crates/logic/cron.rs` and its two helpers, then add the library API, SQLite state, and catch-up and overrun policies.
3. A cron example as a wasm component using the library.

The platform never gets cron plumbing: no reserved class, plan op, harness class, boot-time arming or cron config key.

## 8. Next steps

- [ ] Specify the `on-deploy` hook: the per-version idempotency record, retries, and its reporting in `/state` and `diagnose`.
- [ ] Extract `celld-cron` and keep today's test vectors, including the saffron-divergence cases documented in `cron.rs:16-22`.
- [ ] Decide the `catchup` and `overrun` defaults, and whether time zones ship in v1.
- [ ] Prototype a scheduler cell on wasmtime: `on-deploy` arms, `on-alarm` dispatches and re-arms, and a forced ownership move keeps the retry state.
