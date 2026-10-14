# 11 — Cell concurrency on wasm: component-model async

**Status:** Decided (component-model async). The policy layer is a design sketch.
**Date:** 2026-10-14 (toolchain status checked the same day)
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Builds on:** [01 — Wasm-only runtime](01-wasm-only-runtime.md) (§3.7 async model; §3.10 eviction). Part of the **area D review**.

---

## 1. Premise

In celld a cell is a Durable Object in a V8 isolate:
- **Requests interleave at await points.** A second request runs while the first awaits I/O (a `fetch`, a timer).
- **Storage calls are synchronous** (local SQLite), so no request observes a half-applied storage operation.
- **`blockConcurrencyWhile` and transactions close the *input gate*,** so nothing else runs until they finish (`crates/logic/gate.rs`, `js/input_gate_lifecycle.rs`).
- **The *output gate*** holds every egress until the cell's writes are durable (`crates/logic/output_gate.rs`).

Agents and similar workloads depend on this. A repairai turn waits tens of seconds on an LLM while the same session cell keeps serving status requests and event streams.

**Question:** how do we keep the same property when cells are wasm components instead of V8 isolates?

**Decision recorded:** use the **component-model async** support in WASI 0.3, as implemented by wasmtime. The guest-event-loop fallback on WASI 0.2 (a long-lived `run()` export multiplexing events through pollables) is **not needed**.

## 2. Platform and toolchain status (checked 2026-10-14)

| Item | Status |
|---|---|
| **WASI 0.3.0** | Ratified 2026-06-11. 0.3.1 shipped 2026-08-11; 0.3.2 is planned for 2026-10-13 (wasi.dev roadmap). Patch releases come about every two months. Component Model 1.0 is still pending. |
| **Wasmtime** | **46 (2026-06-22)** turned on WASI 0.3 and the `component-model-async` wasm feature by default. 41–45 ran release-candidate snapshots behind flags. |
| **Known runtime bug class** | CVE-2026-27195 (GHSA-xjhv-v822-pf94): a host panic when an async export's future is dropped before it finishes and the same instance is called again. Fixed in 40.0.4, 41.0.4 and 42+. **Our cancel-by-drop paths must be tested** (§3.5). |
| **Rust guests** | **Ready.** `wit-bindgen` maps `async func` to `async fn`. |
| **Python guests** | **Usable, young.** componentize-py has async-lifted exports (since 0.19.3) and WASIp3 examples. The latest release is 0.25.1 (11 September). A jco maintainer named componentize-py as a supported way to build async components (2026-10-01). |
| **JavaScript guests** | **Not yet.** componentize-js **does not support async exports** (jco maintainer, 2026-10-01, jco issue #2189), and no timeline was given. A Rust rewrite of componentize-js (wit-dylib based) is planned. StarlingMonkey 0.30 shipped in March 2026. Until async lands, JS guests can only use synchronous exports, so no interleaving within a cell. |
| **Go guests** | componentize-go has a WASI 0.3 example using goroutines. Maturity unclear. |
| **C/C++ guests** | Supported with a recent wasi-sdk (named by the jco maintainer as an async option) |

**Consequence:** the platform can be built on async now. Rust apps get full interleaving immediately. Python apps get it with a young toolchain. **JS apps must wait** for componentize-js async, or run without interleaving.

## 3. Design

### 3.1 How it works, and who does what

```
             host (ours)                         wasmtime (generic)                 guest (app)
request ──▶ admission + input-gate policy ──▶ start task on the cell's instance ──▶ async fn on_request()
                                                 │  task runs until it awaits          │  storage calls: sync, atomic
                                                 │  a host future/stream/timer          │  await fetch / sleep / stream
                                             ◀───┘  then another task may run      ◀────┘
egress ◀── output gate (durability proof) ◀── host import implementations (http, streams)
```

**Generic (wasmtime and the component model):**
- async exports and imports;
- **tasks**: several concurrent calls into one component instance;
- futures and streams as first-class types;
- **backpressure**, which lets a component stop accepting new tasks;
- cancellation;
- cooperative scheduling within the single-threaded instance.

**Ours (the policy layer):**
- which events may start a task, and when;
- the per-cell concurrency cap;
- critical sections (the `blockConcurrencyWhile` equivalent);
- binding each egress to the output gate;
- timeouts and cancellation;
- eviction accounting.

**Ours (the guest SDK):**
- the app-facing API for critical sections and storage;
- thin wrappers over generated bindings.

### 3.2 Exports

The cell world (doc 01 §3.3) exports its handlers as async functions:

```wit
interface handler {
  on-request: async func(req: incoming-request) -> result<response, error-code>;
  on-alarm:   async func(scheduled-ms: u64, attempt: u32) -> result<_, string>;
}
```

Each call is one task. Storage imports (`celld:storage`) stay **synchronous**, because they are local SQLite. Network, timer and stream imports are async.

### 3.3 Input-gate policy

| Rule | Behaviour | Mechanism |
|---|---|---|
| Interleave at awaits | A new task may start whenever every running task is suspended on an await | Default component-model task scheduling |
| Storage atomicity | A synchronous storage call or transaction runs to completion before any other task resumes | Synchronous imports. No await inside a transaction (the SDK enforces this). |
| Critical section (`block_concurrency(fn)`) | No new task starts, and suspended tasks don't resume, until `fn` finishes | Component **backpressure** blocks new tasks. Suspended tasks are held by the host (their awaited futures aren't delivered) until the section ends. |
| Per-cell concurrency cap | At most N tasks in flight (celld: 64); above that, return 503 with an overload header | Host admission before starting a task |
| Fairness | A CPU-heavy task can't starve the others | Epoch-interruption yield (doc 01 §3.5) |

