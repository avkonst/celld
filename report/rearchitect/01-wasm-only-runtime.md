# 01 — Wasm-only application runtime instead of V8 isolates

**Status:** Explored (design sketch, nothing prototyped yet)
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Revisions:**
- 2026-10-14 (a): Replaced deploy-time precompilation into the bucket with node-side compilation and a local cache. Added §3.2.1 (code trust) and §3.2.2 (the Spectre boundary).
- 2026-10-14 (b): §3.2.1 now supports three configurable code-trust modes: `compile` (default), `signed` precompiled artifacts, and opt-in `unsigned` artifacts for closed deployments.

---

## 1. Premise

Today celld runs every application inside V8 isolates. This holds even for Rust and Python:
- **Rust** compiles to wasm through workers-rs. That build emits a JavaScript shim, and the wasm module calls Cloudflare-shaped JS bindings through wasm-bindgen (`docs/wasm.md`, `examples/wasm`).
- **Python** is Pyodide, an Emscripten build of CPython. It expects a JS host, and celld even fakes a browser Web Worker for it (`crates/celld/python/shims.js`).
- **Every binding** (Durable Object storage, KV, Queues, D1, R2, Workflows, fetch, streams, crypto, WebSockets) is a JS object implemented in the harness. These are mostly `js/harness.js` and `js/*.js`, backed by Rust host ops.

V8 is required because the product promise is Cloudflare compatibility, and on Cloudflare wasm always runs inside a JS host.

**This exploration asks: if we gave up Cloudflare compatibility, what would a wasm-only runtime look like, and what would it buy us?**

## 2. Goals and non-goals

**Goals**
- Capability-based isolation. A cell can do only what its declared contract allows, and the check happens at deploy time.
- Hard, enforceable limits on memory and CPU for each cell. celld's V8 runtime (the reference) has **no CPU-time limit for deployed Workers or Durable Objects** (report §7, A1).
- Host-mediated networking (HTTP, TCP for databases, WebSockets) and timers, so policy, accounting and the durability gate live in one place.
- An eviction rule simple enough to live in the pure `celld-logic` crate.
- Higher density and faster cold starts than V8 isolates.

**Non-goals**
- Running existing Cloudflare Workers, workers-rs or Python Workers projects unchanged.
- Keeping JS as the primary guest language. Section 3.9 covers the options.
- Changing the distributed engine. Ownership, replication, the fleet log and placement (area D) stay as they are.

---

## 3. Design

### 3.1 Runtime choice

- **wasmtime with the WASI 0.2 component model.** It is written in Rust, and it is embeddable in the existing tokio runtime through `async_support`.
- **Three code-trust modes, chosen per node by configuration** (§3.2.1). The portable `.wasm` component is always in the bucket.
  - **`compile` (default).** The node validates and compiles the `.wasm` with its own Cranelift.
  - **`signed`.** The node loads a precompiled artifact from the bucket only if a trusted key signed it.
  - **`unsigned`.** An explicit opt-in for closed deployments. The node loads precompiled artifacts from the bucket without a signature.

  In every mode, the node caches the compiled artifact on **local disk**. The cache key is the component's SHA-256 plus an engine fingerprint: wasmtime version, enabled wasm features, Cranelift settings and target CPU. This mirrors today's V8 path, where each node compiles JS and keeps a local code cache.
- **Use the pooling instance allocator with copy-on-write memory images.** Instantiation then takes microseconds, and resident memory is only the pages a guest touches.
- **Use one `Store` per cell activation.** The `Store` is the unit of isolation, limits and eviction.

### 3.2 Isolation model

- **Memory safety.** A guest has its own linear memory, bounds-checked with guard pages, and cannot address host memory.
- **No ambient authority.** The guest has no filesystem, network, clock, randomness or environment unless the host links that import. JavaScript is the opposite: globals such as `fetch` exist until you remove them.
- **Narrow boundary.** Every effect crosses the component boundary through typed WIT interfaces. Nothing is reachable by accident.
- **Caveat: the host imports are part of the sandbox.** Every host function exposed to guests is attack surface. A bug in the `celld:storage` or HTTP implementation can be an escape.
- **Caveat: the runtime is part of the sandbox.** Wasmtime and Cranelift have had sandbox-escape bugs, for example a 2023 x86-64 Cranelift miscompilation advisory (believed to be CVE-2023-26489; confirm before citing). Track wasmtime security releases and patch promptly.
- **Caveat: precompiled code is only as trustworthy as its source.** The node compiles it itself (`compile`), a trusted key signed it (`signed`), or every bucket writer is trusted (opt-in `unsigned`). See §3.2.1.
- **Caveat: wasm is not a Spectre boundary** (§3.2.2).

