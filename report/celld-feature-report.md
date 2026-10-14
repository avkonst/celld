# celld — Feature Breakdown, Code Size and Readiness Report

**Snapshot:** celld v0.6.2, commit `90b4301` (repository HEAD), analysed 2026-10-13.
**Scope:** every tracked file in the repository (279 files, 162,216 physical lines).

---

## 1. Summary

**Size.** celld has about **107.3k lines of production code**, excluding comments and blanks. Another **23.8k comment lines** sit in production files: about 22 comment lines per 100 code lines, which is high. Around that core:

| Bucket | Code lines |
|---|--:|
| Shipped CLI tools | 8.0k |
| Examples | 1.2k |
| Build/CI | 0.6k |
| In-repo test code | 3.9k |

**The test suites are not in this repository.** Every test module is pulled in with `include!(env!("CELLD_CONFORMANCE_*" / "CELLD_INTERNAL_*"))` from files outside the repo. The repo contains only:
- about 3.7k lines of **test seams** gated by `cfg(celld_internal_tests)`;
- about 220 lines of **ungated test hooks** that ship in the release binary (`CELLD_TEST_*` environment variables).

So `cargo test` here compiles and runs zero tests. Test coverage cannot be measured from this repository.

**Where the code is.**

| Area | Share of production code |
|---|--:|
| Distributed engine | 33.5% |
| Web/Node runtime APIs | 28.4% |
| Cloudflare service bindings | 22.8% |
| V8 runtime core | 9.6% |
| Operations | 5.7% |

The four biggest features are:
- Node.js compatibility: 13.5k lines, 83% of it ported from workerd, Deno and readable-stream;
- SQLite replication (LTX): 12.6k lines, 63% of it the vendored `celld-ltx` crate;
- the fleet durability log: 7.0k lines;
- Durable Objects: 5.7k lines.

**Third-party code.** About **21.0k lines (19.6%) of production code are ported or vendored**: celld-ltx from rustyriver/Litestream, the Node.js modules from workerd, Deno and readable-stream, a Tokio `select!` fork, and a workerd Pyodide entrypoint.

**Language runtimes.** Rust and Python Workers both run as WebAssembly *inside* the V8/JS runtime. Of the 69.1k lines of runtime and service code:
- 98.3% is shared JS-runtime code;
- 262 lines (0.4%) exist only for WebAssembly / Rust via workers-rs;
- 892 lines (1.3%) exist only for Python on Pyodide; 583 of those are the deploy-time build path.

**Readiness.** The project calls itself beta (`docs/limitations.md:3`), so no feature is rated *Mature*.

| Verdict | Count |
|---|--:|
| Solid | 23 |
| Beta | 14 |
| Partial | 3 (Python Workers, Node.js compat, Cache API) |
| Experimental | 2 (Containers, the managed celld.dev connection) |

**Most material risks found.** Each was checked against the code; details are in §7.
1. Deployed Workers and Durable Objects have **no CPU-time limit**. Only Dynamic Workers get a CPU watchdog. A `while(true){}` holds a runtime thread until the 300 s wall-clock handler budget is checked between turns. This is not documented.
2. The peer tunnel **re-sends a buffered request on a fresh connection after any pooled-tunnel error** (`main/peer_tunnel.rs:428-468`). This bypasses the caller's careful "ambiguous attempts are not retried" rule. If the owner had already run the request, it could run twice. **This needs a targeted test.**
3. Peer **responses are not authenticated**; only requests are signed (`peer_auth.rs:324-335`). Fleet-proof acknowledgements therefore rest on network trust. This is consistent with the documented "trusted private network" model.
4. **Node.js coverage gaps fail late.** About 15 common built-ins (`http`, `net`, `url`, `querystring`, `string_decoder`, …) import successfully and throw on first call.
5. **Several module headers are stale.** `logic/log_tier.rs` says "not yet wired into the engine", but `node_log.rs` makes 42 calls into it. The `node_log.rs` header still describes a base64-JSON wire and `log/<node>.json`.

---

## 2. Method and definitions

### 2.1 Line classes

Every line of every tracked file was put in exactly one class.

| Class | Meaning |
|---|---|
| **Production** | Code that runs in a serving node, or is the node itself. This includes the V8 harness JS. |
| **Tools** | Operator and developer CLI command code compiled into the same binary: `deploy`, `dev`, `d1`/`kv`/`r2`/`queue`/`cell` CLIs, `diagnose`, `connect`/`token`. |
| **Test (gated)** | Items under `#[cfg(celld_internal_tests)]` or `#[cfg(all(test, celld_internal_tests))]`, including attached doc comments and attributes, and JS `__test_*` hooks. These are absent from release builds. |
| **Test (ungated)** | Test-only code *not* behind that cfg: the `CELLD_TEST_SCRIPT_PATH` harness entry in `main.rs` (3875-4084), `CELLD_TEST_FAIL_PUBLISH_ONCE`, the `#[cfg(debug_assertions)]` startup and swap failure injectors, and the KV fail-after-blob hook. It is listed in `method/ungated-test-overrides.txt`. |
| **Examples** | `examples/**`, the dev-support sample apps. |
| **Build/CI** | Cargo manifests, Dockerfile, release workflow, `clippy.toml`, `.gitignore`. |
| **Docs** | Markdown and SVG, counted as non-blank lines. |
| **Excluded** | `Cargo.lock` (generated, 4,267 lines) and LICENSE/NOTICE files (459 lines). |

**"Code lines"** means physical lines that carry at least one code or string token. Comment-only and blank lines are counted separately, and the tables report code lines unless they say otherwise.

### 2.2 How the counting was done

A custom lexer classified each line. It handles Rust raw strings, char literals versus lifetimes, nested block comments, JS template literals and regex literals, and Python triple quotes. I checked its comment counts against a naive line-prefix counter on every source file; no file differed by more than the tolerance.

The extent of each test-gated item was found by an item-aware scanner (headers, generic `<>`, bodies, `let … = if {} else {};`, match arms). I audited all 487 detected ranges for anomalies; none remained.

### 2.3 How lines were assigned to features

Line ranges were assigned to a two-level taxonomy (§3) at item granularity: functions, impls, struct fields and enum variants, match arms, JS classes and sections. Functions longer than about 150 lines were split by purpose. For example, the ~2,100-line `async_main` was split into about 90 ranges, and the giant `on_event` match was split arm by arm.

The large files were mapped by parallel review passes. The merged map has **1,428 rules covering all 162,216 lines, with no gap, overlap or unmapped file**; the validator checks this mechanically. I then audited a random sample of 30 rules against the code they cover.

The judgment calls that most affect the numbers are in Appendix A. Where a line serves several features, it goes to the feature it *exists for*. Shared plumbing goes to the engine or runtime feature that owns it.

### 2.4 Qualitative assessment

For each feature, review passes read the code and docs and recorded runtime dependencies, logical complexity, reliability edge cases (with `file:line`) and readiness signals. I re-verified the high-impact claims myself, and corrected or reworded several:
- the "heap-limit doubling" claim is reworded in A1;
- a claimed in-tree test module for static assets was wrong; it is an external include.

Items marked *(inferred)* come from reading the code and were not exercised.

**Readiness scale:**

| Verdict | Meaning |
|---|---|
| Mature | Not used, because of the beta release. |
| Solid | Ready for production, with known edges. |
| Beta | Works, with material gaps or risks. |
| Partial | A substantial part of the surface is missing. |
| Experimental | Self-declared unstable, or undocumented. |

**Reproduce.** Run in `report/method/`:

```
python3 loc.py feature-map.spec raw.json
python3 merge.py raw.json ungated-test-overrides.txt full.json
python3 tables.py
```

---

## 3. Totals

### 3.1 By class

| Class | Code | Comments | Blank | Physical lines |
|---|--:|--:|--:|--:|
| Production | 107,304 | 23,750 | 6,322 | 137,376 |
| Tools (CLI in binary) | 7,973 | 1,117 | 464 | 9,554 |
| Test, gated | 3,677 | 273 | 19 | 3,969 |
| Test, ungated (ships) | 223 | 32 | 1 | 256 |
| Examples | 1,174 | 34 | 82 | 1,290 |
| Build/CI | 611 | 112 | 53 | 776 |
| Docs (non-blank / blank) | 3,459 | – | 810 | 4,269 |
| Licenses (excluded) | 382 | – | 77 | 459 |
| Cargo.lock (excluded) | 3,846 | 2 | 419 | 4,267 |
| **Total** | | | | **162,216** |

The three buckets you asked for:

| Bucket | What it includes | Code lines |
|---|---|--:|
| Production | | **107,304** |
| Development support & tools | 7,973 CLI tools + 1,174 examples + 611 build/CI | **9,758** |
| Testing (in repo) | 3,677 gated + 223 ungated | **3,900** |

### 3.2 By crate and language (production code)

| Crate | Production code |
|---|--:|
| `crates/celld` (executor, V8 host, harness JS) | 90,710 |
| `crates/logic` (pure sans-IO decision core, zero dependencies) | 8,720 |
| `crates/ltx` (vendored LTX replication) | 7,874 |

| Language | Production code |
|---|--:|
| Rust | 77,466 (72%) |
| JavaScript (harness and API polyfills run inside V8) | 29,791 (28%) |
| Python (the Pyodide entry, `worker.py`) | 47 |

### 3.3 Ported and vendored production code

| Origin | Files | Code lines |
|---|---|--:|
| rustyriver (Rust Litestream v0.5 / LTX), vendored 2026-08-03 | `crates/ltx/src/**` | 7,874 |
| readable-stream 4.2.0 via Deno's bundle | `js/node_stream.js` | 5,402 |
| workerd ports (from Node.js) | `js/node_util.js`, `node_crypto.js`, `node_path.js`, `node_events.js`, `node_os.js` | 7,299 |
| Tokio 1.53.1 `select!` (MIT) | `tokio_select.rs` | 252 |
| workerd Pyodide entrypoint | `python/worker.js` | 157 |
| **Total** | | **20,984 (19.6% of production)** |

