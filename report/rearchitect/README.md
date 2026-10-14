# celld — Re-architecture explorations

This folder records architectural changes we have explored relative to celld. Each document covers one change: the design and the trade-offs, plus its impact measured against celld's codebase. It is an exploration, not a commitment.

**Context: a green-field app inspired by celld.** We are designing a **fresh application** that takes celld as its reference architecture. We are not modifying celld, and nothing built on these designs runs in the wild yet. Therefore:
- **No migration, deprecation or backward compatibility is needed.** There are no existing users, deployments, data or config keys to carry over. A feature that is "dropped" is simply never built.
- **"Integration with the existing engine"** means how the change fits celld's architecture (areas D and E), which we otherwise reuse as the reference design.
- **Code impact** is measured against celld v0.6.2:
  - "removed" means celld code the new app does **not** need to build;
  - "added" means new code the new app needs beyond celld's equivalent.

The baseline for every document is the feature, size and readiness report in [`../celld-feature-report.md`](../celld-feature-report.md) (celld v0.6.2, commit `90b4301`). Feature IDs such as A1, B5 or D3 refer to that report's taxonomy. Line counts are code lines from that report.

## Combined code estimate (all Index items implemented)

The per-document nets in the Index **overlap** and must not be summed. For example, doc 01 already removes all runtime JavaScript, which includes the JS halves of the services that docs 05, 07 and 08 drop. The totals below were therefore recomputed from the baseline line map. Every celld production and tool line gets exactly one fate: **kept** (reused as the reference design), **not built**, or **replaced** by new code.

**Assumptions:**
- the wasm-only runtime (01), with no JS/V8 guest engine;
- **D2, D3 and D4 replaced by doc 12 V9 (decided)**, base estimate; optional takeover mode and incarnation (later) not counted;
- **D5 replaced by doc 13 C1 (decided):** K1 `X-Cell` header, any-node forward, HTTP/2 between nodes over mTLS; K4 (later) not counted;
- SSE Model A only (02);
- Postgres through a host capability (05, Option C);
- **area D fully reviewed** (docs 10–14): D1, D6 and D7 replaced by doc 14 (base estimate; its "later" items not counted);
- **area E reviewed** (doc 16): E6 not built; E1–E5 replaced; the StatefulSet manifest deferred and not counted;
- the cell browser UI (17), a **new feature**, counted in production (backend and frontend);
- change data capture to Kafka through the bucket (15), a **new feature that celld doesn't have**; the range covers both capture mechanisms (its one open choice);
- Open items in doc 09 are not counted.

### Platform production code

