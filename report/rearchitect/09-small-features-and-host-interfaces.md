# 09 — Small features and host-interface decisions

**Status:** Living document. Each item is marked **Decided**, **Open** or **Deferred**.
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Builds on:** [01 — Wasm-only runtime](01-wasm-only-runtime.md) (the WIT capability world)

---

## 1. Premise

The larger docs (01–08) leave a set of small decisions: individual host interfaces, conventions, and features too small for a doc of their own. This doc collects them in one place, so each can be decided quickly and none is forgotten.

**The rule for adding a host interface.** Add a `celld:*` interface only when the host must hold something the guest must not:
- a secret or credential;
- a shared resource such as a connection pool or cache;
- a durability guarantee.

Everything else is a guest library or a standard WASI interface (see the B-section mapping in the [README](README.md)).

## 2. Decision register

| # | Item | Status | Decision / recommendation | Production LOC *(est.)* |
|---|---|---|---|--:|
| 9.1 | **Cell-to-cell calls (A4)** | **Decided** | REST over HTTP, like any external call (§3.1) | +100 to +300 |
| 9.2 | Python guests (A6) | **Decided** | A guest toolchain (componentize-py), not a platform feature | 0 |
| 9.3 | `celld:crypto` (managed keys, signing) | Open | Not needed for algorithms (guest crates). Needed only for host-held keys: signing tokens, KMS-backed keys. | +200 to +500 if built |
| 9.4 | `celld:cache` (per-node response or object cache) | Deferred | Build only when a workload needs it. Apps can cache in cell SQLite or memory. | +200 to +400 if built |
| 9.5 | `celld:blob` (object store with host-held credentials) | Open | Useful because R2 is not built (doc 07). Recommend a thin interface over the fleet object store with a per-app prefix. | +300 to +600 if built |
| 9.6 | `celld:postgres` (pooled Postgres) | Decided in doc 05 | The wasm-runtime path (doc 05 Option C) | counted in doc 05 |
| 9.7 | `celld:pubsub` (SSE gateway, Model B) | Deferred to doc 02 Model B | Build with the gateway, if needed | counted in doc 02 |
| 9.8 | **Secrets** | Open, **needed** | celld has no secrets support (plain `vars` only; report E1). Recommend host-held secrets, injected into the WIT world as `celld:config/secrets.get(name)`, from node configuration or a KMS. | +200 to +400 |
| 9.9 | App configuration (non-secret) | Open | `wasi:config` (store) if stable when building starts, otherwise a small `celld:config` | +50 to +150 |
| 9.10 | Logging / console | Open | `wasi:logging` (or stderr captured per invocation) feeding the node's telemetry and the invocation reports (doc 04 §3.10) | +100 to +200 |
| 9.11 | Clock policy | Open | Freeze or coarsen guest time during execution (Spectre timer hardening, 01 §3.2.2), or expose real time. Decide per tenancy model. | +20 to +60 |
| 9.12 | `on-deploy` lifecycle hook | Decided in doc 06 | A generic hook, also used for migrations and warm-ups | counted in doc 06 |
| 9.13 | DNS and egress policy | Open | Host-side allowlist per app grant (01 §3.3), applied in `wasi:http` and `wasi:sockets` | part of the doc 01 embedding |

**Counted in the README's combined total:** only **Decided** items whose code isn't counted elsewhere, i.e. **9.1**. Open items are listed with estimates, so they can be added once decided.

## 3. Details

### 3.1 Cell-to-cell calls via REST (A4), decided

**Decision.** Cells call each other the way they call any external service: **HTTP requests** through `wasi:http`.
- There is no typed RPC layer, no stub objects, and no structured-clone envelope.
- celld's A4 (service bindings and JS RPC, 2,666 production lines) is not built.
- **Idempotency is the responsibility of the app code.** The recommended convention is an `Idempotency-Key` header, plus a `key → result` table in the callee's SQLite. That is repairai's pattern, which `celld-steps` provides as an optional extension (doc 08 §3.8).

**What the platform still provides**, about 100–300 lines:
1. **Addressing.** A stable internal URL scheme per cell, for example `http://<class>.<app>.cells.internal/<instance>/…`. The host resolves it to the owner node through the existing routing (D5). External callers use the public ingress as usual.
2. **Local fast path.** If the target cell is owned by the same node, the host delivers the request in-process. It keeps HTTP semantics but skips the network hop.
3. **Authentication of internal calls.** The host stamps the caller's cell identity in an unforgeable header, for example `celld-caller: <scope>`, signed or set by the host and stripped from external requests. Apps can then authorise per caller. Internal URLs must not be reachable from the public ingress.
4. **Durability.** Like every egress, a call waits for the caller's durability proof (the output gate), so a callee never acts on state the caller could lose.

**Consequences.**
- No Durable Object stubs or RPC methods: calls are request/response with app-defined payloads (JSON, protobuf, …).
- **Retries.** Without an idempotency key, a retry after an ambiguous failure can run twice. The app decides; `celld-steps` helps.
- No cancellation propagation beyond HTTP connection close. Apps needing it pass their own request IDs.

### 3.2 Python guests (A6), decided

Python apps are built with componentize-py (or a successor) into a wasm component that targets the same `cell` world. The platform has no Python-specific code. celld's A6 (309 production + 583 tools) is not built.

**Costs:**
- interpreted performance;
- tens of MB per instance, mitigated by pre-initialised snapshots at build time (01 §3.9).

### 3.3 Secrets (9.8), open but needed

- The guest asks for a secret by name through a host interface. The value never appears in the deployment manifest or the bucket.
- **Sources,** in order of preference: a KMS, a node-mounted secret file, or environment variables on the node.
- A grant lists the secrets an app or child cell may read; children get a subset (doc 04 §3.6).
- Log redaction: the host never logs secret values.

### 3.4 `celld:blob` (9.5), open

- With R2 not built, apps need an object store. Either:
  - **apps bring their own S3 credentials** (a secret, 9.8), and use an S3 library over `wasi:http`; or
  - **the host offers `celld:blob`**, a get/put/list/delete interface on a per-app prefix in a configured bucket, with credentials held by the host.
- **Recommendation:** start with apps bringing their own credentials (zero platform code). Add `celld:blob` if several apps need it.

## 4. Impact on the codebase

### 4.1 Code impact summary (LOC)

| | Production | Notes |
|---|--:|---|
| Not built: A4 service bindings and JS RPC | (counted in the README combined total under the runtime and V8 rows) | 2,666 production lines in celld |
| Not built: A6 Python platform support | (counted likewise) | 309 production + 583 tool lines in celld |
| **Added: 9.1 cell-to-cell REST** (addressing, local fast path, caller identity) | **+100 to +300** *(est.)* | Counted in the combined total |
| Open, if decided: 9.3 crypto, 9.5 blob, 9.8 secrets, 9.9 config, 9.10 logging, 9.11 clock policy | +870 to +1,910 *(est.)* in total | **Not** counted until decided. 9.8 secrets (+200 to +400) is effectively required. |
| Deferred: 9.4 cache | +200 to +400 *(est.)* | Not counted |

## 5. Next steps

- [ ] Decide 9.8 (secrets) and 9.9 (config) together. They define how apps get any configuration at all.
- [ ] Decide 9.3, 9.5, 9.10 and 9.11 alongside the area D and E review.
- [ ] Specify the internal URL scheme and the caller-identity header for 9.1.