#### 3.2.1 Code trust: `.wasm` versus precompiled artifacts

| Artifact | What it is | Safe if untrusted? | Why |
|---|---|---|---|
| `.wasm` component | Portable bytecode | **Yes** | The runtime **validates** it (types, control flow, memory access shapes). Then its own trusted compiler emits machine code with bounds checks and other safety checks. The safety comes from that validate-and-compile step. |
| Precompiled `.cwasm` (`Engine::precompile_component` / `Component::serialize`) | Native machine code the compiler already emitted | **No** | Native code cannot be validated. `Component::deserialize` checks only that the artifact matches the engine version and configuration, not that the code is honest, which is why it is `unsafe`. A tampered artifact runs as arbitrary native code inside the celld process, with all the node's privileges. |

**Why this matters for celld.**
- `docs/security.md` already says to treat bucket access as fleet-administrator access.
- If deploy stored precompiled artifacts in the bucket, write access to the bucket would turn into **native code execution on every node**. That widens the boundary.
- It is a real escalation for any future setup where tenants, CI systems or the managed control plane (E6) write deploy prefixes.

**Decision: support all three modes, as node configuration.** Each mode trades compile cost on the node against who must be trusted.

| Mode | What the node loads | Who must be trusted | Does bucket write access give native code execution? | Cost |
|---|---|---|---|---|
| **`compile`** (default) | Only `.wasm`. It compiles locally and ignores any precompiled artifacts in the bucket. | Wasmtime/Cranelift and the host imports | **No** | One compile per node per version (ms to s) |
| **`signed`** | A precompiled artifact, if its signature verifies against a configured trusted key. Otherwise it follows the fallback policy. | The holder of the signing key (deploy pipeline, KMS or HSM), plus the above | **No.** Without the private key, a bucket writer can only cause a refusal or a fallback. | Key management; the deploy pipeline builds one artifact per CPU target |
| **`unsigned`** (opt-in) | A precompiled artifact from the bucket, with no signature | **Everyone who can write the bucket**, plus the above | **Yes, by design** | Smallest node-side cost, weakest boundary |

##### Mode `compile` (default)

- The bucket holds `.wasm`, and the node never reads precompiled artifacts in this mode. A `.wasm` is safe even if an untrusted party wrote it, subject to the caveats in §3.2.
- The compiled artifact lives only on the node's disk, under its data directory (`CELLD_WATCH`). Nothing else writes there.
- **Cost:** compile time is paid once per node per version, typically milliseconds to a few seconds depending on component size. Paying it at adoption, before the new generation serves, keeps it off the request path. That matches today's generation build (D9).
- A cache miss recompiles. That happens after a node restart without `CELLD_WATCH`, or on a fresh node. Size the deploy convergence window (`CELLD_DEPLOY_MAX_AGE_S`) for the largest component.

##### Mode `signed`

**Deploy side.**
- `celld deploy --precompile --targets x86_64-linux,aarch64-linux --sign-key <file|kms-uri>` compiles one artifact per target and engine fingerprint.
- It uploads each artifact next to the `.wasm`, plus a signed artifact list (format below).
- The private key never goes to the bucket. KMS- or HSM-backed signing keeps it off build machines too.

**Signed artifact list.** The list records, for each entry: the deployment version, the component `sha256`, the target and engine fingerprint, the artifact `sha256`, and the signing key ID. It is signed with **ed25519**, the same primitive celld's `peer_probe.rs` already uses. Binding the signature to the component digest and deployment version stops replay: an attacker cannot reuse a validly signed artifact under a different component or deployment.

**Node side.**
- `CELLD_WASM_TRUSTED_KEYS` lists ed25519 public keys. Listing several allows rotation; removing a key revokes it.
- The keys come from node configuration (a file, environment variable or secret mount), **never from the bucket**.
- The node verifies the signature first, then the artifact digest, then the engine fingerprint. Only then does it deserialize the artifact, and it writes it to the local cache.

**Fallback policy** (`CELLD_WASM_ARTIFACT_FALLBACK`).
- `compile` (default): if no artifact exists for this node's target or fingerprint, or verification fails, the node compiles the `.wasm` locally. Compiling `.wasm` is always safe, so fallback never lowers security. A verification **failure** is still logged as a security event, because it means a bad signature or an unknown key.
- `refuse`: the node refuses to adopt the deployment. Use this when compile latency must never appear in production, or when a verification failure should stop a rollout.

