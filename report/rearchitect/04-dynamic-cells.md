# 04 — Dynamic cells (unifying Facets and Dynamic Workers)

**Status:** Explored (design sketch, nothing prototyped yet)
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Builds on:** [01 — Wasm-only runtime](01-wasm-only-runtime.md). It assumes every cell is a wasm component with its own SQLite, a WIT capability world, and per-store limits.

---

## 1. Premise

Today celld has two features for running code that the application supplies at runtime:

| Feature | What it does | Size (production) | Verdict |
|---|---|--:|---|
| **C11 Dynamic Workers** | Loads Worker code at runtime into a separate V8 isolate, with an explicit `env`, egress control and CPU/subrequest limits. Stateless; lives only in the loader's memory. | 1,231 | Beta |
| **C3 Durable Object Facets** | Gives runtime-loaded classes durable storage: child objects inside a Durable Object, each with its own SQLite file and replication stream, sharing the root's ownership. | 1,666 | Beta |

They are separate because a Durable Object namespace is fixed at deploy time. Runtime-loaded code therefore cannot get a namespace, so facets exist as a side door to storage, and Dynamic Workers stay stateless.

In the wasm-only design (01), **every cell already gets its own SQLite**, so that reason disappears.

**This exploration asks: can one primitive, the *dynamic cell*, replace both?** A dynamic cell is a cell whose code is supplied at runtime by a parent cell. It has its own SQLite, its own capability grants and limits, and a choice of ownership mode.

## 2. Goals and non-goals

**Goals**
- One concept instead of two subsystems, for code supplied at runtime (user uploads, AI-generated code, plugins).
- Durable storage for runtime-supplied code, with **both** ownership modes (§3.4):
  - `ownership: parent`: storage isolation, co-located with the parent, cheap;
  - `ownership: own`: a full cell that scales out.
- Close today's gaps:
  - facets cannot set alarms, and stubs have "not supported yet" paths (C3);
  - `delete()` is not durable (C3);
  - Dynamic Workers have GC-driven slot release and no memory admission (C11).
- Children are private to their parent by construction, and get only capabilities the parent holds.

**Non-goals**
- Cloudflare API compatibility. `ctx.facets` and `env.LOADER` are replaced, not emulated.
- Atomic commits across parent and child. Facets don't have them today either; §3.9 covers the patterns.
- Replacing ordinary relationships. Rows in the parent's own tables remain the default (§3.1).

---

## 3. Design

### 3.1 Decision rule: tables first, then a child cell

A child cell is not the default way to model related data. Apply this rule in order:

| If the related data or code… | Use | Why |
|---|---|---|
| is handled by the **same trusted code** and fits one cell's throughput and size | **Tables in the parent's SQLite** | Simplest and cheapest, and transactional with the parent. This is normal Durable Object modelling: one cell per unit of coordination. |
| comes from **different or untrusted code**, or needs its **own schema and migration lifecycle**, or **failure isolation**, but is **not independently hot or large** | **Dynamic cell, `ownership: parent`** | A hard storage and code wall, with no per-child bucket or ownership cost |
| is **independently hot**, **large**, or must **scale and fail over on its own** | **Dynamic cell, `ownership: own`** | The only option that scales out across nodes |

Why one SQLite database is not enough in rows 2 and 3:
- **No per-table permissions.** Guest code with a connection can read or corrupt every table. celld's SQL authorizer could be stretched to per-table rules, but that is row- and table-level security inside one database: complex and easy to get wrong.
- **Shared failures.** A critical SQLite failure (FULL, IOERR, NOMEM, INTERRUPT) poisons the whole cell (report C1).
- **One writer.** One cell has one serial SQLite writer on one node, capped at 64 concurrent fetches (A3).
- **Size costs.** Restore, handoff and per-activation bucket cost grow with database size. A database over about 80 MiB at the default budget hands off without a final snapshot (report D3).

**Decision recorded:** both ownership modes are required. `parent` covers isolation without scale; `own` covers scale.

### 3.2 The primitive

A parent cell creates and uses children through a host interface, `celld:cells/children`:

```wit
interface children {
  enum ownership { parent, own }
  enum storage   { sqlite, none }          // none = stateless (Dynamic Worker parity)

  record spec {
    code: code-ref,                        // from code.put (3.3)
    ownership: ownership,
    storage: storage,
    grants: list<capability>,              // a subset of the parent's grants (3.6)
    limits: limits,                        // memory, cpu-ms per call, storage bytes
  }

  create: func(name: string, spec: spec) -> result<child, error>; // idempotent by name
  get:    func(name: string) -> option<child>;
  stop:   func(name: string) -> result<_, error>;   // drop the instance, keep storage
  delete: func(name: string) -> result<_, error>;   // durable tombstone, then storage removal (3.8)
  list:   func(after: option<string>, limit: u32) -> list<child-info>;
}

resource child {
  fetch: func(req: incoming-request) -> result<response, error>;
  call:  func(method: string, args: list<u8>) -> result<list<u8>, error>; // or typed exports
  reports: func() -> stream<invocation-report>;    // generalized Tail Worker reports (3.10)
}
```