| | Production LOC | Source |
|---|--:|---|
| **celld baseline** | **107,304** | report §3 |
| Not built: all runtime JavaScript and Python (harness, API polyfills, Node.js compat, service JS) | −29,838 | 01 (measured) |
| Not built: V8-specific Rust glue (`js.rs`, `js/*.rs`) | −11,584 | 01 (measured by file rule) |
| Replaced: V8 host and API Rust (isolate pool and runtime materialization, the HTTP stream registry, RPC, wasm and Python host parts) | −5,090 | 01 (measured) |
| Not built: WebSockets (B6 Rust plus the D3 frame gate) | −3,359 | 02 (measured) |
| Not built: KV, Queues, D1, R2, Static assets, Containers (Rust parts) | −7,931 | 05, 07 (measured) |
| Replaced: Facets + Dynamic Workers (Rust parts) | −2,411 | 04 (measured) |
| Moved to app libraries: Workflows + Cron (Rust parts) | −660 | 06, 08 (measured) |
| Not built: operator route into cells | −59 | 07 (measured) |
| Not built: D8 peer authentication | −367 | 10 (measured) |
| Not built: D8 signing plumbing at call sites | −50 to −150 | 10 *(est.)* |
| Replaced: D9 deployment adoption and generations (as in celld, including the legacy wake-format migration) | −1,337 | 10 (measured) |
| Replaced: D2 ownership + D3 replication + D4 fleet log (22,566, minus the 214 WebSocket frame-gate lines already counted above) | −22,352 | 12 V9 (measured) |
| Replaced: D5 routing and peer tunnel | −2,372 | 13 (measured) |
| Replaced: D1 lifecycle, D6 placement and pressure, D7 object store (4,074 + 4,060 + 1,161) | −9,295 | 14 (measured) |
| Not built: references to dropped features inside areas D and E | −300 to −600 | 02, 07 *(est.)* |
| Replaced or not built: area E production (E3 server probe 172, E4 node 2,276, E5 telemetry 1,367, E6 celld.dev 2,266) | −6,081 | 16 (measured) |
| **Subtotal not built or replaced** | **≈ −103.1k to −103.5k** | |
| Added: wasmtime embedding and WIT host implementations (glue and policy; bindings generated, WASI from `wasmtime-wasi`/`-http`) | +1,200 to +2,600 | 01 §5.4 *(est.)* |
| Added: code-trust modes (node side) | +500 to +850 | 01 *(est.)* |
| Added: eviction and capability decisions in the pure core | +200 to +400 | 01 *(est.)* |
| Added: SSE Model A (event log, gated stream writes) | +200 to +400 | 02 *(est.)* |
| Added: dual-stack HTTP/1.1 + h2c (net of replaced lines) | +130 to +310 | 03 *(est.)* |
| Added: dynamic cells (spawn/delete, code store, grants, stateless mode, parent mode; children are regular cells over REST) | +800 to +1,700 | 04 *(est.)* |
| Added: `celld:postgres` host capability | +500 to +1,200 | 05 *(est.)* |
| Added: generic `on-deploy` hook | +100 to +250 | 06 *(est.)* |
| Added: cell-to-cell REST (addressing, local fast path, caller identity) | +100 to +300 | 09 *(est.)* |
| Added: simplified D9 (deployment record and loading, generation and swap pump, adoption trigger, format marker and gate stub) | +620 to +980 | 10 *(est.)* |
| Kept: peer protocol-version check (moved to D5) | +20 to +40 | 10 *(est.)* |
| Added: cell concurrency policy layer on component-model async (admission, critical sections, output gate per task, cancellation) | +200 to +500 | 11 *(est.)* |
| Added: V9 durability and ownership (bundle stream, per-cell snapshots, lazy restore, claims, node list, cleanup, rebalancing, durability levels) | +3,170 to +6,810 | 12 §9.9 *(est.)* |
| Added: D5 routing C1 (forwarder, owner cache over claims, HTTP/2 peer transport, retry rule, `X-Cell` extraction and fence) | +900 to +1,700 | 13 *(est.)* |
| Added: D1 lifecycle (state machine, executor with fast path, activation policies, crash handling and quarantine), D6 (pressure, placement and rebalance policies, shutdown modes, readiness), D7 (S3 and GCS, stricter probe) | +3,300 to +6,450 | 14 *(est.)* |
| Added, **new feature (not in celld)**: change data capture to Kafka through the bucket (capture, pending table, declarations, CDC section in bundles, CDC reader) | +1,200 to +2,400 | 15 *(est.)* |
| Added: area E (node process and typed config, operator API, OpenTelemetry for platform and apps) | +2,050 to +4,200 | 16 *(est.)* |
| Added, **new feature (not in celld)**: cell browser UI (event bus and SSE tail, fan-out, read-only row browsing, frontend) | +1,650 to +3,300 | 17 *(est.)* |
| **Subtotal added** | **≈ +16.8k to +34.4k** | |
| **Net** | **≈ −68.7k to −86.6k** | |
| **Resulting platform production code** | **≈ 20.7k to 38.6k** | **≈ 19% to 36% of celld** |

**What is kept** (≈ 4.6k lines): only the Durable Object and Alarm host code (4,568). Every area D and E feature is now replaced or not built.

### Tools, and app-side libraries

| | LOC |
|---|--:|
| Tools: celld baseline | 7,973 |
| Tools: not built (service CLIs and deploy steps, Python build path, operator CLI plumbing, the Workflows/Cron/Facets/Dynamic Workers deploy parts) | −3,949 (measured) |
| Tools: added (code-trust deploy flags, dynamic-cell CLI, `on-deploy` manifest, `[[cdc]]` declarations) | +370 to +850 *(est.)* |
| Tools: area E replaced (E1 `deploy` 1,051, E2 `dev` 1,282, E3 operator CLI 1,239, E6 deploy agent 342) | −3,914 (measured) |
| Tools: area E added (`deploy` to the bucket with rollback, `dev`, operator CLI) | +1,800 to +3,100 *(est.)* |
| **Tools: net** | **≈ −3.9k to −5.7k** (resulting ≈ 2.3k to 4.1k) |
| **App-side libraries** (linked by apps, not part of the platform): `celld-cron` (06) + `celld-steps` core (08) + guest telemetry SDK (16) | **+1.17k to +2.0k** *(est.)*; up to +2.7k with all `celld-steps` extensions |

### Resulting total code base