**Upgrades.** A wasmtime upgrade changes the engine fingerprint. An upgraded node therefore finds no matching artifact. It falls back to compiling, or refuses under `refuse`, until a deploy is re-published with artifacts for the new fingerprint. Rolling-upgrade runbooks must say this.

**Why not an HMAC with the fleet secret?** That secret lives in `fleet/peer-auth.json`, in the same bucket an attacker could write. An asymmetric key held outside the bucket is required.

##### Mode `unsigned` (opt-in, closed deployments only)

**When to use it.** Only where everyone who can write the bucket is already trusted as a fleet administrator: a closed, single-organisation fleet, with a bucket only its own deploy pipeline writes to. `docs/security.md` already treats bucket access as fleet-administrator access. This mode makes that literal: **bucket write access means native code execution on every node.**

**Integrity, not authenticity.** The node still checks the artifact digest and engine fingerprint. That catches corruption and version mismatch, not tampering.

**How to enable it.** It must be explicit and visible:
- Enable it only with `CELLD_WASM_ARTIFACTS=unsigned`. Never make it the default, and never infer it from other settings.
- Log a warning at startup that names the consequence.
- Report the mode in `/state` and `celld diagnose`, so operators can audit it fleet-wide.
- Recommend running it with `fallback=compile`, so a missing artifact does not stop adoption.

**Never in shared buckets.** Refuse to start in `unsigned` mode when managed mode (E6) or any multi-tenant configuration is active. In those setups, other parties can write deploy prefixes.

##### Shared rules (all modes)

- **Configuration.** Use node-level environment variables, validated strictly like every other `CELLD_*` setting (E4):

  | Variable | Values | Default |
  |---|---|---|
  | `CELLD_WASM_ARTIFACTS` | `compile`, `signed`, `unsigned` | `compile` |
  | `CELLD_WASM_TRUSTED_KEYS` | ed25519 public keys | – (required for `signed`) |
  | `CELLD_WASM_ARTIFACT_FALLBACK` | `compile`, `refuse` | `compile` |

  A misspelled value refuses startup.
- **Mixed fleets are fine.** The mode is a per-node security policy, not a bucket format. A node in `compile` mode ignores artifacts that `signed` or `unsigned` nodes use. Precompiled artifacts are optional extra objects next to the `.wasm`. A deployment without them works in every mode except `fallback=refuse`.
- **The local cache is trusted only because the node wrote it itself.** If the node's disk is shared or untrusted, disable the cache.
- **Observability.** Count compiles, cache hits, verified loads, fallbacks and verification failures per mode, so a fleet sees its compile cost and any tampering attempts.

#### 3.2.2 What "not a Spectre boundary" means

**Two kinds of boundary.**
- **Architectural isolation.** The guest cannot *legitimately* read or write outside its linear memory. Bounds checks and guard pages enforce this, and wasm provides it.
- **Spectre resistance.** Modern CPUs speculatively execute past a branch, such as a bounds check, before they know whether it passes. Speculatively executed loads can touch memory the program was never allowed to read. The results are discarded architecturally, but they leave traces in the CPU caches. An attacker who can time memory accesses precisely can recover those bytes.
- So Spectre is a **read-only leak**, not an escape. The guest cannot write outside its sandbox, but it may be able to *read* anything in the same address space.