**Names are local to the parent.** `get("tenant-42")` resolves relative to the calling cell's own identity (§3.5). Nobody else can address the child unless the parent delegates a capability to them.

**Revised:** the `child` resource's `fetch` and `call` are superseded. `get(name)` returns the child's **internal URL**, and the parent calls it over plain REST like any cell (doc 09 §3.1). The host still enforces the child's grants and stamps the caller's identity. That makes an own-mode child a regular cell; only spawn, delete, grants and parent mode are new (§5.1).

### 3.3 Code: durable, content-addressed, always compiled on the node

Today a Dynamic Worker's code lives only in the loader's memory and dies with it. A dynamic cell may be activated on **any** node after eviction or failover, so its code must be durable.

- **`code.put(bytes) -> code-ref`.** The host validates the component, enforces a size limit (today's default is 64 MiB of module source per Dynamic Worker), and stores it content-addressed. The `code-ref` is its SHA-256.
- **Runtime-supplied code is always untrusted input.** It is always compiled on the node, under doc 01's `compile` mode, whatever the node's configured trust mode. Precompiled artifacts are **never** accepted from guests. The `signed` and `unsigned` modes (01 §3.2.1) apply only to code published through `celld deploy`.
- **Where code is stored** is open question 1:

  | Option | Key | Pro | Con |
  |---|---|---|---|
  | A | `code/<sha256>.wasm` | Deduplicated across parents | Needs reference counting or a mark-and-sweep across all parent registries |
  | B | `cells/<parent>/code/<sha256>.wasm` | Garbage-collected with the parent; simple | No deduplication across parents |

- **Ephemeral code** (`storage: none` only) may stay in memory, as Dynamic Workers do today, for single-call or short-lived stateless children. It is never persisted, and is dropped with its parent's activation.

### 3.4 Ownership modes

| | `ownership: parent` | `ownership: own` |
|---|---|---|
| Storage | Its own SQLite file and LTX stream, nested under the parent's prefix, e.g. `cells/<parent>/ltx/e<epoch>/children/<id>/`. This reuses the pattern in today's `facet_streams.rs`. | Its own prefix, `cells/<child-scope>/ltx/e<epoch>/`, like any cell |
| Ownership record and lease | **None.** It uses the parent's record and epoch, and is fenced by them. | **Its own** (D2) |
| Placement | Always on the parent's owner node | Any node. Placement, rebalance and failover are independent (D6). |
| Moves with the parent | Yes: eviction, reset and handoff take the parent and its parent-mode children as a group | No |
| Instance lifecycle | Activated lazily on first call; can be dropped while the parent stays resident; storage stays | Like any cell |
| Alarms | **Yes.** Stored in the child's SQLite, and woken through the parent (the parent's wake entry covers its children). Closes the facet gap. | Yes, natively (C2) |
| Durability gate | The child's writes and the parent's gate together (as facets today), or one gate covering the parent's and its children's streams | Its own gate |
| Bucket and coordination cost per child | Low: one SQLite file plus a nested stream | One ownership record, a lease and a prefix; cold activation costs an ownership CAS plus a restore |
| Depth | Bounded, e.g. 4 levels as facets today | Bounded by the policy in §3.6 |
| Good for | Untrusted plugins, per-user scripts, small isolated stores; many tiny children | Tenants, agents and apps with independent load; children larger than one cell should hold |

**Changing mode** (`parent` ↔ `own`) means copying the child's database to a new location under a quiesce. That is a deliberate operation, not a flag flip (open question 3).

**Stateless children** (`storage: none`) need no ownership mode at all. They run on the caller's node and are freed when their handle is dropped or idles. That is today's Dynamic Worker, with explicit release instead of GC-driven release.

### 3.5 Addressing and capabilities

- **Parent-relative names.** A child is addressed by `(parent-scope, name)`. The host resolves names against the *calling* cell's identity, which the guest cannot forge. Only the parent can reach its children, with no token needed.
- **Own-mode scope encoding.** A cell scope's character set is a **security fence** (`crates/logic/cell.rs`): it becomes a path component and an object-store key, and `/` is not admitted. An own-mode child's scope therefore must not be built by joining strings. Derive it, for example `Dyn:<sha256(parent-scope ‖ 0x00 ‖ name)>`, and keep the mapping in the parent's child registry (§3.8).
- **Calls and delegation (revised).** Calls go over **REST** like any cell-to-cell call (doc 09 §3.1). The host stamps the caller's identity on each request, and the child's app code decides whom to serve: by default only its parent, plus any cells the parent lists in the child's grants. No capability tokens are needed. Revocation means changing the child's grants or deleting it.