---

## 4. Feature breakdown and line counts

Each table lists one first-level area. "Test" combines gated and ungated test lines, with the ungated part in parentheses. "All code" is production + tools + test + examples. The "Main files" lines name the biggest contributors (production + tools).

### A — Workers runtime (V8 engine)

| Feature | Prod code | Prod comments | Tools | Test | Examples | Docs | **All code** |
|---|--:|--:|--:|--:|--:|--:|--:|
| A1 V8 isolate engine core | 2,968 | 1,084 | – | 423 | 8 | 214 | 3,399 |
| A2 Module system & bootstrap | 1,328 | 321 | – | 104 (26) | – | – | 1,432 |
| A3 Cell runtime host & isolate pool | 2,871 | 1,031 | – | 408 (71) | – | – | 3,279 |
| A4 Service bindings & JS RPC | 2,666 | 545 | 37 | – | 29 | – | 2,732 |
| A5 WebAssembly & workers-rs (Rust) | 137 | 26 | 125 | – | 54 | 93 | 316 |
| A6 Python Workers (Pyodide) | 309 | 97 | 583 | 4 | 49 | – | 945 |
| **A subtotal** | **10,279** | **3,104** | **745** | **939 (97)** | **140** | **307** | **12,103** |

Main files:
- **A1:** js.rs 2,607, harness.js 283.
- **A2:** js/modules.rs 672, js.rs 285, js/bootstrap.rs 280.
- **A3:** runtime.rs 1,685, pool.rs 499.
- **A4:** harness.js 1,470, js.rs 530, main.rs 267.
- **A5:** deploy.rs 125, js/modules.rs 103.
- **A6:** deploy/python.rs 571, python/*.js 239.

### B — Runtime APIs (Web platform & Node)

| Feature | Prod code | Prod comments | Tools | Test | Examples | Docs | **All code** |
|---|--:|--:|--:|--:|--:|--:|--:|
| B1 Fetch / Request / Response / bodies | 3,419 | 511 | – | 267 | 29 | – | 3,715 |
| B2 Streams & encoding | 2,770 | 537 | – | – | – | – | 2,770 |
| B3 Other web standards (URL, events, …) | 1,730 | 238 | – | – | 21 | – | 1,751 |
| B4 Web Crypto & node:crypto | 3,877 | 568 | – | – | – | – | 3,877 |
| B5 Node.js compatibility | 13,465 | 1,062 | – | – | – | – | 13,465 |
| B6 WebSockets (incl. hibernation) | 3,653 | 808 | – | 148 | 74 | – | 3,875 |
| B7 HTMLRewriter | 1,066 | 74 | – | – | – | – | 1,066 |
| B8 TCP sockets | 453 | 38 | – | 16 | – | – | 469 |
| B9 Cache API | 76 | 18 | – | – | – | – | 76 |
| **B subtotal** | **30,509** | **3,854** | **–** | **431** | **124** | **–** | **31,064** |

Main files:
- **B1:** http_streams.rs 1,625, harness.js 950, js.rs 588.
- **B2:** streams.js 707, byte_streams.js 589, writable_stream.js 547.
- **B4:** js/crypto.rs 1,662, node_crypto.js 1,491, crypto.js 697.
- **B5:** node_stream.js 5,402, node_util.js 4,001, node_path.js 1,048, node_buffer.js 744.
- **B6:** main/websocket.rs 1,039, js/websocket.rs 668, ws_registry.rs 583.

### C — Cloudflare services (bindings)

| Feature | What it is · Cloudflare docs status · celld verdict | Prod code | Prod comments | Tools | Test | Examples | Docs | **All code** |
|---|---|--:|--:|--:|--:|--:|--:|--:|
| C1 Durable Objects (storage, SQL, gates) | Stateful server objects: one named instance with its own SQLite, single-threaded, storage + SQL + alarms. The base primitive every other service builds on. · **Yes · Solid** | 5,695 | 1,190 | 84 | 407 | 361 | 176 | 6,547 |
| C2 Alarms & wake discovery | Durable per-object timers (`setAlarm`) that wake an object even after eviction, with a fleet-wide wake index. · **Yes · Solid** | 2,643 | 597 | – | 342 | 40 | – | 3,025 |
| C3 Durable Object Facets | Child objects inside a Durable Object, each with its own SQLite, often running classes loaded at runtime for untrusted or generated code. · **Yes · Beta** | 1,666 | 318 | – | 9 | 50 | 113 | 1,725 |
| C4 Workers KV | Key-value store (get/put/list, metadata, expiration). In celld one namespace is one cell; strongly consistent, one writer. · **Yes · Solid** | 1,165 | 501 | 748 | 40 (10) | 29 | 48 | 1,982 |
| C5 Queues | Message queues: producers send, a consumer Worker gets batches, with retries and dead-letter queues. · **Yes · Solid** | 3,143 | 283 | 359 | 269 (19) | 44 | 59 | 3,815 |
| C6 D1 | Serverless SQLite database through a binding (prepare/batch/exec/migrations). One database is one cell. · **Yes · Solid** | 1,126 | 242 | 598 | 35 (7) | 49 | 72 | 1,808 |
| C7 R2 | S3-like object storage (put/get/list, multipart). Stored in the fleet bucket under `r2/`; multipart is node-local. · **Yes · Solid** | 1,918 | 480 | 525 | 7 (7) | 36 | 73 | 2,486 |
| C8 Workflows | Durable multi-step execution (`step.do`, sleeps, events, pause/restart) built on replay. · **Yes · Beta** | 1,454 | 297 | 83 | 141 (24) | 42 | 63 | 1,720 |
| C9 Cron Triggers | Scheduled `scheduled()` runs from cron expressions, once per occurrence across the fleet. · **Yes · Solid** | 615 | 306 | 27 | 7 (7) | 14 | 62 | 663 |
| C10 Static assets | Serve static files as a site, with `_headers`/`_redirects`, alone or with a Worker. · **Yes · Solid** | 1,685 | 75 | 398 | 4 | 19 | 258 | 2,106 |
| C11 Dynamic Workers & Tail Workers | Load and run Worker code supplied at runtime (user- or AI-written) in a separate isolate with limits; Tail Workers get per-invocation reports. · **Yes (upstream open beta) · Beta** | 1,231 | 197 | 32 | 7 | 44 | 121 | 1,314 |
| C12 Containers & Sandbox | Run a non-JS program in a Docker/Podman container supervised by a Durable Object; includes the `@cloudflare/sandbox` SDK. · **Experimental · Experimental** | 2,098 | 364 | 244 | – | 154 | 186 | 2,496 |
| **C subtotal** | | **24,439** | **4,850** | **3,098** | **1,268 (74)** | **882** | **1,231** | **29,687** |

Main files:
- **C1:** storage.rs 1,755, js.rs 1,223, harness.js 1,157, js/storage_ops.rs 711.
- **C2:** wake.rs 529, logic/lib.rs 476, js.rs 428.
- **C3:** js.rs 678, storage.rs 247, facet_streams.rs 245.
- **C4:** services/kv.js 767, kv_cli.rs 689.
- **C5:** harness.js 1,467, queue_policy.rs 367, js.rs 344.
- **C6:** storage.rs 827, d1_cli.rs 445.
- **C7:** r2_store.rs 555, js/r2_ops.rs 522, r2_cli.rs 482, bucket.rs 411.
- **C8:** harness.js 1,351, which makes it a JS-only engine.
- **C9:** logic/cron.rs 416.
- **C10:** assets.rs 1,328, deploy.rs 398.
- **C11:** js.rs 825, harness.js 322.
- **C12:** container.rs 1,253, js/container.rs 285.

### D — Distributed cell engine (fleet)

| Feature | Prod code | Prod comments | Tools | Test | Examples | Docs | **All code** |
|---|--:|--:|--:|--:|--:|--:|--:|
| D1 Decision core & cell lifecycle | 4,074 | 1,024 | – | 280 | – | – | 4,354 |
| D2 Ownership leases & fencing | 2,979 | 606 | – | 70 (31) | – | 264 | 3,049 |
| D3 SQLite replication & restore (LTX) | 12,557 | 4,700 | – | 380 | – | 128 | 12,937 |
| D4 Fleet durability ensemble (node log) | 7,030 | 2,051 | – | 190 | – | – | 7,220 |
| D5 Routing, ingress & peer transport | 2,372 | 491 | – | 4 | 28 | – | 2,404 |
| D6 Placement, pressure & drain | 4,060 | 1,420 | – | 83 (1) | – | – | 4,143 |
| D7 Object-store backends (S3/GCS/Azure) | 1,161 | 425 | 26 | 54 | – | – | 1,241 |
| D8 Peer authentication | 367 | 24 | – | – | – | 124 | 367 |
| D9 Deployment adoption & generations | 1,337 | 487 | – | 26 (20) | – | – | 1,363 |
| **D subtotal** | **35,937** | **11,228** | **26** | **1,087 (52)** | **28** | **516** | **37,078** |

Main files:
- **D1:** logic/lib.rs 1,771, actor.rs 1,585.
- **D2:** logic/lib.rs 1,326, actor.rs 598, ownership_store.rs 367.
- **D3:** ltx_repl.rs 2,704, plus the ltx crate 7,874 (db.rs 1,659, client/object_store.rs 972, replica.rs 680).
- **D4:** node_log.rs 5,049, ltx_repl.rs 870.
- **D5:** main.rs 1,278, peer_tunnel.rs 541.
- **D6:** logic/lib.rs 758, main.rs 699, actor.rs 622.
- **D7:** bucket.rs 1,027.
- **D9:** generation.rs 272, fleet.rs 216.

Notes:
- The D area has the highest comment density: D3 has 37 comment lines per 100 code lines.
- `docs/guarantees.md` (264 lines) is attributed to D2, but it covers D2, D3, D4 and C2.

### E — Operations & tooling

| Feature | Prod code | Prod comments | Tools | Test | Examples | Docs | **All code** |
|---|--:|--:|--:|--:|--:|--:|--:|
| E1 `celld deploy` | – | – | 1,051 | 4 | – | – | 1,055 |
| E2 `celld dev` | – | – | 1,282 | 8 | – | – | 1,290 |
| E3 Operator CLI & diagnose | 231 | 28 | 1,429 | 4 | – | – | 1,664 |
| E4 Node process, CLI & operator API | 2,276 | 369 | – | 121 | – | – | 2,397 |
| E5 Telemetry | 1,367 | 196 | – | 20 | – | 109 | 1,387 |
| E6 Managed control plane (celld.dev) | 2,266 | 121 | 342 | 18 | – | – | 2,626 |
| **E subtotal** | **6,140** | **714** | **4,104** | **175** | **–** | **109** | **10,419** |

Notes:
- E1 counts only the generic deploy pipeline. Deploy code for one service is attributed to that service as tools. For example, 398 lines of asset upload count toward C10, and 166 lines of queue config toward C5.
- In total, `deploy.rs` plus `deploy/python.rs` contribute 2,973 tool lines across 14 features. 942 of them are E1; 2,031 are attributed to other features.

Cross-cutting docs that are not attributed to any feature: 1,296 non-blank lines (README, docs index, compatibility, limitations, testing).

---

## 5. Breakdown by language runtime: JS, Rust (WASM), Python (WASM)

celld has **one** application runtime: V8 running JavaScript.

| Runtime | How it runs |
|---|---|
| JS runtime | V8 runs the application directly. |
| Rust on WASM | workers-rs emits a JS shim plus a `.wasm` module. The shim is an ordinary JS entry point, so the Rust app depends on the entire JS runtime and binding surface. |
| Python on WASM | `celld deploy` bundles Pyodide 0.28.3 (CPython 3.13 compiled to wasm) with a JS entry adapted from workerd. Python code reaches bindings through the same JS objects. |

So the honest split is shared JS-runtime code against code that exists *only* for one language. Scope: areas A, B and C (runtime, APIs, services), production + tools.

| Runtime support | Production | Tools (build/deploy) | Total | Share |
|---|--:|--:|--:|--:|
| Shared JS runtime (used by all three) | 64,781 | 3,135 | 67,916 | 98.3% |
| WebAssembly / Rust (workers-rs) specific | 137 | 125 | 262 | 0.4% |
| Python (Pyodide) specific | 309 | 583 | 892 | 1.3% |
| **Total A+B+C** | **65,227** | **3,843** | **69,070** | |

The shared tools lines are the service CLIs and per-service deploy steps (KV, D1, R2, Queues, assets, containers). Those are language-neutral.

What the language-specific lines are:
- **WASM / Rust (262 lines):**
  - a 32-entry SHA-256-keyed compiled-module LRU and wasm import stubs (`js/modules.rs`, 103 lines);
  - deploy-side discovery and upload for `.wasm` and `no_bundle`, plus the `wasm-v1` feature flag (`deploy.rs`, 125 lines);
  - side-band wasm in the Worker Loader (`js.rs`, 29 lines).

  workers-rs needs **no Rust-specific runtime code**. Its shim Proxy is resolved by the generic class-resolution walk.
- **Python (892 lines):**
  - the deploy build path (`deploy/python.rs`, 571 lines): Pyodide download with pinned size and SHA-256, compatibility-date and flag rules, vendoring;
  - the JS entry, fatal gate and loader shims (`python/*.js`, 239 lines);
  - the Python entry (`worker.py`, 47 lines);
  - small hooks in `js.rs` and `harness.js`: isolate condemnation on a Pyodide fatal error, and a body-stream `constructor` shim for the Python SDK.
- **Platform code (D + E, 42,077 production lines) is language-neutral.** It is the engine underneath whichever runtime serves the request.

The interpreter itself is not counted, because it is downloaded at build time. Pyodide is tens of MB of third-party wasm. The V8 engine (rusty_v8) is likewise an external dependency.

---

## 6. Readiness overview

The "Docs status" column is taken from `docs/cloudflare-compat.md` where that page rates the feature.

| Feature | Docs status | Logical complexity | Verdict | Main reason |
|---|---|---|---|---|
| A1 V8 engine core | Yes | Very High | **Solid** | Rigorous turn model. No CPU limit for deployed code (undocumented). |
| A2 Modules & bootstrap | Yes | Medium–High | **Solid** | Regex import scan; no top-level await. |
| A3 Isolate pool & host | – | High | **Solid** | Pure pool policy with fixed incident bugs. Stateless growth compiles on a tokio worker. |
| A4 Service bindings & RPC | Yes (3 diffs) | Very High | **Beta** | Stubs cannot cross isolates; "not supported yet" paths; no depth or size caps. |
| A5 WASM / workers-rs | Yes | Low–Medium | **Solid** | Small, gated by the `wasm-v1` feature. |
| A6 Python Workers | **Partial** | Medium | **Partial** | `fetch`-only by design. A broken main module still adopts. |
| B1 Fetch | Yes | High | **Solid** | 120 s whole-request fetch timeout also cuts streaming bodies and SSE (undocumented). |
| B2 Streams & encoding | Yes | High | **Solid** | Native zlib state can leak; no inflate size cap. |
| B3 Web standards | Yes | Medium | **Solid** | Fidelity gaps (console, performance), not data-loss gaps. |
| B4 Crypto | Yes / Partial | High | **Beta** | Large node:crypto gap list; synchronous RSA keygen on the isolate thread. |
| B5 Node.js compat | **Partial** | Medium | **Partial** | About 15 common built-ins throw on first call; `process.env` is `{}`. |
| B6 WebSockets | Yes (8 diffs) | Very High | **Solid** | Fleet-tested at 20k sockets. Unbounded send queues; hibernation limits not enforced. |
| B7 HTMLRewriter | Yes | High | **Beta** | Thread per transform; blocking cross-thread calls; no backpressure. |
| B8 TCP sockets | Yes | Medium | **Beta** | Possible `startTls` hang; no connect timeout; cancel is a no-op. |
| B9 Cache API | **Partial** | Low | **Partial** | Always-miss facade by design. |
| C1 Durable Objects | Yes (13 diffs) | Very High | **Solid** | Densest fault injection. No SQL interrupt; possible coalesced-put error masking *(inferred)*. |
| C2 Alarms & wake | Yes | Very High | **Solid** | Precise contract. Bookkeeping SQL errors ignored; minute-scale waker failover. |
| C3 Facets | Yes (6 diffs) | High | **Beta** | "Not supported yet" APIs; unbounded stop retry; no fault hooks. |
| C4 KV | Yes | Medium–High | **Solid** | Single writer per namespace; caller-clock expiry; test knobs honored in production. |
| C5 Queues | Yes | High | **Solid** | Careful fencing. A lost settlement counts as a failure; retention can drop stuck DLQ marks. |
| C6 D1 | Yes (10 diffs) | Medium | **Solid** | Atomic and fails closed. No query time bound; hard result caps. |
| C7 R2 | Yes | Medium | **Solid** | Multipart uploads are node-local and in-memory (documented). |
| C8 Workflows | Yes | Very High | **Beta** | JS-only replay engine; determinism not checked; uncounted crash retries. |
| C9 Cron Triggers | Yes | Medium | **Solid** | At-least-once on crash; a slow handler skips occurrences. |
| C10 Static assets | Yes | Medium | **Solid** | Cache hits checked by size only; a bad `_headers` value gives a 502. |
| C11 Dynamic Workers | Yes (upstream beta) | High | **Beta** | Slot release depends on GC; no memory admission. |
| C12 Containers | **Experimental** | High | **Experimental** | No Docker timeouts; orphan windows; whole image tar in memory. |
| D1 Core & lifecycle | – | Very High | **Solid** | Clean sans-IO core. 7.1k-line file; invariants checked in debug builds only. |
| D2 Ownership & fencing | – | Very High | **Solid** | TLA+-found bugs fixed; CAS probe at startup. Clock-skew assumption undocumented. |
| D3 Replication (LTX) | – | Very High | **Beta** | Rigorous output gate. Vendored crate about 10 weeks in-tree; restore not yet measured. |
| D4 Fleet log ensemble | – | Very High | **Beta (→ Experimental)** | Self-described "v0" yet the default mode; stale headers; declared-loss path. |
| D5 Routing & peer transport | – | High | **Beta** | Pooled-tunnel resend vs at-most-once; plaintext; protocol churn. |
| D6 Placement & pressure | – | High | **Beta** | Good hysteresis. cgroup detection reads the root only; count-based balancing. |
| D7 Object-store backends | – | Medium | **Solid** | Strict CAS error contract and mandatory probe. |
| D8 Peer authentication | Beta | Medium | **Beta** | Responses unauthenticated; no key rotation; body read before auth. |
| D9 Deployment adoption | – | Medium–High | **Beta** | Frequent stop-all upgrades; no canary (`percent` ignored); non-atomic multi-script load. |
| E1 `celld deploy` | Yes | High | **Solid** | Strict allowlist. Last deploy wins; no rollback or secrets. |
| E2 `celld dev` | Yes | Medium | **Solid** | Careful supervisor. A failed node start ends the session. |
| E3 Operator CLI | Yes | Medium | **Solid** | Signed probes. Bind-check quirk; sequential probing. |
| E4 Node process | Yes | Very High | **Solid** | Bounded shutdown. An `accept()` error exits without a handoff; alpha operator API. |
| E5 Telemetry | Yes (`v0-unstable` schema) | Medium | **Beta** | No shutdown flush; no bucket retry; no metrics. |
| E6 Managed control plane | **Undocumented** | High | **Experimental** | Re-exec bypasses graceful shutdown; pointers written without CAS. |

---

## 7. Per-feature detail

Every section uses the same four headings: dependencies, complexity, edge cases and readiness. "Prod" in the opening line means production code lines. File paths are relative to `crates/celld/` unless they start with `crates/`, `docs/` or `examples/`. *(inferred)* marks a conclusion drawn from reading the code that was not exercised.

### A — Workers runtime

#### A1 V8 isolate engine core
*Prod 2,968 · Test 423 · Solid · Very High*

**Dependencies**
- `v8` 152.1 (rusty_v8 with `v8::Locker` and Send Globals), `tokio`, `serde_json`.
- Thread CPU clocks: `CLOCK_THREAD_CPUTIME_ID` on Linux, Mach thread ports on macOS.
- One `celld-cpu-watchdog` OS thread.
- No external binaries.

**Complexity: Very High.**
- One shared isolate serves many requests and up to 32 cells, one turn at a time.
- `finish_turn` runs a fixed seven-step order: settle, checkpoint, facet gate, WebSocket flush, and so on.
- The op layer must hand continuations that ran in another event's microtask checkpoint back to the right driver.
- Termination, CPU accounting, the input and output gates and `waitUntil` all meet in one 12.7k-line file.

**Edge cases**
- **No CPU-time limit for deployed Workers or Durable Objects.** `WorkerConfig::new` sets `resource_limits: None` (`js.rs:2069`). Only the Worker Loader calls `.with_resource_limits` (`js.rs:7686`), and `CpuWatchdog::start` returns `None` without a declared limit (`js.rs:2641-2648`). The only bound is the 300 s `CELLD_HANDLER_BUDGET_S` (`engine_api.rs:874-883`), checked between turns. A tight synchronous loop therefore holds a runtime thread. Neither knob appears in docs/.
- **The heap-limit callback extends the heap instead of failing hard.** `near_heap_limit` latches "excessively exceeded" and extends the limit to `max(current + reserve, 2 × limit)` so condemned JS can unwind (`js.rs:2852-2875`). V8 aborts the process if the callback does not extend. The latch is consulted by the SQL `toArray()` and `acceptWebSocket` guards, and `recover_heap` clears it below 75%. The residual risk: a single allocation beyond the extension still aborts the process (release builds use `panic = "abort"`), and up to 32 cells share that heap.
- **`waitUntil` work has no time bound** once the response is sent (`js.rs:3700-3704`). An endless background loop keeps an old generation's isolate retiring.
- **Dynamic Worker top-level code runs before any watchdog exists** (`js.rs:5875-5890`, run under `spawn_blocking` at `js.rs:7691`).

**Readiness: Solid.** Workers, Context, Handlers and Console are rated **Yes**. The code has zero TODO markers and 132 test seams. The two resource-isolation gaps above keep it below Mature.

#### A2 Module system & bootstrap
*Prod 1,328 · Test 104 · Solid · Medium–High*

**Dependencies**
- `v8` (modules, synthetic modules, code cache), `regex`, `sha2`.
- All builtin JS (about 23k lines) is compiled in with `include_bytes!`. Nothing is loaded at runtime.

**Complexity: Medium–High.**
- Lazy builtins compile on first read.
- `node:*` and `cloudflare:*` resolve to generated stub modules.
- A process-wide bytecode cache is keyed by script name.
- Per-binding `env` construction (attributed to the services) is called from here.

**Edge cases**
- **The import scan is a regex** that requires `import … from` (`js/modules.rs:257-262`). A side-effect `import "node:x"` or an `export … from` adds no stub, so instantiation fails *(inferred)*. Bundled output usually avoids this. `no_bundle` and Dynamic Worker code can hit it.
- **No top-level await.** A pending evaluation promise is never awaited (`js.rs:5875-5905`; `python/worker.js:12-16`). The compatibility page does not mention it.
- **Unknown compatibility flags are silently accepted.** Only 6 flags plus the asset-navigation flags change behaviour.
- **A sibling module that fails to compile is only logged** (`js/modules.rs:647,661`). The real error surfaces later as an unresolved import.

**Readiness: Solid.** No TODOs. The gaps are narrow and mostly avoided by esbuild-bundled deployments.

#### A3 Cell runtime host & isolate pool
*Prod 2,871 · Test 408 (71 ungated) · Solid · High*

**Dependencies**
- `celld-logic::isolate` (a pure policy module), `tokio`, `v8`.
- The data directory must allow long file names (a minimum `NAME_MAX`).
- Host channels are unbounded `mpsc` channels.

**Complexity: High.**
- Three independent counts must agree with `may_free` (`crates/logic/isolate.rs:319-332`): turns (CPU), affiliations (live promises) and residencies (cells).
- A per-cell round-robin turn scheduler.
- StartRuntime → Publish → StopRuntime handle states.

**Edge cases**
- **The freed-isolate re-entry fix (#147).** An affiliation is taken under the registry lock (`runtime.rs:1995-2015`). If the invariant breaks, `pool.rs:258` panics, which aborts the process.
- **Stateless pool growth compiles the whole script on a tokio worker** (`pool.rs:609-610`). Cell placement correctly uses `spawn_blocking` (`runtime.rs:1279-1284`).
- **Blast radius of a shared heap.** Up to 32 cells pack into the fullest isolate, so one OOM or fatal error hits all of them. Condemnation frees a cell isolate only once its cells leave (`pool.rs:404-442`).
- **Per-cell admission cap.** At most 64 concurrent fetches per cell; the rest get 503 with `X-Celld-Overload` (`runtime.rs:57`).

**Readiness: Solid.** The comments record the defects that were found and fixed, and `/state` exposes pool census data.

#### A4 Service bindings & JS RPC
*Prod 2,666 · Tools 37 · Beta · Very High*

**Dependencies**
- The V8 ValueSerializer behind the `__sc_encode` / `__sc_decode` RPC envelope.
- `tokio` channels, `hmac`/`sha2` for DO id derivation.
- D5 transport for cross-node calls.

**Complexity: Very High.** About 1,500 harness lines plus 530 lines of `js.rs` implement:
- ref-counted stubs and promise pipelining;
- RpcTarget, ReadableStream and AbortSignal bridging;
- workerd-exact request-context confinement;
- a tagged structured-clone envelope.

**Edge cases**
- **Stubs cannot cross isolates.** `__rpcOut` throws `DataCloneError` for non-same-isolate transports (`js/harness.js:5000-5016`). Returning an `RpcTarget` across scripts or nodes fails where Cloudflare succeeds. This is documented.
- **`__abortedCtxs` only grows** (`js/harness.js:3842,3891`): a slow leak, bounded by isolate retirement.
- **No recursion-depth, subrequest or payload cap for deployed code.** Subrequest charging applies only when a limit exists (`js.rs:11116-11125`).
- **"Not supported yet" paths.** Awaitable and pipelined properties on facets and loaded workers throw that error (`js/harness.js:2193-2197,3294-3298`).

**Readiness: Beta.** RPC is rated **Yes** with 3 listed differences. The isolate-boundary gap is material for multi-Worker designs.

#### A5 WebAssembly & workers-rs (Rust)
*Prod 137 · Tools 125 · Examples 54 · Solid · Low–Medium*

**Dependencies**
- `v8` wasm compilation, `sha2`.
- At build time: `worker-build` (workers-rs) and esbuild's `.wasm` copy loader.
- The fleet feature flag `wasm-v1`.

**Complexity: Low–Medium.**
- A bounded 32-entry LRU of compiled modules keyed by SHA-256 (`js/modules.rs:681-725`).
- One stub module per wasm import.
- A deploy-time directory scan.
- A Proxy-safe prototype walk for the workers-rs shim (`js/bootstrap.rs:867-894`, bounded at 1,000 steps).

**Edge cases**
- **Compile failures are not cached.** Every new isolate recompiles and throws `CompileError` again.
- **LRU eviction by Dynamic Worker blobs.** Evicted deployed modules are recompiled synchronously in the next isolate build.
- **No code-generation policy.** Runtime `WebAssembly.compile(bytes)` and `eval` appear to be allowed *(inferred)*, which is more permissive than workerd.

**Readiness: Solid.** WebAssembly is rated **Yes**. The feature gate keeps mixed-version fleets safe at deploy time. Any missing JS API also limits workers-rs apps, since they run on the JS runtime (`docs/wasm.md`).

#### A6 Python Workers (Pyodide)
*Prod 309 · Tools 583 · Test 4 · Partial · Medium*

**Dependencies**
- Pyodide 0.28.3 (CPython 3.13.2), downloaded once at build time from jsDelivr and verified by size and SHA-256 per file (`deploy/python.rs:29-63`).
- `esbuild`, plus `uv run pywrangler sync` to vendor packages.
- The `python-workers-v1` fleet feature.

**Complexity: Medium.** Mostly a careful port of workerd's Python entry:
- the interpreter starts lazily and is shared;
- failures are sticky;
- a fatal gate fails waiting calls;
- loader shims refuse unbundled assets.

**Edge cases**
- **A main module that raises on import still passes deploy and adoption.** The interpreter starts on the first request, so every request then fails with the same traceback.
- **Wasm linear memory is outside the V8 heap accounting** (`js.rs:2877-2882`). Each stateless isolate holds its own interpreter of tens of MB.
- **JSPI is hidden on purpose** to avoid a C-stack leak, so `run_sync` and most `import_from_javascript` calls do not work.
- **`celld deploy` does not check that nodes support `python-workers-v1`.** A mixed fleet serves two versions.

**Readiness: Partial.** The docs scope it to "`fetch` handlers on the Pyodide 0.28 runtime line". Durable Objects, Workflows, Cron, Queue consumers, RPC and native wheels are all unsupported.

### B — Runtime APIs

#### B1 Fetch / Request / Response / bodies
*Prod 3,419 · Test 267 · Solid · High*

**Dependencies**
- `reqwest` 0.12 with rustls and webpki-roots (no gzip/brotli features).
- `hyper` 1 and `http-body-util`.
- A 2,105-line Rust HTTP stream registry.
- Outbound fetch waits on the cell's egress (output) gate, so it depends on D3/D4 durability proofs.

**Complexity: High.**
- Buffered, host-stream and JS-stream body representations.
- Tee, transfer, a 60 s idle sweeper and termination state.
- Every response chunk is gated.

**Edge cases**
- **The whole-request timeout also cuts long bodies.** `CELLD_FETCH_TIMEOUT_S` (default 120 s, `js.rs:9197-9201`) covers the full body, so it cuts long downloads, SSE and EventSource. It is undocumented.
- **`text()`/`json()`/`arrayBuffer()` materialize the body in the V8 heap**, while the default ingress body limit is 1 GiB.
- **`Response.clone()` on a streaming body throws** (`js/harness.js:362-364`), while `Request.clone()` tees.
- **A host-stream tee couples backpressure.** An unread branch stalls its sibling after 16 chunks.

**Readiness: Solid.** Rated **Yes**, with listed gaps: no `cache` option, `cf` is `{}`, and `Content-Length` is stripped.

#### B2 Streams & encoding
*Prod 2,770 · Solid · High*

**Dependencies**
- `flate2` (compression), `encoding_rs` (TextDecoder).
- Everything else is pure JS.

**Complexity: High.**
- A JS re-implementation of the WHATWG Readable, Writable and Transform stream state machines and of BYOB byte streams.
- workerd-specific IdentityTransformStream and FixedLengthStream semantics, with deliberate deviations from the spec to match workerd.

**Edge cases**
- **Native zlib state can leak.** `ZLIB_STREAMS` is a process-global map with no finalizer (`js/zlib.rs:162-174`), so an abandoned Compression or Decompression stream leaks its state.
- **No cap on inflated output** (`js/zlib.rs:132-145`).
- **A global mutex serializes every compression push across isolates.**

**Readiness: Solid.** Streams and Encoding are rated **Yes**. The code cites WPT and workerd behaviour throughout.

#### B3 Other web standards
*Prod 1,730 · Examples 21 · Solid · Medium*

**Dependencies:** `url`, `urlpattern` (pinned to =0.4.2), `base64`, the V8 serializer for `structuredClone`.

**Complexity: Medium.** Many small, independent shims. The tricky parts are the I/O-frozen clocks and structured-clone host types.

**Edge cases**
- **`console` formats with `JSON.stringify`.** A Map logs as `{}`, `%s` is not expanded, and `console.assert`/`table`/`time` are no-ops.
- **`performance.mark()` and `measure()` return `undefined`.**
- **`AbortSignal.any()` never removes its source listeners**, so they accumulate.
- **`BroadcastChannel` and `FileReader` throw on construction.** BroadcastChannel is documented as **No**.

**Readiness: Solid.** These are fidelity gaps, not correctness gaps.

#### B4 Web Crypto & node:crypto
*Prod 3,877 · Beta · High*

**Dependencies**
- The RustCrypto stack: `sha1`/`sha2`/`md-5`, `hmac`, `hkdf`, `pbkdf2`, `aes`/`aes-gcm`/`cbc`/`ctr`, `rsa`, `p256`/`p384`/`p521`, `ed25519-dalek`, `x25519-dalek`, `dsa` (parse only), `pkcs8`.
- No OpenSSL, unlike workerd.
- `node_crypto.js` (1,491 lines) is a port of workerd at commit 191a27f.

**Complexity: High.**
- Key formats: SPKI, PKCS#8 (including encrypted), PKCS#1, SEC1, JWK and raw.
- Two API layers: Web Crypto and Node's KeyObject.

**Edge cases**
- **Asymmetric operations block the isolate thread and pass bytes as JSON arrays.** RSA keygen up to 16,384 bits runs synchronously on the isolate thread.
- **`Hash`/`Hmac`/`DigestStream` buffer their whole input.**
- **Missing algorithms and narrower checks.** No AES-192 and no AES-KW; GCM IVs must be 12 or 16 bytes; Web Crypto ECDSA supports only P-256.
- **Many node:crypto APIs throw `ERR_METHOD_NOT_IMPLEMENTED`:** createCipheriv, createSign, publicEncrypt, scrypt, DH/ECDH, X509Certificate. Some of these are missing from the docs' gap list.

**Readiness: Beta.** The Web Crypto core is broad. The node:crypto gaps and blocking key generation limit real use.

#### B5 Node.js compatibility
*Prod 13,465 (83% ported) · Partial · Medium*

**Dependencies:** almost entirely JS:
- `node:stream` is readable-stream 4.2.0 via Deno (5,402 lines);
- `util`, `path`, `events` and `os` are workerd ports;
- `buffer`, `assert`, `async_hooks`, `diagnostics_channel` and `test` are hand-written;
- a request-scoped VFS, and the `flate2` one-shot zlib op.

**Complexity: Medium.** The volume is high, but the celld-specific logic is the lazy registry and stub generation.

**Edge cases**
- **Unlisted built-ins resolve to a Proxy that throws on first call.** This covers `http(s)`, `net`, `tls`, `dns`, `url`, `querystring`, `string_decoder`, `timers`, `vm`, … (`js/harness.js:9576-9605`). Failures appear deep inside dependencies at run time.
- **`process.env` is always `{}`.**
- **`node:zlib` is sync-only gzip/deflate**, with no streams and no brotli.
- **`node:fs` is read-mostly.** All writes throw.

**Readiness: Partial**, as documented. The 19 TODOs are almost all upstream ones (BridgeAR, ronag).

#### B6 WebSockets (incl. hibernation)
*Prod 3,653 · Test 148 · Examples 74 · Solid · Very High*

**Dependencies**
- `fastwebsockets` pinned to =0.8.1 for its `unstable-split` feature, a deliberate risk that the manifest comment explains.
- `hyper`, `tokio-rustls`.
- Peer tunnel splicing from D5.

**Complexity: Very High.**
- Three socket shapes: local, proxied to the owner, and outbound.
- Two delivery models: host-pushed and isolate-polled.
- Frames are held behind the output gate.
- Hibernation and facet routing.

**Edge cases**
- **No send-side backpressure.** Every output path uses an unbounded `mpsc` channel, and there is no `bufferedAmount`.
- **Hibernation limits are not enforced.** Any number of tags, no attachment size cap, and no `setHibernatableWebSocketEventTimeout`. This is a defect by the project's own rule that an unsupported feature must error.
- **Movement closes sockets.** `acceptWebSocket` refuses above 90% heap use. Rebalance closes sockets with 1012, and a moving cell closes its outbound sockets.
- **The outbound client has no handshake timeout inside `ws_client::connect`.**

**Readiness: Solid.** Rated **Yes**, with 8 documented differences. The lab claims 20k concurrent sockets on 10 nodes (`docs/testing.md`).

#### B7 HTMLRewriter
*Prod 1,066 · Beta · High*

**Dependencies:** `lol_html` 3.0 (the same parser workerd uses), `encoding_rs`, and **one OS thread per `transform()`**.

**Complexity: High.** It bridges lol_html's synchronous callbacks to async JS handlers through a parked parser thread.

**Edge cases**
- **Each token accessor is a synchronous cross-thread round trip** with a 10 s timeout. It blocks the isolate thread.
- **No backpressure on input or output** (unbounded queues), and no lol_html memory settings.
- **Unbounded thread creation** under concurrent transforms.

**Readiness: Beta.** Rated **Yes** and functionally faithful, but the design is a latency and scale liability.

#### B8 TCP sockets
*Prod 453 · Test 16 · Beta · Medium*

**Dependencies:** `tokio::net`, `tokio-rustls` with the Mozilla roots. Egress is gated like fetch.

**Complexity: Medium.** A split-stream registry. `startTls` re-registers the socket under a new id.

**Edge cases**
- **`startTls()` can deadlock behind an in-flight read** that holds the read-half lock (`js/tcp.rs:195-206` vs `298-301`).
- **No connect or read timeout.**
- **`readable.cancel()` and `writable.abort()` are no-ops.**
- **No subrequest accounting.**

**Readiness: Beta.** Rated **Yes**, with 3 listed differences. A small, young component.

#### B9 Cache API
*Prod 76 · Partial · Low*

**Dependencies:** none (pure JS).

**Edge cases**
- **Always miss.** Code that relies on the cache for correctness (dedupe, rate limits) silently loses that behaviour.
- **`put()` drains the body into the heap and discards it.**
- **The `cache:` request option throws.**

**Readiness: Partial.** Documented as an always-miss facade.

### C — Cloudflare services

#### C1 Durable Objects
*Prod 5,695 · Tools 84 · Test 407 · Examples 361 · Solid · Very High*

**Dependencies**
- `rusqlite` 0.31 with bundled SQLite in WAL mode, `synchronous=NORMAL`.
- `sqlite-vec` pinned to =0.1.9, behind a compatibility flag.
- V8 serializer wire version 15, for workerd compatibility.
- It rides on D2 ownership, D3 replication and the output gate, D4, and C2.

**Complexity: Very High.**
- A SQL authorizer of about 130 lines.
- Raw-FFI cursors (51 `unsafe` sites in `storage.rs`).
- Coalesced puts, and nested transactions built from savepoints.
- A cross-event input gate, and `storage.sync()` tickets.

**Edge cases**
- **The input gate is narrower than workerd's.** Only `blockConcurrencyWhile` and transactions close it. Any other await interleaves, and a transaction that awaits I/O shuts out the whole object.
- **A failed coalesced flush may reject only the flushing caller** *(inferred)* (`js/storage_ops.rs:215-226`). A sibling put from the same turn could then resolve even though its value was rolled back.
- **No production SQLite progress handler**, so a runaway query holds the turn.
- **Critical SQLite errors poison the scope** (FULL, IOERR, NOMEM, INTERRUPT). Writes then fail closed until the database reopens.

**Readiness: Solid.** Rated **Yes** with 13 documented differences. It has the densest fault-injection hooks in the repository.

#### C2 Alarms & wake discovery
*Prod 2,643 · Test 342 · Solid · Very High*

**Dependencies**
- SQLite triggers and rows: `_cf_ALARM`, `_cf_WAKE`.
- Bucket objects: `wake/entries/…`, `wake/retired/…` and the `wake/waker.json` lease.
- D2 for owner proofs, D3 for durable consumes, D9 for the format gate.

**Complexity: Very High.**
- Immutable `(epoch, sequence)` publications.
- Arm gates that hold the `setAlarm()` response until the wake entry is written.
- A three-proof retirement CAS.
- Paginated mark-and-collect, and an elected waker.

**Edge cases**
- **Bookkeeping SQL results are ignored.** That covers the consuming `DELETE` and the retry/abandon updates (`storage.rs:4416-4464`). A failed consume re-fires the alarm unreported. A failed retry update may re-fire without counting *(inferred)*.
- **`setAlarm(NaN)` arms at epoch 0** and fires immediately. workerd rejects such input.
- **Waker failover is slow.** The waker lease is 3 ticks of 60 s, so after an owner crash the alarms of its evicted cells can fire minutes late. `CELLD_ALARM_RESIDENT_MS` (1 h) is undocumented.
- **The wake-format upgrade from v0.4.1 needs a fully stopped fleet.**

**Readiness: Solid.** It has an unusually precise contract (`docs/guarantees.md:263-330`).

#### C3 Durable Object Facets
*Prod 1,666 · Examples 50 · Beta · High*

**Dependencies**
- A separate SQLite database and LTX stream per facet, nested under the root.
- The Worker Loader (C11).
- Its egress waits on two proofs: the facet's and the root's.

**Complexity: High.** Facet trees up to depth 4, root-first stop ordering, and a legacy image migration.

**Edge cases**
- **No atomicity with the root.** Rolling back a root transaction does not undo a facet write.
- **A failing facet stop is retried forever** (50 ms to 5 s backoff), which blocks the root's handoff.
- **`ctx.facets.delete()` swallows failures and leaves no tombstone** *(inferred)*. Data can return on a later activation.
- **Facet sockets never hibernate.**

**Readiness: Beta.**
- Several APIs throw "not supported yet", and facets cannot set alarms.
- There are no facet fault-injection hooks.
- The compatibility table still says **Yes**.

#### C4 Workers KV
*Prod 1,165 · Tools 748 · Test 40 (10 ungated) · Solid · Medium–High*

**Dependencies**
- A reserved DO class over SQLite, one shard (`SHARDS = 1`).
- Values over 1 MiB go to `kv/blobs-v2/<cell>/e<epoch>/<sha256>` in the bucket.
- The CLI uses the authenticated `/runtime/` operator route.

**Complexity: Medium–High.** Epoch-scoped blob references and mark-and-sweep, ordered against puts by one protocol lock.

**Edge cases**
- **Expiry uses the caller's clock** (`kv.js:1003,1063,1090`). Node clock skew moves the expiry boundary.
- **An absolute `expiration` skips the documented 60 s minimum.**
- **The blob sweep lists a namespace's whole prefix in one call under the write lock.** Legacy `kv/blobs/` objects are never collected.
- **The `CELLD_TEST_KV` knobs are read from the production environment** (`kv_blob.rs:133-180`). `fail-after-blob` and `no-sweep` would be honored on a live node.

**Readiness: Solid.** Four documented differences. Notably, KV is strongly consistent where Cloudflare is eventually consistent.

#### C5 Queues
*Prod 3,143 · Tools 359 · Test 269 (19 ungated) · Solid · High*

**Dependencies:** a reserved `__Queue` DO class with SQL tables, the `celld_logic::queue` policy via a synchronous op, alarms, and the output gate.

**Complexity: High.** Leases with per-row generations, settlement fencing, retries and dead-letter queue (DLQ) transfers with idempotent receipts, purge-while-leased, and producer coalescing.

**Edge cases**
- **Settlement fencing is sound.** A late settlement from an expired lease cannot ack a newer delivery.
- **A lost settlement counts as a failed delivery.** An acked batch can be redelivered and eventually dead-lettered. The lease is about 316 s.
- **Retention can drop messages in transit to the DLQ.** The 4-day sweep deletes rows that still carry a pending DLQ target.
- **A failed wake wait after commit rejects every producer in the group**, although their messages are already committed. A producer that retries then creates duplicates.

**Readiness: Solid.** The limits are enforced at deploy time.

#### C6 D1
*Prod 1,126 · Tools 598 · Test 35 · Solid · Medium*

**Dependencies:** one DO cell per database, a raw-FFI statement executor, FTS5 allowed, and a CLI over the operator route.

**Complexity: Medium.** One `__d1_run` op covers prepared statements, batch, exec and migrate. The authorizer forbids `BEGIN`, `ATTACH` and `VACUUM`.

**Edge cases**
- **Batch, exec and migrate are atomic** (`BEGIN IMMEDIATE`). A failed rollback poisons the scope.
- **Migrations cannot double-apply**, because the name is UNIQUE and recorded in the same transaction.
- **No query time limit**, and results are capped at 100k rows or 32 MiB; past the cap a query fails instead of truncating.
- **A typo in the database name silently creates an empty database.**

**Readiness: Solid.** Ten documented differences: no replicas, no Time Travel, no REST API, no `dump()`.

#### C7 R2
*Prod 1,918 · Tools 525 · Solid · Medium*

**Dependencies:** the fleet bucket under the `r2/<bucket>/` prefix, through `object_store` multipart, `md5`/`sha1`/`sha2`, and a metadata envelope.

**Complexity: Medium.** A part-reorder buffer, single-PUT versus multipart selection, and R2 metadata semantics on stores that lack them.

**Edge cases**
- **Multipart uploads are node-local and in memory.** Resume fails across nodes or restarts (`js/r2_ops.rs:470-481`). This is documented.
- **Upload IDs are a process counter that restarts at 1** (`js/r2_ops.rs:364`).
- **Each `uploadPart` buffers the whole part in the V8 heap** and returns `etag: ""`.
- **Multi-key `delete` is not atomic**, and a `list({include})` page fails on any single head error.

**Readiness: Solid.** Rated **Yes**, with its gaps documented explicitly.

#### C8 Workflows
*Prod 1,454 · Test 141 (24 ungated) · Beta · Very High*

**Dependencies:** a reserved DO class per instance, a SQLite ledger, and alarms as the only driver. The engine is about 1,350 lines of harness JS.

**Complexity: Very High.**
- A replay engine with steps memoized by name and count.
- Quiescence detection, persisted sleeps and retries.
- Pause, resume and restart fenced by generations.
- Retention.

**Edge cases**
- **Replay determinism is unchecked.** A same-kind step mismatch, for example after a redeploy that reorders steps, returns the wrong memoized value.
- **A timed-out attempt keeps running** alongside its retry.
- **A crash mid-callback re-runs the attempt without counting it**, so it falls through to the alarm ceiling of 6. The instance may then be left "running" with no wake *(inferred)*.
- **Pending non-step awaits fail the instance after 60 s**, a limit specific to celld.

**Readiness: Beta.** Rated **Yes**, with listed gaps: no rollback, no REST API, no `schedules`.

#### C9 Cron Triggers
*Prod 615 · Tools 27 · Solid · Medium*

**Dependencies**
- A pure parser and planner (`crates/logic/cron.rs`).
- One reserved cell per script, plus its alarm.
- Every node arms the schedule at boot and at each adoption.

**Complexity: Medium.** It supports the Cloudflare dialect (L, LW, W, #n) and looks ahead 400 years.

**Edge cases**
- **At-least-once on a node crash mid-handler.** Fleet-wide deduplication holds.
- **A slow handler *skips* intervening occurrences.** The doc says "delays".
- **Retry state is in memory only.**
- **Arming can fail silently** until the next adoption, which every node's arm attempt mitigates.

**Readiness: Solid.** No unit tests sit next to the pure parser in this repo.

#### C10 Static assets
*Prod 1,685 · Tools 398 · Solid · Medium*

**Dependencies:** a SHA-256-verified asset index and blobs in the bucket, a local LRU disk cache (512 MiB), `regex`, and `axum`.

**Complexity: Medium.** It implements:
- `_redirects` and `_headers`;
- the html_handling and not_found handling modes;
- `run_worker_first`;
- Range and conditional requests;
- a version-skew fallback.

**Edge cases**
- **Cache hits are validated by size only.** Same-size disk corruption is served.
- **A cold miss reads the whole blob (up to 25 MiB) into memory** and fsyncs it before the first byte goes out.
- **An invalid header value after `_headers` substitution gives a 502.** It never reaches the Worker and is logged with `eprintln!`.
- **The default cache directory is the shared OS temp directory.**

**Readiness: Solid.** Tests for it are external (`assets.rs:1019` includes them).

#### C11 Dynamic Workers & Tail Workers
*Prod 1,231 · Tools 32 · Beta · High*

**Dependencies:** one V8 isolate per loaded Worker, a process-global registry (256 per process, 255 per script generation), CPU and subrequest limits, and the egress gate.

**Complexity: High.** Asynchronous load states, lifetimes driven by garbage collection, capability brokering, and merged limits.

**Edge cases**
- **Slots are freed only on `dispose()` or by GC.** A hot path that never disposes can hit the 256 cap.
- **No memory-based admission.** Up to 256 isolates of 128 MiB each are possible.
- **A failed `get()` stays cached under its id.**
- **Tail reports are best-effort `waitUntil` work, for fetch invocations only.**

**Readiness: Beta.** The upstream API is itself in beta.

#### C12 Containers & Sandbox
*Prod 2,098 · Tools 244 · Examples 154 · Experimental · High*

**Dependencies**
- The Docker or Podman Engine API over a Unix socket, through a hand-written `hyper` client.
- An nftables "fence" container (`NET_ADMIN`).
- Image tars in the bucket.
- An optional runtime such as `runsc` or `kata`.

**Complexity: High.**
- An external, stateful engine tied to cell ownership.
- Run numbering, idle-eviction adoption and sweepers.
- A fleet-converging `max_instances` cap.

**Edge cases**
- **Docker API calls have no timeout.** A hung daemon stalls cell stop and handoff.
- **Orphaned containers** after a celld crash, or after a hibernated cell is moved.
- **The whole image tar is loaded into memory.**
- **`max_instances` fails open** when the capacity sample is unreadable.
- **Container start is not behind the output gate.**

**Readiness: Experimental**, as the project itself declares.

### D — Distributed cell engine

All of D shares one dependency base:
- `tokio`, `hyper`, `object_store` 0.12, `rusqlite`, `crc-fast`, `lz4_flex`, `tikv-jemallocator`.
- `panic = "abort"` in release builds.
- A bucket that provides conditional create and overwrite, read-after-write consistency and exact ranged reads. Epoch GC also needs list-after-write consistency (`docs/guarantees.md:9-23`).
- A supervisor that restarts the process and waits at least one lease TTL first.

`celld-logic` has zero dependencies. The TLA+ specs and the deterministic simulator described in `docs/testing.md` are **not in this repo**.

#### D1 Decision core & cell lifecycle
*Prod 4,074 · Test 280 · Solid · Very High*

**Complexity: Very High.**
- 20 `Phase` variants interleave with lease authority, permits, capacity waits and drain pins.
- Every event goes through one serial `on_event`, followed by three pump passes.

**Edge cases**
- **Head-of-line blocking on the serial core.** A paged-restore page fault runs on the core thread for up to about 20 s (`crates/ltx/src/client/object_store.rs:673-684`).
- **State invariants are checked only in debug builds** (`crates/logic/lib.rs:6932`). In release, the 36 `expect`s are fatal to the process. A deterministic panic would follow its cell from node to node.
- **Ambiguous acquires are re-read, not retried blindly.** The re-read is capped at 3.
- **An unreadable capacity sample parks activations.**

**Readiness: Solid.** Decisions are cleanly separated from effects.

#### D2 Ownership leases & fencing
*Prod 2,979 · Test 70 (31 ungated) · Solid · Very High*

**Dependencies**
- Bucket CAS with zero transport retries: ETag dialect on S3 and Azure, generation match on GCS.
- Records: `nodes/<node>.json` leases and per-cell `{node, epoch}` ownership records.
- `CELLD_TTL_MS` (10 s), renewed at TTL/3.

**Complexity: Very High.** Renewal continuity, adoption of the node's own "zombie" writes after ambiguous renewals, three-way fencing, and takeover interlocked with log recovery.

**Edge cases**
- **The takeover decision compares a remote wall-clock expiry with the local wall clock, with no skew margin** (`crates/logic/lib.rs:4412`). Writes stay safe through ownership re-reads and seals. Stale *reads* are possible inside the skew window, and the TLA+ model assumes perfect clocks.
- **A 2026-09-03 fence storm under a slow store led to quarter-window retry pacing.**
- **An asymmetric partition keeps the lease live while peers cannot reach the node.**
- **A fenced node exits with code 3** and depends on its supervisor.

**Readiness: Solid.** This is the most-verified area by the docs' own account: TLA+ found a split-brain, and 5,500 contended claims showed one writer per epoch.

#### D3 SQLite replication & restore (LTX)
*Prod 12,557 (63% vendored) · Test 380 · Beta · Very High*

**Dependencies**
- `celld-ltx`, vendored 2026-08-03 from rustyriver (a Rust Litestream v0.5).
- `object_store` with separate LTX clients, `ureq` for paged range reads, `fsync`/rename.
- Pure decision modules: `output_gate`, `durability`, `epoch_gc`, `restore`.

**Complexity: Very High.** It spans four protocols:
- WAL capture with the checkpoint lock lattice;
- epoch-chain restore;
- a fault-in SQLite VFS;
- one output gate over every egress channel.

**Edge cases**
- **Stale-owner late writes land in dead epoch prefixes.** A bucket proof re-reads ownership before it acknowledges.
- **A failed proof resets the cell.** The proof budget was rebuilt after a 4,000-cell false-failure incident.
- **429 and 5xx responses become proof timeouts** after 3 retries in 30 s.
- **Epoch GC is off by default**, so bucket bytes grow with every activation. Turning it on requires list-after-write consistency.
- **Mixed-version fleets must disable L1 compaction and paging.**

**Readiness: Beta.** The vendored crate has been in-tree about 10 weeks. `ltx/src/replica.rs:18-40` still says "L0-only… deferred", although L1 compaction ships on by default. `docs/testing.md` gives no restore timings for celld-ltx yet.

#### D4 Fleet durability ensemble (node log)
*Prod 7,030 · Test 190 · Beta (→ Experimental) · Very High*

**Dependencies:** signed peer POSTs and an upgraded stream (`/peer/log/*`), follower `fsync`, the node lease record, and at least two live nodes.

**Complexity: Very High.**
- Write-all/ack-all with an empty-joiner reconfiguration.
- Seal-before-certify recovery with claimant takeover.
- Gray-follower eviction, hedged appends, and windowed bundle recovery.

**Edge cases**
- **One slow follower stalls every acknowledgement.** Eviction tuning was revised after lab incidents. The bucket upload races every fleet proof, which caps latency at the bucket proof.
- **Correlated loss is *declared* rather than prevented.** Recovery writes `log/<dead>.e<N>.loss.json` (`node_log.rs:5221-5262`), so RPO=0 needs at least one surviving follower disk.
- **Persistent follower HTTP errors can block recovery and startup.**
- **Follower responses are unauthenticated** (see D8).

**Readiness: Beta (leaning Experimental).** The header calls it "v0", yet it is the **default** durability mode. Headers are stale:
- `crates/logic/log_tier.rs:3-4` says "design stage; not yet wired", but it has 42 live call sites;
- the "base64 JSON" wire description is out of date;
- `log/<node>.json` should be `nodes/<node>.json`.

#### D5 Routing, ingress & peer transport
*Prod 2,372 · Beta · High*

**Dependencies:** `hyper` (http1 only), an h1-upgrade peer tunnel (64 idle tunnels per peer), plaintext peer traffic on a private network, a 1 GiB public body limit, and a 30 s idle timeout.

**Complexity: High.** Stale-route generation tracking, byte-faithful tunneling, WebSocket splicing, and Host/authority validation as a security fence.

**Edge cases**
- **Pooled-tunnel resend.** `send_inner` replays a buffered body on a fresh tunnel after *any* pooled-tunnel error, without knowing whether the request was delivered (`main/peer_tunnel.rs:428-468`). That bypasses `classify_remote_attempt`'s rule that ambiguous attempts are not retried (`main.rs:401-416`). It could run a non-idempotent DO request twice, contrary to `docs/guarantees.md:245-247`. **This needs a targeted test.**
- **Strict `peer_protocol` equality**, which is why several upgrades are stop-all.
- **Without the tunnel pool, about 1k calls/s is the OS ceiling** on ephemeral ports.

**Readiness: Beta.**

#### D6 Placement, pressure & drain
*Prod 4,060 · Test 83 · Beta · High*

**Dependencies:** cgroup v1/v2 files and `/proc/self/statm`, jemalloc stats and background purge, and the bucket objects `fleet/capacity-v1.json` and `drain/token.json`.

**Complexity: High.** Two hysteresis latches (an 80% threshold and a 95% hard cap), weighted-share rebalancing with a deadband, a drain token plus a readiness gate, and an LRU snapshot cache.

**Edge cases**
- **Only the cgroup mount root is read** (`memory.rs:40-56`). A systemd `MemoryMax=` on the unit's own cgroup is invisible, so the node falls back to 80% of host RAM.
- **A drain into a full fleet leaves cells dormant.**
- **The drain token is advisory.** A donor that waits past ¾ of the shutdown budget proceeds unserialized.
- **Rebalancing counts cells, not CPU or memory**, and moves only hibernated cells.

**Readiness: Beta.**

#### D7 Object-store backends
*Prod 1,161 · Solid · Medium*

**Dependencies**
- `object_store` S3 (`ETagMatch`), GCS (XML API with generation match) and Azure.
- Credentials: the AWS chain, Google ADC, Azure key/MSI/workload identity (public cloud only).
- Retries: 2 for ordinary operations, 0 for CAS.

**Complexity: Medium.** The error contract carries correctness: only a clean 412 or 409 means "not committed", and everything else is ambiguous.

**Edge cases**
- **The startup probe checks sequential CAS and ranged reads, but not concurrent atomicity or list-after-write consistency.**
- **After 3 ambiguous probe attempts the node starts with only a warning.**
- **Provider quirks:** MinIO, Azure App Service MSI, GCE read-only default scopes.
- **`docs/guarantees.md:69-71` omits the reserved `drain/`, `kv/` and `r2/` prefixes.**

**Readiness: Solid.**

#### D8 Peer authentication
*Prod 367 · Beta · Medium*

**Dependencies:** HMAC-SHA256 with nonces and a ±30 s clock window, a shared secret in `fleet/peer-auth.json`, and a trusted private network.

**Edge cases**
- **Peer responses are not authenticated** (`peer_auth.rs:322-335`).
- **Only tunnel establishment is signed.** Calls inside the tunnel and most operator routes are unauthenticated, which is documented.
- **`/peer/log/*` reads up to about 1 GiB of body before verifying the signature.**
- **The replay cache is in memory only, and there is no key rotation.**

**Readiness: Beta.** The docs themselves say "not safe for hostile multi-tenant use".

#### D9 Deployment adoption & generations
*Prod 1,337 · Test 26 (20 ungated) · Beta · Medium–High*

**Dependencies:** `deploy/current.json` plus named pointers, content-addressed versions, the `wake/format.json` format gate published in leases, and 30 s polling.

**Edge cases**
- **The multi-script snapshot is non-atomic.** Each pointer is re-read separately, so a node can adopt a mix of versions.
- **`Rollout.percent` is never read**, so there is no canary rollout.
- **A failed build is remembered** until the pointer changes or someone calls `POST /reload`.
- **Old binaries are kept out only by operator discipline.**

**Readiness: Beta.** Most minor releases needed a format or protocol transition, and five were stop-all or conditional.

### E — Operations & tooling

#### E1 `celld deploy`
*Tools 1,051, plus 2,031 deploy-tool lines attributed to per-service features · Solid · High*

**Dependencies:** `esbuild` (unpinned, no timeout), `docker` for container images, Pyodide from jsDelivr for Python, and bucket CAS.

**Edge cases**
- **The pointer CAS is head-then-put, so the last deploy wins.**
- **The named pointer and the fleet pointer are written non-atomically.** The immutable objects are written first, which is the real safety property.
- **The whole `docker save` output is buffered in memory.**
- **No rollback or secrets support.** `.dev.vars` is ignored by deploy.
- **The strict key allowlist refuses `routes`, `env` and `wrangler.toml`.**

**Readiness: Solid.**

#### E2 `celld dev`
*Tools 1,282 · Solid · Medium*

**Dependencies:** `notify`, an SQLite-backed local object store, process groups, and `PR_SET_PDEATHSIG` on Linux.

**Edge cases**
- **A failed build keeps the old app, but a failed node start ends the whole session** (`dev.rs:657-666`).
- **The recursive watch also registers `node_modules`**, so it can exhaust inotify watches.
- **A stuck node can stall each reload for up to 35 s.**

**Readiness: Solid** as a developer tool.

#### E3 Operator CLI & diagnose
*Tools 1,429, Prod 231 (server-side probe) · Solid · Medium*

**Dependencies:** `reqwest`, Ed25519 challenge probes, the fleet HMAC, and bucket listings.

**Edge cases**
- **`diagnose` bind-checks the default 127.0.0.1:8080**, so it fails on a host that already runs a node on that port (`main.rs:3561-3580`).
- **Peers are probed sequentially, at up to 5 s each.**
- **The probe writes to the bucket unless `--read-only` is given.**
- **The d1/kv/queue CLIs never retry a timeout**, so a migration cannot run twice. This is correct by design.

**Readiness: Solid.**

#### E4 Node process, CLI & operator API
*Prod 2,276 · Test 121 · Solid · Very High*

**Dependencies:** `tokio`, `tracing` with a lossy non-blocking appender, `jemalloc`, a raised `RLIMIT_NOFILE`, and SIGTERM/SIGINT handling.

**Complexity: Very High.** `async_main` is one function of about 2,140 lines. Shutdown runs seven phases under one deadline.

**Edge cases**
- **The default shutdown budget is 40 s, longer than Kubernetes' default 30 s grace.** This is documented but not enforced.
- **An `accept()` error such as EMFILE exits the node without a handoff** (`main.rs:4752,4763`).
- **Misspelled environment variables are silently ignored**, and empty values are handled inconsistently.
- **`CELLD_TEST_*` hooks ship in the binary.**
- **The operator API is unauthenticated** and labelled "alpha".

**Readiness: Solid.**

#### E5 Telemetry
*Prod 1,367 · Beta · Medium*

**Dependencies:** `parquet` (no arrow), a hand-written OTLP protobuf encoder, `reqwest`, and the bucket or a collector. Telemetry is off by default.

**Edge cases**
- **No flush at shutdown, so up to 5 minutes of data are lost per stop or crash.**
- **Bucket PUT failures are not retried.**
- **A collector outage blocks the pump** (head-of-line blocking).
- **No compaction, and no metrics.**

**Readiness: Beta.** The schema is stamped `v0-unstable`.

#### E6 Managed control plane (celld.dev)
*Prod 2,266 · Tools 342 · Experimental · High*

**Dependencies**
- The `https://celld.dev` API and a presence WebSocket.
- Managed R2 credentials, or bring-your-own S3.
- `execv` re-exec on credential rotation.

**Edge cases**
- **Credential rotation re-execs the process, bypassing the graceful drain and handoff.**
- **One transient error during the first-deploy wait aborts startup.**
- **The managed deploy agent writes both pointers with plain PUT, not CAS** (`control_plane.rs:1922-1938`).
- **The explorer lets celld.dev read up to 25 rows of any cell table.** This is an undocumented data-egress path.

**Readiness: Experimental.**
- It is absent from `docs/` and `--help`; the only mention is two removed-settings rows.
- The server side is not in this repository.

---

## 8. Cross-cutting findings

1. **Test evidence is external.**
   - About 40 `include!(env!(…))` hooks pull in conformance, simulation and unit suites from outside the repository.
   - The Dockerfile test stage therefore proves only that the code compiles and passes clippy.
   - `docs/testing.md` describes differential testing against workerd, TLA+ (maintained by hand, not in CI) and fault-injection fleet labs. None of that can be checked here.
2. **Test hooks ship in release.** The ungated paths total 256 lines:
   - the `CELLD_TEST_SCRIPT_PATH`, `CELLD_TEST_FAIL_PUBLISH_ONCE`, `CELLD_TEST_DATA_DIR` and `CELLD_TEST_BUCKET` hooks;
   - the `CELLD_TEST_KV` knobs;
   - `CELLD_TEST_GENERATION`.

   A misconfigured environment can change node behaviour.
3. **Some knobs are undocumented:** `CELLD_FETCH_TIMEOUT_S`, `CELLD_HANDLER_BUDGET_S`, `CELLD_ALARM_RESIDENT_MS`, and the whole managed mode (`CELLD_CLOUD`, `connect`/`token`).
4. **Stale headers and comments:**
   - `logic/log_tier.rs` ("not yet wired");
   - the `node_log.rs` wire format and record path;
   - `ltx/src/replica.rs` ("L0-only");
   - `js/node_crypto.js:22`;
   - the "replication process" log text in `main.rs`;
   - "shares fleet buckets with celld" in `dead_node_gc.rs`, a leftover from a rename.
5. **Code hygiene is strong otherwise.**
   - There are no first-party TODO/FIXME markers; all 19 hits are in upstream-ported code.
   - There are no `unimplemented!` or `todo!` macros.
   - `clippy.toml` forbids raw tokio spawn/sleep, clocks, RNG and fs inside the execution boundary, so a simulator can drive it.
   - Comments record dated incidents and the fixes they led to.
6. **Unsafe Rust is concentrated** in `storage.rs` (51 sites, raw SQLite FFI) and `ltx/src/paged_vfs.rs` (20, the SQLite VFS).

---

## Appendix A — Attribution judgment calls that move numbers

| Decision | Effect |
|---|---|
| `dispatch_do_call` in `main.rs` goes to C1, although much of its body is remote forwarding | C1 +~320 lines |
| The output gate, egress gates and the WebSocket frame-flush barrier (`ws_registry.rs:839-1265`) go to D3 | D3 +~430 lines; B6 would grow by the same amount if they went there instead |
| Idle eviction stays in D1; only shedding and drain go to D6 | – |
| Clean-reload pieces go to D2 | – |
| Epoch GC in `node_log.rs` goes to D3; the rest of `node_log.rs` goes to D4 | – |
| Read-only snapshot restore for the explorer goes to E6 | E6 +~180 lines; restore code arguably belongs to D3 |
| Per-binding code in the deploy pipeline, `bootstrap.rs` `build_env`, `fleet.rs` and `generation.rs` goes to the service features | This is why many C rows carry tools lines |
| `deploy.rs` reserved-class constants (66-184) and queue attachment publishing stay **production**, because nodes use them | – |
| The `LAZY_MODULES` builtin-source tables in `modules.rs` count as A2, not B2/B5 | – |
| `crates/logic/schedule.rs` (cell-isolate dispatch and WebSocket close-code helpers) goes to A3 | – |

## Appendix B — Artifacts

`report/method/` contains:

| File | Contents |
|---|---|
| `feature-map.spec` | The 1,428 attribution rules |
| `ungated-test-overrides.txt` | The ungated test ranges |
| `loc.py` | Lexer, test-gate detector and coverage validator |
| `merge.py` | Applies the ungated-test overrides |
| `aggregate.py`, `tables.py` | Build the report tables |
| `taxonomy.md` | Feature definitions |