**Why the process is the boundary.**
- After 2018, browser vendors concluded that software sandboxes inside one process cannot fully stop speculative leaks, and moved to process isolation (Chrome's Site Isolation).
- A **Spectre boundary** therefore means a separation the attacker's speculation cannot reach. In practice that is a **separate process** (a separate address space), plus the OS and CPU mitigations applied on context switches.
- Any wasm guest in the celld process shares an address space with everything else in that process.

**What a guest could leak in celld.** Within one node process:
- other cells' data in memory: SQLite page caches, buffered bodies, other guests' linear memories;
- the fleet HMAC secret (`fleet/peer-auth.json`);
- bucket and cloud credentials;
- TLS session keys.

**Mitigations, from cheapest to strongest**
1. **Compiler mitigations.** wasmtime can emit Spectre-hardened bounds checks for linear memory and table accesses: conditional moves instead of plain branches, so speculation cannot use an out-of-bounds index. Keep these enabled. They address the classic bounds-check-bypass variant (Spectre v1), not every variant.
2. **Deny precise timers.** A Spectre attack needs a high-resolution clock. Because clocks are host imports (§3.7), the host can freeze or coarsen time during execution, as Cloudflare and celld's V8 runtime already do. Also deny guest threads and shared memory, which can be used to build a timer.
3. **Keep secrets out of guest-hosting processes.** Move the fleet secret, bucket credentials and TLS keys into a separate broker process, so the process running guests holds as little as possible.
4. **Process separation per tenant.** Run cells of different tenants in different worker processes; cells of one tenant may share. This is the real Spectre boundary. Cloudflare adds dynamic process isolation for suspicious workers on top of its isolate model.

**Which level celld needs.**

| Deployment | Recommended level |
|---|---|
| Single-tenant fleet (one organisation runs only its own code, as celld is mostly deployed today) | 1 + 2. Spectre is a low concern, because every guest is already trusted not to attack its neighbours. |
| Multi-tenant fleet running mutually untrusted code | All four levels. Plan process-per-tenant from the start; it changes the node architecture (one supervisor plus worker processes). |

### 3.3 Defining what an app can do: the WIT world as permission set

The host publishes a WIT package. A **world** is the full list of capabilities a cell may use:

```wit
package celld:platform@0.1.0;

world cell {
  // Effects — each import is a capability the host may or may not grant
  import wasi:http/outgoing-handler;     // outbound HTTP, host-mediated (3.6)
  import wasi:sockets/tcp;               // raw TCP, granted per app (3.6)
  import wasi:clocks/monotonic-clock;    // in-memory timers (3.7)
  import wasi:clocks/wall-clock;
  import wasi:random/random;
  import celld:storage/sql;              // the cell's own SQLite (replaces DO storage)
  import celld:storage/kv;
  import celld:alarm/schedule;           // durable timers (3.7)
  import celld:cells/call;               // call another cell (replaces DO stubs / RPC)
  import celld:postgres/query;           // optional pooled DB capability (3.6)

  // Entrypoints the host calls
  export celld:cell/handler;             // on-request, on-alarm, on-websocket-message, ...
}
```

**Enforcement**
- **Deploy time.** `celld deploy` inspects the component's imports. An import the app's grant does not include is refused before publication. This extends the current "refuse unsupported config at deploy" rule (E1).
- **Instantiation time.** The linker links only the granted interfaces. An ungranted import can also be linked to a deny-stub that returns a typed error, if graceful degradation is wanted.
- **Call time.** Fine-grained policy lives in the host implementation of each import, not in the WIT:
  - egress allowlists by host and port;
  - per-cell quotas: concurrent outgoing requests, bytes buffered, open sockets;
  - subrequest counts;
  - durability gating (3.8).

**Per-app grant.** The deploy manifest (`protocol.rs`) gains a `capabilities` section, for example `{ http: { allow: ["api.example.com"] }, tcp: { allow: ["db.internal:5432"] } }`. The host builds each `Store`'s linker and policy from that section.

### 3.4 Limiting RAM

**Guest linear memory**
- Each `Store` gets a `ResourceLimiter`: the stock `StoreLimitsBuilder::new().memory_size(N)` or a custom limiter.
- A custom limiter can also report growth to the node's pressure classifier (D6, `crates/logic/pressure.rs`). Pressure decisions would then use exact per-cell numbers instead of process RSS.
- When `memory.grow` would pass the cap, the growth fails inside the guest. That typically becomes an OOM trap that kills only that instance.

**Host-side memory per guest.** The wasm limit covers only linear memory. The host must count what it holds on a guest's behalf and charge it to the store: buffered request and response bodies, stream buffers, socket buffers, resource-table entries, pending outgoing requests. celld already has this gap today: V8 heap accounting does not see Pyodide's wasm memory (report §7, A6).

**Memory never shrinks.**
- Wasm memory can only grow, so a long-lived instance stays at its peak size.
- Reclaim by **dropping the store and re-instantiating**. This is cheap with pooling and copy-on-write (3.1).
- This pushes the design towards a "durable state in storage, memory is a cache" contract. celld already follows that for Durable Objects.

**Density.** A Rust guest typically touches well under a few MB, compared with a multi-MB V8 isolate baseline. Interpreted guests are heavier (3.9).

### 3.5 Limiting CPU

**Epoch interruption, for production.** It is cheap, and it preempts even tight loops:

```rust
let mut config = wasmtime::Config::new();
config.async_support(true).epoch_interruption(true);
let engine = wasmtime::Engine::new(&config)?;

// One ticker thread per engine (the whole node):
std::thread::spawn({ let engine = engine.clone(); move || loop {
    std::thread::sleep(std::time::Duration::from_millis(10));
    engine.increment_epoch();
}});

// Per store:
store.set_epoch_deadline(1);
store.epoch_deadline_callback(move |_ctx| {
    budget.charge_tick()?;                       // Err(..) traps: CPU limit exceeded
    Ok(wasmtime::UpdateDeadline::Yield(1))       // otherwise yield to tokio, continue
});
```

This gives three things:
- **Fair time-slicing.** A spinning guest yields its tokio worker every tick, so one cell cannot starve the node.
- **A hard per-invocation CPU budget.** Trap after N ticks. This closes the A1 gap.
- **One shared mechanism** for Dynamic Workers (C11, which today has a watchdog thread and thread CPU clocks) and for ordinary cells.

**Fuel, for the simulator.** Fuel counts instructions deterministically, at a higher runtime cost. In the deterministic simulator (`docs/testing.md`), fuel makes CPU-limit failures reproduce exactly from a seed.

**Wall-clock budget.** Keep a per-call wall-clock budget, like today's `CELLD_HANDLER_BUDGET_S`, for guests that are blocked on I/O rather than computing.

### 3.6 Networking

**Outbound HTTP: `wasi:http/outgoing-handler`.**
- The host performs the request with hyper and reqwest, so it owns DNS, TLS roots, timeouts, connection pooling, egress allowlists, subrequest accounting and durability gating.
- The guest sees only request and response resources and their body streams.
- This replaces B1's dual JS/Rust body machinery: 1,177 JS lines plus 2,242 Rust lines today.

**Raw TCP, for Postgres, Redis and similar: `wasi:sockets/tcp`.**
- The host decides per connect through wasmtime-wasi's socket policy hooks: an address check, and toggles for TCP, UDP and name lookup.
- **TLS has two options:**
  - **Guest-side:** rustls compiled into the guest and running over the socket. The host stays simple, but every guest carries a TLS stack.
  - **Host-side:** a `celld:tls` interface or a `start-tls` call. This centralises certificate policy and keeps guests small. It replaces today's `startTls` registry (B8), whose lock hazard the report lists.

**Databases as a host capability (recommended): `celld:postgres/query`.**
- The host keeps connection pools, in the manner of Cloudflare's Hyperdrive.
- With thousands of cells, raw per-cell connections exhaust the database. The host can pool, cap per tenant, and keep connections alive across instance eviction (3.10).

**Inbound HTTP.** The host terminates HTTP as today (D5) and calls the `handler.on-request` export with an incoming-request resource.

### 3.7 Timers

**In-memory timers.**
- They come from `wasi:clocks/monotonic-clock`: `subscribe-duration` / `subscribe-instant` return a pollable that the host backs with tokio.
- Because clocks are imports, the host can **virtualize time**. It can freeze time during execution, as Cloudflare does for side-channel resistance, or drive time from the simulator.
- They die with the instance.

**Durable timers.**
- These use a host interface, `celld:alarm/schedule.set(at)`. The host persists the alarm in the cell's SQLite, and it is published through today's wake machinery (C2: wake entries, arm gates, the waker).
- When the alarm fires, the host instantiates the cell if needed and calls `handler.on-alarm`.
- This keeps the existing alarm guarantees and removes their JS half.

**Async model (checked 2026-10-14).** Build on **WASI 0.3 / component-model async**:
- WASI 0.3.0 was ratified on 2026-06-11. Wasmtime 46 (2026-06-22) enables WASI 0.3 and the `component-model-async` wasm feature by default.
- Exports can be `async`, and the host can run several tasks on one instance. Each task runs until it awaits, so requests to a cell interleave at await points, as in a V8 isolate.
- Pin wasmtime ≥ 46. CVE-2026-27195 was a host panic when an async export's future is dropped and the instance is called again, fixed in 42+. Our cancel-by-drop paths must be tested.
- Guest toolchains: Rust `wit-bindgen` maps `async func` to `async fn`; Python, JS, C# and C async bindings were still in progress in mid-2026.

### 3.8 Durability (output) gate at the boundary

Today the rule "nothing that reveals unproven state leaves the process" (`crates/logic/output_gate.rs`) is enforced across many JS egress paths. That includes per-chunk tickets for streamed bodies, WebSocket frame barriers, R2, TCP and more (report D3).

With wasm, every egress is a host import:
- `outgoing-handler.handle`;
- writes to response body streams;
- WebSocket sends;
- socket writes.

Each import implementation awaits the cell's current durability proof before the bytes leave. The gate keeps its decision logic in `celld-logic`, but now has **one enforcement layer instead of one per JS API**.

### 3.9 Guest languages

| Language | Path | Notes |
|---|---|---|
| Rust | `cargo component` / `wit-bindgen` | First-class. Small guests, native speed. |
| Go, C/C++, Zig, .NET | WASI 0.2 toolchains | Usable. Maturity varies by toolchain. |
| Python | componentize-py | Interpreted, tens of MB. Pre-initialize at deploy with a Wizer-style snapshot for fast starts. |
| JavaScript | StarlingMonkey (SpiderMonkey to wasm) or QuickJS | No JIT inside wasm, so much slower than V8. Supported but second-class. |

**Two-engine option.** Add V8 for JS guests and run everything else on wasmtime. Both runtimes would sit behind the same `celld-logic` decisions and the same capability model.

### 3.10 Eviction: detecting when an unused instance can go

This is the biggest simplification compared with V8. In JS, work can hide inside promise chains, which is why celld tracks turns, affiliations, residencies, `may_free` and input-gate claims (A3, C1). In wasm, **everything a guest can wait on is a host object**, so the host knows exactly what each instance is doing.

**An instance is evictable when all of these hold:**
1. **No export call is in flight.** No `on-request`, `on-alarm` or `on-websocket-message` is executing or awaiting a pollable.
2. **The resource table holds no live non-durable resources:**
   - outgoing requests;
   - unfinished response body streams;
   - raw sockets;
   - in-memory timer pollables.
3. **No background work is pending.** This is the `waitUntil` equivalent, modelled as a host-tracked task list such as a `celld:cell/background.spawn` import, never as hidden guest state.
4. **Policy says go.** The idle timeout has expired, the node is under memory pressure (D6), or rebalance or drain wants the cell (D6/D9).

**These do not block eviction:**
- **Host-held WebSockets.** The host keeps the socket and calls `on-websocket-message` on a fresh instance. This is today's hibernation model, mapped one-to-one.
- **Durable alarms.** They wake the cell later.
- **Pooled database connections.** They belong to the host pool, not the instance.

**Evict by dropping the `Store`.** The next event re-instantiates in microseconds, and the code reloads its state from storage. Running instances that hold sockets are not snapshotted. The "durable state in storage" contract makes that unnecessary.

**Placement in the code.** The predicate is a pure function of host-visible counters, so it belongs in `crates/logic`, next to today's lifecycle phases (D1). It can be tested in the deterministic simulator.

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| **D: distributed engine.** Ownership (D2), replication (D3), fleet log (D4), routing (D5), placement (D6), object store (D7), peer auth (D8), deployment adoption (D9) | **Unchanged in principle.** It already treats a cell as "SQLite file plus events", and the runtime is just the effect executor. D9 gains the capability manifest, the optional signed artifact list, and an adoption step that compiles or verifies according to the node's code-trust mode and fills the node-local artifact cache (§3.2.1). D6 gains exact per-cell memory figures. |
| **D1: actor and lifecycle** | Lifecycle effects (StartRuntime, Publish, StopRuntime) map to instantiate, ready and drop store. Eviction is the predicate in 3.10. |
| **A3: runtime host and pool** | The V8 isolate pool becomes the wasmtime pooling allocator plus one `Store` per cell. The bin-packing policy in `logic/isolate.rs` (up to 32 cells per isolate) disappears, because each cell gets its own instance. |
| **C: services** | The Rust host halves stay and become WIT import implementations: SQLite storage, wake and alarms, the queue policy, KV blobs, the R2 store, assets, containers. The JS halves are removed. |
| **E: operations** | `celld deploy` builds or validates components instead of running esbuild, checks capabilities, and uploads the `.wasm`. With `--precompile`, it also uploads per-target artifacts, signed with `--sign-key` or unsigned for closed fleets (§3.2.1). `/state` and `celld diagnose` report each node's code-trust mode. `celld dev` keeps its supervisor. Telemetry gains per-cell CPU-tick and memory figures. |

---

## 5. Impact on the codebase

All counts are production code lines from the baseline report. Rows marked *(est.)* are rough judgements; the per-feature Rust/JS split was measured.

### 5.1 Removed: the JS application runtime

| What | Code lines | Notes |
|---|--:|---|
| All harness and polyfill JavaScript in areas A, B and C | **29,791** | `harness.js`, the `js/*.js` API polyfills, `services/kv.js`, `python/*.js`. 11.2k lines of this are ported Node.js code. |
| B5 Node.js compatibility | 13,465 (in total, Rust + JS) | Not needed. A guest brings its own standard library. |
| A5 WebAssembly-in-V8 and A6 Pyodide-in-V8 | 1,154 (production + tools) | Replaced by native component loading and componentize-py. |
| V8-specific Rust host code: `js.rs` and the `js/*.rs` op layers | about 9–10k *(est.)* | `js.rs` alone has 806 `v8::` call sites. The op bodies that wrap real logic (storage, R2, crypto) move into WIT implementations, not into the bin. |

### 5.2 Rewritten: thin WIT host implementations over existing Rust

| What | Today (Rust production code) | Expected effort |
|---|--:|---|
| C1 storage (SQLite, transactions, authorizer) | 4,538 | Keep `storage.rs`, and replace `js/storage_ops.rs` with `celld:storage/*`. |
| C2 alarms and wake | 2,612 | Keep. Add `celld:alarm/schedule`. |
| C4 KV, C5 Queues, C6 D1, C7 R2 | 4,438 | Keep the host parts. The JS cell logic (about 2.9k lines, chiefly the Queue broker in `harness.js`) is re-implemented in Rust on the host, or dropped if those services are out of scope. |
| C8 Workflows | 103 Rust / 1,351 JS | The engine is JS-only today. It must be rewritten (in Rust, on the host) or dropped. |
| B1 fetch, B6 WebSockets, B8 TCP | 5,635 | They collapse onto `wasi:http`, host-held WebSockets and `wasi:sockets`. Much of `http_streams.rs` and `ws_registry.rs` simplifies. |
| B4 crypto | 1,689 Rust / 2,188 JS | Guests bring their own crypto libraries, so it can be dropped, or a small `celld:crypto` can be offered. |

### 5.3 Kept as is

- **Area D:** 35,937 lines, including the `celld-ltx` crate.
- **Most of area E:** 6,140 production lines and 4,104 tool lines. Deploy needs new build steps.
- **`crates/logic`:** 8,720 lines. It gains the eviction predicate and the capability-policy decisions.

### 5.4 Code impact summary (LOC)

Scope: the runtime and service areas A+B+C, **65,227 production code lines** today (35,389 Rust, 29,791 JS, 47 Python). Areas D and E are unchanged apart from small additions, which are listed below. Rows marked *(est.)* are estimates.

| | Production | Notes |
|---|--:|---|
| **Removed:** all runtime JavaScript (harness, API polyfills, Node.js compat, `services/kv.js`, `python/*.js`) | **−29,791** | Measured. Includes 11.2k lines of ported Node.js code. |
| **Removed:** V8-specific Rust (the op layers in `js.rs` and `js/*.rs`, bootstrap, module loader, isolate glue) | **−9,000 to −10,000** *(est.)* | `js.rs` alone has 806 `v8::` call sites. The logic these ops wrap moves into WIT implementations, so it isn't deleted. |
| **Rewritten:** service logic that exists only in JS today (Queue broker, Workflows engine, KV/D1/R2 cell logic) | −4,265 JS (counted in the first row) → **+3,000 to +4,000** Rust *(est.)* | Applies to this doc standing alone. It is **0** when docs 05–08 apply, because those services are not built or become app libraries. |
| **Added:** wasmtime embedding, WIT host implementations, `celld:platform` WIT package | **+1,200 to +2,600** *(est.)* | Bottom-up, below. WIT bindings are generated by `bindgen!`, and WASI io/clocks/random/sockets/http come from the `wasmtime-wasi` and `wasmtime-wasi-http` crates, so the host writes only glue and policy. |
| **Added:** code-trust modes (§3.2.1): compile cache, signed artifact list and verification, deploy `--precompile/--sign-key` | **+700 to +1,200** *(est.)* | Split between the node (D9) and deploy (E1, as tools) |
| **Added:** eviction predicate and capability-policy decisions in `crates/logic` | **+200 to +400** *(est.)* | Pure functions, testable in the simulator |
| **Net for A+B+C** | **≈ −30.6k to −34.7k** | 65.2k → about **30.5k to 34.6k**. Removals of 38.8k to 39.8k minus additions of 5.1k to 8.2k. |

**Embedding breakdown** *(est.)*:

| Part | Lines |
|---|--:|
| Engine configuration: pooling allocator, epoch ticker, `ResourceLimiter` | 150 to 300 |
| Per-cell store lifecycle (instantiate, call export, drop), behind the runtime-manager seam | 300 to 600 |
| WASI wiring: `WasiView` / `WasiHttpView`, plus policy hooks (egress allowlist, durability gate in `send_request`, socket connect checks) | 200 to 500 |
| `celld:storage` host implementation over the existing `storage.rs` (SQL, KV, transactions) | 300 to 600 |
| `celld:alarm` | 50 to 100 |
| Inbound HTTP dispatch to `on-request` | 100 to 200 |
| Host-side accounting (resource-table charging, memory reporting to pressure) | 100 to 300 |
| **Total** | **1,200 to 2,600** |

For comparison, celld's V8 equivalents are large (`js.rs` alone is about 9.8k code lines), because V8 needs hand-written ops and value marshalling. The component model generates that layer.

**Tests and examples.**
- A+B+C has 2,638 test lines today, 171 of them ungated hooks. They are test seams in V8-path code, so expect to replace them, not carry them over.
- The examples (about 1,150 lines of JS, Python and Rust apps) need porting to components. Budget roughly the same size again for Rust or other component examples.

**Unchanged.** Area D (35,937 production lines) and most of area E (6,140 production + 4,104 tool lines), except for the additions above.

Most of the reduction comes from deleting the JS harness, the Node.js compatibility layer and the V8 op plumbing.

---

## 6. Risks and open questions

**Risks**
1. **Ecosystem.** The app does not get the Cloudflare and npm ecosystem; Cloudflare Workers projects don't run on it. This is the product question, not a technical one.
2. **JS performance.** JS guests without a JIT are much slower. If JS matters, the two-engine option (§3.9) is required.
3. **WASI churn.** WASI 0.3.0 is stable (ratified 2026-06-11; on by default in Wasmtime 46). Build on 0.3. Expect 0.3.x patch releases about every two months, and Component Model 1.0 still to come. The async runtime code is young: see CVE-2026-27195 (fixed in 42+).
4. **Spectre and multi-tenant hardening.** Mutually untrusted tenants need process separation (§3.2.2), which changes the node into a supervisor plus worker processes. Wasmtime itself is attack surface, though heavily fuzzed.
5. **Durable Object semantics.** Today's concurrent requests within one cell (input gate, C1) need a defined model in wasm. Options:
   - one call at a time per instance, which is simplest;
   - WASI 0.3 async exports.
6. **Host-side accounting completeness.** Every host import must charge its memory and work to the right store, or the limits leak.

**Open questions**
- What capability granularity do apps need? Per-interface grants only, or per-host and per-port allowlists from day one?
- Should Postgres be a raw-socket capability, a host-pooled capability, or both?
- Should the existing services (KV, Queues, D1, R2, Workflows) be kept as celld-native WIT interfaces, or dropped in favour of "your SQLite plus outbound HTTP"?
- What is the eviction idle timeout and pressure policy, when instantiation is microseconds rather than milliseconds?
- Should cross-cell calls be component-model typed interfaces (generated stubs) or untyped messages?

---

## 7. Build order

1. **Engine and world.** The wasmtime embedding (pooling allocator, epochs, `ResourceLimiter`), and the `cell` world with storage, alarms and HTTP. Use celld's engine-neutral seams (`cell_host.rs`, `engine_api.rs`, `host_channels.rs`) as the reference shape for the boundary to the engine (area D). First apps: counter, alarm, and an SSE stream (doc 02), as Rust components.
2. **Capabilities and code trust.** The capability manifest and deploy-time import checks. The code-trust modes in order: `compile` with its local artifact cache, then `signed` (signed artifact list, trusted keys), then the opt-in `unsigned` mode.
3. **Lifecycle.** The eviction predicate in the pure decision crate, driven in the simulator from the start.
4. **Measurement.** Density, cold start and tail latency, against celld's published V8 numbers as the reference (`docs/testing.md`).
5. **JS decision.** Whether JS guests are supported at all, and if so, how: StarlingMonkey in wasm, or a second V8 engine (§3.9).

## 8. Next steps

- [ ] Pin wasmtime and WASI versions, and confirm the API names used here: epoch callbacks, socket policy hooks, the `wasi:http` send hook.
- [ ] Draft the `celld:platform` WIT package (storage, alarm, cells/call, handler).
- [ ] Spike: one Rust component cell serving HTTP with SQLite storage, behind `cell_host.rs`.
- [ ] Benchmark instantiation time and per-cell RSS against a V8 isolate cell.
- [ ] Write up the Durable Object concurrency model decision (risk 5).
- [ ] Measure Cranelift compile time for representative components, to size the adoption window and to quantify what `signed`/`unsigned` saves (§3.2.1).
- [ ] Specify the signed artifact list's format and signing flow (local key versus KMS), and how it interacts with key rotation and wasmtime upgrades (§3.2.1).
- [ ] Define the guard that refuses `unsigned` mode when managed or multi-tenant configuration is active.
- [ ] Decide the tenancy model: single-tenant fleets, or process-per-tenant workers (§3.2.2).