### 3.6 Attenuated grants, limits and quotas

- **Grants are a subset of the parent's.** `create` fails if `spec.grants` includes a capability the parent does not hold, such as outbound HTTP or a host allowlist entry. The rule is a pure function in `crates/logic`, so it is testable in the simulator.
- **Per-child limits**, using doc 01 §3.4–3.5: a memory cap through `ResourceLimiter`, a per-call CPU budget through epoch interruption, and a storage byte cap enforced by the host's `celld:storage` implementation.
- **Per-parent quotas:**
  - maximum children;
  - maximum total child storage;
  - maximum code bytes;
  - maximum concurrently active children;
  - maximum own-mode children (because each one costs bucket objects).
- **Memory-based admission.** Before activating a child, the host checks node memory headroom (D6). Today's Dynamic Workers have no memory-based admission (report C11).

### 3.7 Calls and concurrency

- **`child.fetch` and `child.call`:**
  - Parent mode: in-process, same node, no network hop.
  - Own mode: routed through D5 like any cross-cell call.
  - Both are gated by the caller's durability proof, as every egress is.
- **One call at a time per child instance, or WASI 0.3 async exports.** This follows the same decision as 01 risk 5, applied to children.

### 3.8 Lifecycle and garbage collection

- **The child registry** is a table in the parent's SQLite. Each row records the name, code ref, mode, grants, limits, own-mode scope, generation and state (`active`, `stopped` or `deleting`). The registry is the authority for which children exist.
- **Durable delete,** which fixes the facet delete gap (C3):
  1. Mark the row `deleting` and bump the generation. This revokes outstanding tokens.
  2. Stop the instance.
  3. Remove the storage: the nested stream (parent mode), or the cell prefix and ownership record (own mode).
  4. Remove the row.

  An alarm-driven sweeper retries step 3 in bounded batches (the `logic/sweep.rs` pattern) until it succeeds. No failure is silent, and nothing can come back on a later activation.
- **Cascade delete.** Deleting a parent puts all of its children into `deleting` first, recursively, and lets the sweeper drain them.
- **Orphan check for own-mode children.** An own-mode child's cell record names its parent. A periodic, bounded scan finds `Dyn:` cells whose parent registry no longer lists them, and deletes them.
- **Code GC:** per open question 1.

### 3.9 Consistency between parent and child

There is no atomic commit across a parent and a child in either mode, the same as facets today. Recommended patterns:
- **Idempotent child operations,** keyed by IDs the parent generates.
- **An outbox in the parent's SQLite.** The parent records an intent and its outcome, and an alarm retries until the child confirms.
- **The parent as the source of truth for membership** (the registry). The child is the source of truth for its own data.

### 3.10 Observability

- **Generalized invocation reports** replace Tail Workers. A parent can subscribe to its children's invocation reports: request metadata, outcome, logs, exceptions, CPU and memory used. Reports from all children feed the node's telemetry (E5).
- **Report delivery is best effort,** as today. If a parent needs durable reports, they can go into the parent's event log (doc 02 §3.2).

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| D1 core and lifecycle | Parent-mode children ride the parent's lifecycle. Own-mode children are ordinary cells. The eviction predicate (01 §3.10) counts a parent's active parent-mode children. |
| D2 ownership | No change for parent mode, which uses the parent's epoch. Own mode adds `Dyn:` scopes with derived names. |
| D3 replication | Nested LTX streams for parent mode, reusing today's facet-stream pattern. The output gate covers the parent's and its children's streams. |
| D5 routing | Own-mode calls route like any cell. Delegated capability tokens are verified by the host before dispatch. |
| D6 placement and pressure | Memory-based admission for children; per-parent quotas. Own-mode children rebalance independently. |
| D9 deployment | Durable code store for runtime-supplied components (§3.3). Deployed code keeps the 01 trust modes; runtime code is always compiled on the node. |
| C2 alarms | Alarms for parent-mode children, woken through the parent. |
| E3 / E4 operations | `celld cell list` shows `Dyn:` cells. A `celld children` operator view lists a parent's registry, and `/state` shows child counts and quota use. |
| E5 telemetry | Per-child CPU, memory and storage, plus invocation reports. |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

Code lines, measured against the baseline report where possible; estimates are marked *(est.)*. **Overlap with doc 01:** doc 01 already counts the JS halves of C3 and C11 (486 lines) and their V8 glue as removed. Measured against a post-01 codebase, this doc's effect is mostly the "added" rows.

