# 16 — Operations and tooling (area E)

**Status:** Decided, except the open questions in §4. Kubernetes packaging is a recorded task, deferred.
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). Baseline: celld v0.6.2 (`90b4301`).
**Builds on:** [01](01-wasm-only-runtime.md) (code-trust modes), [09](09-small-features-and-host-interfaces.md) (secrets, logging), [10](10-drop-peer-auth-simplify-deployment.md) (deployment record, mTLS), [14](14-lifecycle-placement-and-object-store.md) (shutdown, readiness, probe, crash reports). The cell browser UI is [17](17-cell-browser-ui.md).

---

## 1. Scope

| Feature | celld production | celld tools | celld readiness |
|---|--:|--:|---|
| E1 `deploy` | – | 1,051 | Solid |
| E2 `dev` | – | 1,282 | Solid |
| E3 Operator CLI & `diagnose` | 172 | 1,239 | Solid |
| E4 Node process, CLI & operator API | 2,276 | – | Solid |
| E5 Telemetry | 1,367 | – | Beta |
| E6 Managed control plane (celld.dev) | 2,266 | 342 | Experimental |
| **Total** | **6,081** | **3,914** | |

E3 is net of the 59 production and 190 tool lines that doc 07 already counts as not built: the operator route into cells and `operator_cell.rs`.

## 2. Decisions

| Feature | Decision |
|---|---|
| **E6** celld.dev | **Not built.** Self-hosted only, so there are no presence WebSocket, managed credentials, credential re-exec or row explorer. Browsing cell data is doc 17, with its own access controls. |
| **E1** `deploy` | **The CLI writes to the bucket directly.** It uploads the component, manifest and grants as immutable objects (doc 10), then moves `deploy/current.json` with a **conditional write**, so a concurrent deploy fails instead of silently winning. **Rollback** moves the pointer back to an earlier version; `deploy list` shows the history. The CLI accepts a prebuilt `.wasm`; `build` is a thin wrapper over `cargo` / componentize-py. The deployer needs bucket write access only to `deploy/`. In the `signed` code-trust mode, nodes reject unsigned code even if someone can write there. Secrets are referenced by name, never uploaded (doc 09 §9.8). |
| **E2** `dev` | One local node, a file watcher, rebuild on change and hot swap through D9 (doc 10). A built-in file-backed object store by default, so no Docker is needed; MinIO as an option. |
| **E3** Operator CLI | Commands: `cells` (list, inspect, release from quarantine), `nodes`, `diagnose` (doc 14's bucket probe, plus a peer reachability check through the operator API). The CLI reads the bucket (claims, node list) and calls the node operator API. **celld's Ed25519 signed peer probe is not built:** mTLS already proves node identity (doc 10). |
| **E4** Node process | Startup split into small stages instead of one 2,140-line function. **Configuration:** a typed file plus env overrides, with unknown keys rejected and values validated at startup. Test hooks only behind a Cargo feature. Shutdown and readiness per doc 14, with the shutdown budget checked against the pod's grace period. An `accept()` error backs off instead of exiting. **The operator API** (JSON + SSE, shared by the CLI and the doc 17 UI) binds to the private interface and authenticates callers by mesh identity (mTLS). |
| **E5** Telemetry | **Standard OpenTelemetry, for the platform and for app code.** Details in §3. celld's bucket Parquet sink and hand-written OTLP encoder are not built. |
| **Kubernetes packaging** | **Task recorded, deferred:** a plain StatefulSet manifest (PVC template, `PodDisruptionBudget`, `preStop`, probes, grace period, mesh annotations). No custom Kubernetes operator. |

## 3. Telemetry (E5)

**Platform:**
- The OpenTelemetry Rust SDK (with `tracing-opentelemetry` for spans and logs) exports traces, metrics and logs over **OTLP to a collector**. The collector handles storage, sampling and Prometheus exposure.
- Data is flushed on shutdown and buffered with a bound while the collector is down; it never blocks requests.
- **Performance instrumentation** across the request path:
  - request latency by phase: ingress, forward, activation wait, guest, durability;
  - activations (warm or cold) and their duration;
  - fsync latency, bundle upload lag and size;
  - bucket operation latency, errors and `429`/`503` responses;
  - pressure level and memory per class;
  - forwarded and held requests;
  - instantiation time, epoch interruptions, crashes and quarantines (doc 14).
- **Cardinality rule:** metric labels are class, version, node, phase or status, never a cell ID. Cell IDs go on spans and logs only.

**App code (wasm):**
- **Host interface:** `wasi:otel` (a Phase 1 WASI proposal; experimental in Spin 3.6, and implemented by the `omnia-wasi-otel` crate) if it is stable enough when building starts. Otherwise a small `celld:otel` that mirrors it, swapped for `wasi:otel` later.
- **The host joins guest spans to the request's trace:** it accepts `traceparent` on incoming requests and injects it into outgoing `wasi:http` calls.
- **The host adds resource attributes:** app, class, code version, node.
- **Per-cell limits:** rate and attribute-cardinality caps, so an app can't overload the platform's pipeline.
- **Guest SDK (app-side):** a Rust crate that bridges the standard `opentelemetry` / `tracing` APIs to the host interface, so apps use the normal libraries. Python follows.
- Logging (doc 09 §9.10) uses the same pipeline as OTel logs. Crash reports (doc 14) and invocation reports (doc 04) are emitted as OTel events.

## 4. Open questions

1. `wasi:otel` or `celld:otel` first? This depends on the proposal's state when building starts.
2. Whether `dev` needs a multi-node mode to test forwarding and claims locally.
3. Local Postgres in `dev` for apps that declare it (doc 05).

## 5. Impact on the codebase (LOC)

All additions are estimates. The code-trust deploy flags, the dynamic-cell CLI and the `on-deploy` manifest are already in the README's tools row (+320 to +700).

| | Removed | Added *(est.)* |
|---|--:|--:|
| **Production** | | |
| E4: startup stages 400–800; config 150–300; node CLI 100–200; operator API 200–400; async runtime helpers 200–400; shutdown wiring 100–200; health endpoints 50–100 | −2,276 | +1,200 to +2,400 |
| E5: SDK setup, export and flush 150–300; platform instrumentation 400–900; app telemetry host interface 300–600 | −1,367 | +850 to +1,800 |
| E3: server-side signed probe | −172 | 0 |
| E6: celld.dev | −2,266 | 0 |
| **Production total** | **−6,081** | **+2,050 to +4,200** (net ≈ −1.9k to −4.0k) |
| **Tools** | | |
| E1 `deploy` (upload, conditional pointer, rollback, history, build wrapper) | −1,051 | +500 to +900 |
| E2 `dev` (watcher, supervisor, local store) | −1,282 | +800 to +1,300 |
| E3 operator CLI | −1,239 | +500 to +900 |
| E6 deploy agent | −342 | 0 |
| **Tools total** | **−3,914** | **+1,800 to +3,100** (net ≈ −0.8k to −2.1k) |
| **App-side library:** guest telemetry SDK (Rust) | – | +200 to +500 |

Deferred, not counted: the StatefulSet manifest (YAML).

## 6. Build order

1. E4 node skeleton: startup stages, typed config, health endpoints, OTel export of platform traces, metrics and logs.
2. E1 `deploy` to the bucket, with the conditional pointer and rollback.
3. E2 `dev` with the built-in local store.
4. Platform instrumentation, added alongside each subsystem as it is built.
5. The operator API and E3 commands, then the doc 17 UI on the same API.
6. App telemetry: host interface plus the Rust guest SDK.
7. Deferred: the StatefulSet manifest.