| | celld v0.6.2 (baseline) | New app (all Index items) | Change |
|---|--:|--:|--:|
| Platform production code | 107,304 | **≈ 20.7k to 38.6k** | ≈ −68.7k to −86.6k |
| Tools (CLIs, deploy, dev) | 7,973 | **≈ 2.3k to 4.1k** | ≈ −3.9k to −5.7k |
| App-side libraries (`celld-cron`, `celld-steps` core, telemetry SDK) | – | **≈ 1.2k to 2.0k** | +1.2k to +2.0k |
| **Total code base** | **115,277** | **≈ 24.1k to 44.7k** | **≈ −70.6k to −91.2k (≈ 21% to 39% of celld)** |
| Total, with all `celld-steps` extensions | 115,277 | ≈ 24.5k to 45.4k | ≈ −69.9k to −90.8k |

All figures are code lines, excluding comments and blanks. Adding the Open items from doc 09, if all are built, raises the total by about 0.9k to 1.9k.

**Not included:**
- tests (celld's suites are external to its repo; per-doc test estimates are in each doc);
- examples and docs;
- Open items in doc 09 (+0.9k to +1.9k if all are built; secrets, about +0.2k to +0.4k, is effectively required);
- SSE Model B (02);
- doc 14's "later" items (+0.3k to +0.7k);
- the StatefulSet manifest (doc 16, deferred).

## Feature disposition: A, B, C

Status per baseline feature, when all Index items are implemented.

| Feature | Status | What happens | Doc |
|---|---|---|---|
| **A1** V8 isolate engine core | **Evolves** | wasmtime engine: stores, epochs, `ResourceLimiter` | 01 |
| **A2** Module system & bootstrap | **Evolves** | Component loading, with code-trust modes and a local artifact cache | 01 |
| **A3** Cell runtime host & isolate pool | **Evolves** | Pooling allocator; one store per cell; eviction predicate | 01 |
| **A4** Service bindings & JS RPC | **Dropped** | Cells call each other over **REST (HTTP)**, like any external call. Idempotency is the app's responsibility. | 09 §3.1 |
| **A5** WebAssembly & workers-rs | **Evolves** (absorbed) | Wasm *is* the runtime | 01 |
| **A6** Python Workers | **Evolves** (out of the platform) | A guest toolchain (componentize-py); no platform code | 09 §3.2 |
| **B1** Fetch / Request / Response | **Evolves** | `wasi:http`, with host policy and the durability gate | 01 |
| **B2** Streams & encoding | **Dropped** | `wasi:io` streams plus guest libraries | see B mapping |
| **B3** Other web standards | **Dropped** | Guest libraries plus `wasi:clocks` | see B mapping |
| **B4** Web Crypto & node:crypto | **Dropped** | Guest crypto crates plus `wasi:random`; optional `celld:crypto` for host-held keys | 09 (9.3) |
| **B5** Node.js compatibility | **Dropped** | Not applicable; guests use their own standard library | 01 |
| **B6** WebSockets | **Dropped** | SSE plus POST is the real-time primitive; WebSockets stay an optional capability | 02, 03 |
| **B7** HTMLRewriter | **Dropped** | `lol_html` as a guest library | see B mapping |
| **B8** TCP sockets | **Evolves** | `wasi:sockets`, with a host connect policy | 01 |
| **B9** Cache API | **Dropped** | Optional `celld:cache`, deferred | 09 (9.4) |
| **C1** Durable Objects | **Stays** | The core primitive: a cell with its own SQLite, through `celld:storage`. Request interleaving and the input gate run on component-model async. | 01, 11 |
| **C2** Alarms & wake discovery | **Stays** | Durable timers; cron and steps build on them | 01, 06, 08 |
| **C3** Durable Object Facets | **Evolves** | Dynamic cells, `ownership: parent` | 04 |
| **C4** Workers KV | **Dropped** | Cell SQLite, Postgres or Redis; optional `celld-kv` library | 07 |
| **C5** Queues | **Dropped** | An external broker; optional `celld-queue` library | 07 |
| **C6** D1 | **Dropped** | Postgres (`celld:postgres`) | 05 |
| **C7** R2 | **Dropped** | The app's own S3, or an optional `celld:blob` | 07, 09 (9.5) |
| **C8** Workflows | **Evolves** (to an app library) | `celld-steps`: durable steps in the cell's SQLite | 08 |
| **C9** Cron Triggers | **Evolves** (to an app library) | `celld-cron` over alarms plus an `on-deploy` hook | 06 |
| **C10** Static assets | **Dropped** | A CDN or static host in front | 07 |
| **C11** Dynamic Workers & Tail Workers | **Evolves** | Dynamic cells (stateless and `own` modes), with invocation reports | 04 |
| **C12** Containers & Sandbox | **Dropped** | An external container platform | 07 |

**Totals:** 2 stay, 11 evolve, 14 dropped (of 27 A/B/C features). By area: A has 5 evolving and 1 dropped; B has 2 evolving and 7 dropped; C has 2 staying, 4 evolving and 6 dropped.

## Feature disposition: D (reviewed)

| Feature | Status | What happens | Doc |
|---|---|---|---|
| **D8** Peer authentication | **Dropped** | Node trust comes from the network: mTLS mesh (preferred) or a WireGuard/Tailscale overlay. The protocol-version check is kept in D5. | 10 |
| **D9** Deployment adoption and generations | **Evolves** | Deployment record (component + manifest + grants), a pure swap pump, a poll-or-watch adoption trigger; no legacy wake-format migration | 10 |
| **D1** Decision core & cell lifecycle | **Evolves** | About 12 states over V9's three levels (Running, Warm, Absent); one serial pure core with a fast path for running cells; crash handling with quarantine; pluggable activation policies. Concurrency per doc 11. | 11, 14 |
| D2, D3, D4 | **Evolves (decided: V9)** | Identity-owned disk (operator's choice), fsync per commit with app-chosen durability levels, per-node bundle stream + per-cell snapshots in the bucket, per-cell claims checked once per placement, node list for membership. Zombie protection by Kubernetes now (incarnation later); optional takeover mode. | 12 §9 |
| **D5** Routing and peer transport | **Evolves (decided: C1)** | `X-Cell` header as the cell key (an app rule from a reflectapi schema annotation possibly later); any node accepts and forwards once to the claim owner; HTTP/2 between nodes over mTLS; re-send only when provably not processed (`421` "not owner" before the handler). A load-balancer hash on `X-Cell` is an optional optimization. | 13 |
| **D6** Placement, pressure & drain | **Evolves** | Pressure from exact per-cell figures; first placement `claim-local`, then `pressure-aware`; rebalancing by dominant share; shutdown modes `in-place` / `hot` / `all` with ingress hold-and-retry (default mode open); no drain token (Kubernetes serializes restarts) | 14 |
| **D7** Object-store backends | **Evolves** | S3 and GCS; S3-compatible stores (MinIO first) for self-hosting and testing; Azure only if requested; a stricter, fail-closed probe | 14 |

## Feature disposition: E (reviewed)

| Feature | Status | What happens | Doc |
|---|---|---|---|
| **E1** `deploy` | **Evolves** | CLI writes to the bucket directly; conditional pointer write; rollback by moving the pointer; secrets by name only | 16 |
| **E2** `dev` | **Evolves** | One local node, watcher, hot swap; built-in file-backed store (MinIO optional) | 16 |
| **E3** Operator CLI & `diagnose` | **Evolves** | `cells`, `nodes`, `diagnose` over the bucket and the operator API; no Ed25519 signed probe (mTLS proves identity) | 16 |
| **E4** Node process & operator API | **Evolves** | Staged startup, typed config that rejects unknown keys, operator API on the private interface with mTLS identity. StatefulSet manifest: task deferred. | 16 |
| **E5** Telemetry | **Evolves** | Standard OpenTelemetry over OTLP for the platform (performance metrics throughout) and for app code (`wasi:otel` or a mirroring `celld:otel`) | 16 |
| **E6** Managed control plane | **Dropped** | Self-hosted only | 16 |

## New features (not in celld)

Features the new app adds that **celld does not have at all**. Their code is purely added; nothing in celld is replaced.

| Feature | Status | What it does | Doc |
|---|---|---|---|
| **Change data capture to Kafka** | **New** (explored). Decided: delivery through the bucket by a separate CDC reader (1–4 s latency), after-state only, per-app declarations, app outboxes kept separate. Open until built: the capture mechanism. | An app declares tables and included or excluded columns per cell class. The platform records changed keys in the same transaction; each node adds the rows' current values (or tombstones for deletes) to the V9 bundle it uploads anyway; a CDC reader follows the bundles and writes them to Kafka exactly once, keyed for compacted topics. Nodes never talk to Kafka. | 15 |
| **Cell browser UI** | **New** (proposed). Open: frontend stack, SQL console, app-defined views, column redaction. | Browse fleet, cells and cell state (read-only rows behind a separate, audited permission); watch live lifecycle, request and alarm events over SSE. Served by the operator API behind an authenticating proxy. | 17 |

## B-section mapping: where each runtime API comes from

In the wasm-only design, guests have no JavaScript globals. Each capability celld's B area provides comes from one of three places:
- a **WASI** standard interface, implemented by the host;
- a **guest library** compiled into the app;
- a **celld** host interface, where no standard exists.

| celld today | In the new app | Comes from |
|---|---|---|
| B1 fetch, Request/Response, bodies | `wasi:http/outgoing-handler` for outbound; incoming requests arrive as `incoming-request` resources | **WASI**, implemented by the host (policy, durability gate). The guest uses an HTTP types library. |
| B2 streams, compression, text encoding | `wasi:io` input/output streams. Compression through guest libraries (e.g. `flate2`). Text uses native strings (UTF-8). | **WASI** for streams; **guest libraries** for the rest |
| B3 URL, events, AbortSignal, structuredClone, performance | Language libraries (e.g. `url`, serde). Cancellation by dropping futures or resources. Time from `wasi:clocks`. | **Guest libraries** plus **WASI** clocks |
| B4 Web Crypto / node:crypto | Entropy from `wasi:random`; algorithms from guest crates (RustCrypto, ring) | **WASI** plus **guest libraries**. Optional `celld:crypto` for host-held keys (09). |
| B5 Node.js compatibility | Not applicable; guests use their own standard library | – |
| B6 WebSockets | SSE (02). WebSockets only as an optional `celld:websocket` capability. | **celld**, optional |
| B7 HTMLRewriter | `lol_html` compiled into the guest, streaming over `wasi:io` | **Guest library** |
| B8 TCP sockets | `wasi:sockets/tcp` with a host connect policy; TLS as a guest library (rustls) or on the host | **WASI** |
| B9 Cache API | Optional `celld:cache` (deferred) | **celld**, optional (09) |

**Trade-off.** celld's 30.5k-line B area mostly moves *into the apps*, as their own libraries. The platform maintains far less, and every guest gets its language's full ecosystem. In exchange, each app ships and patches its own stack. That's why security-sensitive pieces (TLS, HTTP egress, secrets) stay on the host.

## Open small decisions

Small host interfaces and conventions are tracked in [09 — Small features and host-interface decisions](09-small-features-and-host-interfaces.md):
- **Decided:** cell-to-cell calls over REST; Python as a guest toolchain.
- **Open:** secrets (needed), app config, logging, clock policy, `celld:crypto`, `celld:blob`.
- **Deferred:** `celld:cache`.

## Next

- **Area D review, done.**
  - Done:
    - D8 dropped and D9 simplified (doc 10);
    - cell concurrency on component-model async (doc 11);
    - **durability, restore and single-writer: decided, V9** (doc 12 §9);
    - **routing and peer transport: decided, C1** (doc 13);
    - cell lifecycle, placement and pressure, object store (doc 14). Open there: the default shutdown mode.
- **Area E review, done** (doc 16). Open: `wasi:otel` or `celld:otel` first; a multi-node `dev` mode; local Postgres in `dev`. Deferred task: the StatefulSet manifest.
- **Cell browser UI (doc 17), a new feature.** Open: frontend stack (htmx recommended), SQL console, app-defined views, column redaction.
- **All areas (A–E) are reviewed.** Remaining: the open decisions listed per doc, and the doc 09 open items.
- **Change data capture (doc 15), a new feature.** Delivery through the bucket is decided. The capture mechanism (generated triggers or a host hook) is deliberately left open until it is built.

## Index

| # | Change | Status | Constraint changed | Code impact (production LOC) | Summary |
|---|---|---|---|---|---|
| [01](01-wasm-only-runtime.md) | **Wasm-only application runtime instead of V8 isolates** | Explored (design sketch) | Drops Cloudflare Workers compatibility | Removed ≈ −39k to −40k; added ≈ +5.1k to +8.2k *(est.)*; **net ≈ −30.6k to −34.7k** (A+B+C: 65.2k → ~30.5k to 34.6k). See §5.4. | Run each cell as a wasmtime component in a host-defined WIT world, with capability-based isolation, hard memory and CPU limits, host-mediated networking and timers, and an eviction rule based on host-visible state. Code trust is configurable per node: `compile` (default), `signed` precompiled artifacts, or opt-in `unsigned` artifacts for closed fleets. |
| [02](02-sse-instead-of-websockets.md) | **Server-Sent Events instead of WebSockets** | Explored (design sketch) | Drops WebSockets as the default real-time primitive (and Cloudflare compatibility) | Removed ≈ −4.1k to −4.3k (3,961 measured); added +0.2k to +0.4k for Model A *(est.)*; **net ≈ −3.7k to −3.9k**. Model B adds +0.8k to +3k. See §5.1. | Default push is SSE plus POST, with a per-cell event log and `<epoch>-<seq>` IDs for lossless resume. Model A: the cell holds its streams and stays resident (accepted default). Model B: a pub/sub SSE gateway for fan-out and placement independence. WebSockets stay an optional capability. |
| [03](03-http2-ingress.md) | **HTTP/2 on the public edge** | Step 1: deployment requirement. Step 2: explored (design sketch). | Adds HTTP/2 alongside HTTP/1.1 (no protocol is dropped) | Step 1: **0** (load-balancer requirement, ~30 doc lines). Step 2: added +150 to +320 *(est.)*, replaced −10 to −20; **net ≈ +130 to +310**. See §5.1. | **Now:** TLS and HTTP/2 at the load balancer fix the SSE tab limit with no celld code. **Next:** dual-stack HTTP/1.1 + h2c on the public listener (hyper-util auto builder), with the `:authority` URL fix, HTTP/2 header filtering and h2 abuse limits. HTTP/1.1 stays for probes, health checks and h1-only clients. |
| [04](04-dynamic-cells.md) | **Dynamic cells (unifying Facets and Dynamic Workers)** | Explored (design sketch) | Replaces Cloudflare's `ctx.facets` and `env.LOADER` with one primitive (builds on 01) | Removed −2,897 (C3 + C11); added +0.8k to +1.7k *(est.)*; **net ≈ −1.2k to −2.1k**. See §5.1. | Runtime-supplied code becomes a cell with its own SQLite, attenuated grants, limits and durable code. Two ownership modes, both required: `parent` (storage isolation, co-located, cheap) and `own` (a full cell that scales out). Stateless children replace Dynamic Workers. Decision rule: tables first, then a parent-mode child, then an own-mode child. |
| [05](05-drop-d1-use-postgres.md) | **Drop D1 (C6); use Postgres for shared relational data** | Explored (design sketch) | Drops Cloudflare D1 compatibility | Not built ≈ −1,150 production, −600 tools, −35 test, −49 examples; added +50 to +150 *(est.)* (Option A: TCP-socket hardening); **net ≈ −1.0k to −1.1k production, −0.6k tools**. Option B (Hyperdrive-style pool) would add +1.5k to +3k instead. See §5.1. | Per-entity state lives in cell SQLite; shared app data lives in external Postgres. Option A: drivers over TCP sockets with an external pooler. In the wasm design: a `celld:postgres` host capability with pooling. D1 is simply never built. |
| [06](06-cron-as-library.md) | **Cron Triggers as an app library on top of alarms** | Explored (design sketch) | Drops the `triggers.crons` config key (builds on 01) | Platform: −615 removed or moved out, +100 to +250 for a generic `on-deploy` hook; **net platform ≈ −365 to −515**. Library: +416 moved, +250 to +500 new *(est.)*. **Overall ≈ +0.15k to +0.55k.** See §5.1. | The platform keeps only alarms plus a generic `on-deploy` hook. The pure cron logic moves to a `celld-cron` crate (or composable component) linked into the app, which owns a scheduler cell. It gains persisted retries, configurable catch-up and overrun, and optional time zones. |
| [07](07-drop-kv-queues-r2-assets-containers.md) | **Drop KV, Queues, R2, Static assets and Containers (C4, C5, C7, C10, C12)** | Explored (decision recorded) | Drops five Cloudflare service bindings | Not built: −10,009 production, −2,274 tools, −320 test (measured), plus the operator route and `operator_cell.rs` (−59 / −190; D1, KV and Queues are all gone). Keeps +80 to +150 of `bucket.rs` helpers. **Net ≈ −10.1k to −10.4k production (9.4%–9.7% of all), −2.5k tools.** See §5.1. | Use external services: Postgres or Redis, a broker, S3, a CDN, a container platform. KV and Queue logic can live on as optional app libraries (doc 06 pattern). These features are simply never built. |
| [08](08-workflows-as-durable-steps.md) | **Workflows as durable steps over SQLite-persisted steps** | Explored (design sketch, grounded in repairai) | Drops the Cloudflare Workflows API and the `workflows` config key | Platform: −1,454 production, −83 tools, −141 test (measured). Library core: +300 to +600 *(est.)*, comparable to repairai's existing ~560 lines. Extensions bring it to +650 to +1,300. **Overall: core ≈ −0.85k to −1.15k; with all extensions ≈ −0.15k to −0.8k.** See §5.1. | repairai's variant B as a `celld-steps` library: a step journal, runs, events and an outbox in the entity's own SQLite. Commits atomically with business state; timers multiplexed onto the cell alarm; attempts counted before they run; timed-out attempts cancelled; drift detection with code versions and patch markers. Portable to any Durable Object runtime. |
| [09](09-small-features-and-host-interfaces.md) | **Small features and host-interface decisions** | Living register (Decided / Open / Deferred) | Cell-to-cell calls become plain REST; small host interfaces decided one by one | Decided: +100 to +300 for cell-to-cell REST *(est.)*. Open items: +0.87k to +1.91k if all are built (not counted). See §4.1. | A4 RPC is replaced by HTTP between cells (idempotency up to the app); Python is a guest toolchain. Secrets (needed), config, logging, clock policy, `celld:crypto`, `celld:blob` are open; `celld:cache` is deferred. |
| [10](10-drop-peer-auth-simplify-deployment.md) | **Drop D8 (peer authentication); simplify D9 (deployment adoption and generations)** | Explored (D8 drop decided; D9 sketched) | Node trust moves to the network layer (mTLS mesh or overlay); no legacy formats | D8: −367 measured, −50 to −150 plumbing, +20 to +40 version check, **net ≈ −0.38k to −0.50k**. D9: −1,337 measured, +620 to +980 new, **net ≈ −0.36k to −0.72k**. See §5.1. | The first area D review doc. D8 is replaced by mTLS between nodes. D9 keeps its guarantees (requests finish on their generation; a cell never runs on two versions) with a wasm-sized deployment record, a pure swap pump, a poll-or-watch adoption trigger, and no legacy wake-format migration. |
| [11](11-cell-concurrency-component-model-async.md) | **Cell concurrency on wasm: component-model async** | Decided (policy layer sketched) | Interleaving moves from V8's event loop to wasmtime's component-model tasks (WASI 0.3, wasmtime ≥ 46) | Added **+200 to +500** platform *(est.)*; celld's V8 interleaving machinery is already counted as not built in 01. See §5.1. | Requests to a cell interleave at await points, with sync SQLite. Our policy layer adds admission caps, critical sections via backpressure, a cell-wide output gate, and cancel-by-drop (tested against CVE-2026-27195). Rust is ready, Python is usable (componentize-py), and JS async is not available yet (componentize-js). |
| [12](12-durability-and-single-writer-options.md) | **Durability, restore and single-writer options** | **Decided: V9** (catalogue kept as reference) | Replaces celld's bucket-only replication and ownership (D2, D3, D4) with one chosen variant | Against D2+D3+D4 (22,566 measured): **V9 (decided) −15.8k to −19.4k** (identity-owned disk + per-node bundle stream); **V4 −19.3k to −21.8k** (replicated volume + gated flush) … **V3 −9.6k to −15.6k** (own quorum). **V9 is in the combined total.** See §10. | Catalogue of durability (R1–R8 with commit and no-loss latencies), restore (S1–S4) and single-writer (L1–L5) options; valid pairings; variants V0–V8 with RPO by failure type; a generic "replicated volume with exclusive attach" contract (GKE Hyperdisk HA today, LINSTOR/DRBD, Mayastor or Longhorn self-hosted); the gated-flush design (~0.3k–0.8k lines). **V9 decided:** with an operator-chosen disk, fsync per commit, app-chosen durability levels, per-node bundles plus per-cell snapshots, claims checked once per placement, membership via a node list, zombie protection by Kubernetes now (incarnation fencing as a later feature), optional takeover, and how Kubernetes handles a down identity. |
| [13](13-routing-ingress-and-peer-transport-options.md) | **Routing, ingress and peer transport (D5)** | **Decided: C1** (catalogue kept as reference) | The edge identifies the cell (`X-Cell`); peer trust from mTLS; WebSocket splicing gone | Against D5 (2,372 measured): **C1 −0.7k to −1.5k** (+0.9k to +1.7k new) *(est.)*. **C1 is in the combined total.** K4 later: +150 to +400, not counted. See §7. | **Decided:** K1 header cell key, with K4 (an app rule from a reflectapi schema annotation) possibly later; O1 any-node ingress with one forward hop to the V9 claim owner; T2 HTTP/2 between nodes over the mTLS mesh; re-send only when provably not processed (connect failure, `REFUSED_STREAM`/`GOAWAY`, `421` before the handler), which fixes celld's pooled-tunnel resend risk. A consistent hash on `X-Cell` at the load balancer is an optional optimization (Envoy, ingress-nginx, HAProxy; GKE support to check). The rejected options (router tier, ring-aligned ownership, redirects, HTTP/1 tunnels, HTTP/3) are kept as reference. |
| [14](14-lifecycle-placement-and-object-store.md) | **Cell lifecycle, placement and pressure, object store (D1, D6, D7)** | Decided (open: default shutdown mode, self-hosted store, measured defaults) | Lifecycle on V9's Running/Warm/Absent; app crashes contained as traps; Kubernetes serializes restarts; no Azure | Against D1+D6+D7 (9,295 measured): added +3.3k to +6.45k *(est.)*; **net ≈ −2.8k to −6.0k**. See §6. | One serial pure core with a fast path; crash ladder and quarantine; pluggable activation limits. `claim-local` then `pressure-aware` placement; rebalancing by dominant share; shutdown `in-place`/`hot`/`all` with ingress hold-and-retry; no drain token. S3 and GCS, S3-compatible stores (MinIO for testing), fail-closed probe with a concurrent-create race. |
| [15](15-change-data-capture-to-kafka.md) | **Change data capture to Kafka, through the bucket** (**new feature, not available in celld**) | Explored (design sketch; key decisions recorded, capture mechanism open until built) | Adds a platform feature celld doesn't have; Kafka becomes a dependency of the CDC reader only, never of nodes | Removed 0 (celld has no CDC); added **+1.2k to +2.4k** production, +0.05k to +0.15k tools *(est.)*; **net ≈ +1.2k to +2.4k**. See §5.1. | Apps declare tables and included or excluded columns per cell class. A small `_cdc_pending` table records changed keys in the same transaction. When a node builds its V9 bundle, it adds a CDC section with the rows' current values at the bundle's cut (tombstones for deletes), and deletes the pending rows once the bundle is uploaded. A separate CDC reader follows each node's bundles and writes them to Kafka in one transaction per bundle, with its cursor, so delivery is exactly once; a predecessor marker keeps per-cell order across moves. Only changes already in the bucket are sent, so restores never revert anything Kafka has seen. 1–4 s latency. Key `(cell, table, pk)`, partitioned by cell, versioned `<gen>-<seq>`, for compacted topics. After-state only; app outboxes stay separate. **Open:** generated triggers or a host hook for capture. |
| [16](16-operations-and-tooling.md) | **Operations and tooling (area E)** | Decided (open: OTel interface choice, `dev` extras; StatefulSet manifest deferred) | Self-hosted only; deploy straight to the bucket; standard OpenTelemetry | Production: −6,081 measured, +2.05k to +4.2k *(est.)*, **net ≈ −1.9k to −4.0k**. Tools: −3,914, +1.8k to +3.1k, net ≈ −0.8k to −2.1k. App-side telemetry SDK +0.2k to +0.5k. See §5. | E6 celld.dev not built. `deploy` writes to the bucket with a conditional pointer and rollback. `dev` runs one local node with a built-in store. Operator CLI over the bucket and an mTLS-authenticated operator API; no signed probe. Node process with staged startup and typed config. OpenTelemetry for the platform (performance metrics throughout) and for app code via `wasi:otel` or `celld:otel`. |
| [17](17-cell-browser-ui.md) | **Cell browser UI** (**new feature, not in celld**) | Proposed (open: frontend stack, SQL console, app views, redaction) | Adds an operator UI; row access becomes an explicit, audited permission | Removed 0; added **+1.65k to +3.3k** (backend +0.65k to +1.3k, frontend +1.0k to +2.0k) *(est.)*. See §5. | Fleet, cell list and cell detail (metadata, alarms, crashes, steps runs, read-only paged rows) plus a live SSE stream of lifecycle, request and alarm events, merged across nodes. Never wakes cells. Served by the operator API behind an authenticating proxy; viewer and data-reader roles. |

The impacts are separate changes and do not simply add up. 01 already removes most of 02's WebSocket code, as part of removing the JS and V8 runtime.

## Document conventions

Each exploration uses these sections, so documents can be compared side by side:

1. **Premise.** The constraint we pretend to relax and why.
2. **Goals and non-goals.**
3. **Design.** One subsection per concern.
4. **Integration with the existing engine.** How the change fits areas D and E of the report.
5. **Impact on the codebase.** What is removed, rewritten or kept. **Every document must start this section with a code impact summary (LOC)**:
   - a table of lines **removed**, **added** and **net**, for production code, plus test, example and doc lines where relevant;
   - measured against the baseline report's line map wherever possible, with estimates marked *(est.)* and given as ranges;
   - the result copied into the "Code impact" column of the index above.
6. **Risks and open questions.**
7. **Build order.** How a fresh implementation would sequence the work. No deprecation, data migration or compatibility steps, because the app is green-field.
8. **Next steps.**

Status values:

| Status | Meaning |
|---|---|
| Explored | Thought through on paper. |
| Prototyped | Code exists on a branch. |
| Measured | Benchmarks recorded. |
| Proposed | Ready for a decision. |
| Rejected / Adopted | Decision recorded, with the reason. |

API names in these documents are written against the state of the ecosystem when the document was written. Wasm and WASI APIs move quickly, so check names against the pinned crate versions before you build anything.

Add a new exploration as the next number (`18-…md`) and add a row to the index, including its code impact.