| | Production | Tools | Test | Examples |
|---|--:|--:|--:|--:|
| **Removed: C3 Facets** | −1,666 (1,502 Rust, 164 JS) | – | −9 | −50 |
| **Removed: C11 Dynamic Workers and Tail Workers** | −1,231 (909 Rust, 322 JS) | −32 | −7 | −44 |
| **Removed subtotal** | **−2,897** | **−32** | **−16** | **−94** |
| Added: spawn and delete host calls, the child cell record (code ref, grants, parent, mode), cascade sweep through the parent field | +150 to +300 *(est.)* | | | |
| Added: durable code store (runtime wasm by hash, loaded through the normal component path, always compiled on the node) | +100 to +200 *(est.)* | | | |
| Added: grant attenuation (subset check), quotas, memory admission | +100 to +250 *(est.)* | | | |
| Added: stateless mode (no storage, no ownership record) | +50 to +150 *(est.)* | | | |
| Added: parent mode (nested SQLite file and stream, combined gate, alarms through the parent; reuses the facet-stream pattern) | +400 to +800 *(est.)* | | | |
| Not needed: addressing and calls (an own-mode child is a regular cell, reached over REST with caller identity, doc 09 §3.1); delegation tokens | 0 | | | |
| Added: operator CLI (`celld children`) | – | +100 to +300 *(est.)* | | |
| Added: tests and examples | | | +400 to +800 *(est.)* | +80 to +150 *(est.)* |
| **Added subtotal** | **+800 to +1,700** *(est.)* | **+100 to +300** | **+400 to +800** | **+80 to +150** |
| **Net** | **≈ −1.2k to −2.1k** | ≈ +0.07k to +0.27k | ≈ +0.38k to +0.78k | ≈ 0 to +0.06k |

**Revised 2026-10-14.** An own-mode child is just a **regular cell spawned by another cell with reduced grants**. Calls to it are plain REST with host-stamped caller identity (doc 09 §3.1), so the earlier parent-relative addressing, `child.fetch`/`call` handles and delegation tokens are not needed. The only substantial new mechanism is **parent mode** (shared ownership and nested storage). The earlier estimate was +1.5k to +3.1k.

### 5.2 Reusable pieces in today's code

| Today | Reused for |
|---|---|
| `facet_streams.rs` (nested replication streams under the root) | Parent-mode child storage |
| Worker Loader admission (`js.rs:7346-7380`: one registry lock for check plus insert) | The child admission pattern |
| `logic/sweep.rs` (bounded reclamation per cell turn) | Durable-delete and orphan sweepers |
| `logic/cell.rs` scope fence | Own-mode `Dyn:` scope derivation |
| `peer_auth.rs` HMAC | Delegated capability tokens (§3.5) |

---

## 6. Risks and open questions

**Risks**
1. **Own-mode child explosion.** Millions of own-mode children mean millions of ownership records and prefixes, slower listings, and more bucket cost. Mitigate with per-parent quotas, making `parent` the default for small children, and the bounded listing semantics `celld cell list` already uses.
2. **Garbage-collection correctness.** Cascade delete, orphan scan and code GC must never delete live data. Use a tombstone-first ordering and simulator coverage, the way the alarm-wake reclamation is proved today (C2).
3. **Hot parents in parent mode.** All of a parent's parent-mode children share its node and lease. A parent with many busy children becomes a hotspot. Detect it, and recommend `own` mode.
4. **Capability leaks.** A delegated token outliving its intent. Use short expiry, generation-based revocation, and audit logging.

**Open questions**
1. Where runtime code is stored: global `code/<sha256>` with deduplication, or per-parent with simple GC (§3.3)?
2. The default ownership mode for `create` (`parent` is suggested, for cost).
3. Mode migration: support `parent` → `own` promotion (copy under quiesce), or make the mode immutable?
4. Depth limits for nesting in each mode.
5. Does an own-mode child keep a hard link to its parent (cascade delete), or can it be detached and re-parented?
6. Quota defaults, and whether quotas are per parent, per deployment or per tenant.

---

## 7. Build order

After doc 01's engine and cell world exist:
1. **Stateless children** (`storage: none`): what Dynamic Workers do in celld, with explicit release, memory admission and invocation reports.
2. **Own-mode children:** durable code store, child registry, `Dyn:` scopes, durable delete. This mostly reuses ordinary cells.
3. **Parent-mode children:** nested storage and streams, the combined gate, child alarms through the parent.
4. **Delegation tokens and quotas.**

celld's C3 Facets and C11 Dynamic Workers are not built; dynamic cells replace both.

## 8. Next steps

- [ ] Draft the `celld:cells/children` WIT interface and the child world (default grants, limits).
- [ ] Decide open questions 1 and 2: code storage and default mode.
- [ ] Specify the child registry schema and the tombstone-first delete protocol. Model it in the simulator.
- [ ] Spike: own-mode child create → call → delete on wasmtime, reusing ordinary cell activation.
- [ ] Measure the per-child cost of parent mode versus own mode (activation latency, bucket objects, RSS).