### 3.4 Output gate per task

- **Cell-wide (as in celld):** after any unproven write, *every* egress from the cell waits for the durability proof. This is the simplest correct rule: a task that read another task's not-yet-durable data cannot leak it.
- **Possible later refinement:** track per-task read dependencies to release unrelated responses earlier. Not needed for v1.

### 3.5 Cancellation and timeouts

- **A client disconnect** cancels the task, by dropping its future on the host side. **Timeouts** do the same.
- **CVE-2026-27195 lesson:** keep wasmtime ≥ 42 (we pin ≥ 46), and include "drop an unfinished task, then call the same instance again" in the test suite.
- **A cancelled task's partial writes:**
  - committed transactions stay committed (they were atomic);
  - nothing is half-applied, because storage calls are synchronous.
- **`celld-steps` (doc 08)** relies on cancel-by-drop for timed-out attempts. This is the same path, so it is covered by the same tests.

### 3.6 Interaction with eviction (doc 01 §3.10)

An instance is busy while **any task** is running or suspended. The eviction predicate counts live tasks per store, plus the non-durable resources they hold. Host-held SSE streams (doc 02 Model A′) and alarms don't count.

### 3.7 Guests without async (JS today)

A component without async exports still works: each call runs to completion, and calls to that cell are serialized (one at a time). The platform needs no special mode for this, because the host simply never has more than one task to run. When componentize-js gains async, the same app gains interleaving after a rebuild.

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| A1/A3 runtime | The V8 turn/IoContext machinery is replaced by component-model tasks on a per-cell store (doc 01) |
| C1 input gate | `logic/gate.rs` semantics carry over to the policy layer (§3.3). celld's V8-specific gate claims and the cross-event lifecycle (`js/input_gate_lifecycle.rs`, cell gates in `js.rs`) are not built. |
| D3 output gate | Unchanged rule; applied at host imports (doc 01 §3.8), cell-wide (§3.4) |
| D1 lifecycle | The eviction predicate counts live tasks (§3.6) |
| doc 08 `celld-steps` | Cancel-by-drop for timeouts is covered by §3.5 tests |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

Code lines; estimates are marked *(est.)*. **Overlap:** celld's V8 concurrency machinery (input-gate claims, IoContext, turn scheduling in `js.rs`, `js/input_gate_lifecycle.rs`) is already counted as not built in doc 01 and in the README's combined estimate. This doc adds only the new policy layer.

| | Production | Test | Notes |
|---|--:|--:|---|
| Not built: V8 interleaving machinery | (counted in doc 01) | – | For example `js/input_gate_lifecycle.rs` (191 code lines) and the cell-gate parts of `js.rs` |
| Kept as the reference: input-gate decisions (`crates/logic/gate.rs`, 145 lines in celld) | carried into the policy layer | – | Pure logic, reusable as-is |
| **Added:** policy layer: task admission and concurrency cap, critical sections via backpressure, cell-wide output-gate binding per task, timeouts and cancel-by-drop, live-task accounting for eviction | **+200 to +500** *(est.)* | +300 to +600 *(est.)* | Includes the CVE-2026-27195 regression scenario |
| Added: guest SDK helpers (`block_concurrency`, transaction guard that rejects awaits) | in the app-side SDK, +50 to +150 *(est.)* | +50 to +100 | Rust first |
| **Net (platform)** | **+200 to +500** | +300 to +600 | Small, because the engine does the scheduling |

### 5.2 Effect on the combined estimate

The README's combined estimate gains one row: **+200 to +500** platform production (doc 11). The guest SDK helpers are app-side and not counted in the platform total.

## 6. Risks and open questions

**Risks**
1. **The async runtime code is young** (CVE-2026-27195). Pin wasmtime ≥ 46, track security releases, and keep the cancellation tests.
2. **JS guests have no interleaving** until componentize-js supports async (no timeline as of 2026-10-01).
3. **Deadlocks in critical sections.** A `block_concurrency` section that awaits something only another task can provide never finishes. Mitigate with a timeout on critical sections, and a reset of the cell, as celld does after 30 s.
4. **Spec churn within 0.3.x** (patches about every two months). Pin, and upgrade deliberately.

**Open questions**
- The critical-section timeout default (celld: 30 s).
- Whether per-task output gating (§3.4 refinement) is ever worth it.
- Whether the policy layer exposes per-cell concurrency caps to apps, or keeps them as an operator setting only.

## 7. Build order

1. Async `on-request` / `on-alarm` exports in the cell world, on wasmtime ≥ 46; Rust guest example with two interleaving requests.
2. The policy layer: admission cap, cell-wide output gate per task, cancellation and timeouts, with the CVE scenario test.
3. Critical sections via backpressure, plus the SDK's transaction guard.
4. Live-task accounting in the eviction predicate.
5. A Python guest example (componentize-py). Re-check componentize-js async before planning JS guests.

## 8. Next steps

- [ ] Prototype two interleaving requests and one critical section on wasmtime 46+.
- [ ] Write the cancellation test matrix: client disconnect, timeout, eviction during suspension, drop-then-call.
- [ ] Track componentize-js async (the upstream issue referenced from jco #2189) and componentize-py releases.
