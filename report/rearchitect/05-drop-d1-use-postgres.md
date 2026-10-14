# 05 — Drop D1 (C6); use Postgres for shared relational data

**Status:** Explored (design sketch)
**Context:** a green-field app inspired by celld (see the [README](README.md)). D1 is simply never built; nothing is migrated.
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Related:** [01 — Wasm-only runtime](01-wasm-only-runtime.md) (§3.6, the Postgres host capability); feature B8 TCP sockets in the baseline report

---

## 1. Premise

D1 (feature C6) is Cloudflare's serverless SQLite database service. In celld a D1 database is **one cell of a reserved Durable Object class** that runs SQL sent over a binding (`env.DB.prepare(...).all()`, `batch()`, `exec()`, migrations). It is a single writer, holds one SQLite file, and is replicated like any cell.

Outside the Cloudflare environment, the standard tool for **shared, app-wide relational data** is **Postgres**, available managed (RDS, Cloud SQL, Neon, Supabase, …) or self-hosted. Compared with D1 in celld, Postgres offers:
- concurrent writers (MVCC);
- a mature SQL dialect, extensions and ecosystem;
- operational tooling: backups, point-in-time recovery, read replicas;
- no one-writer ceiling.

That covers the cases D1 is for, and celld's D1 has gaps of its own: no replicas, no Time Travel, no REST API, no `dump()`, results capped at 100k rows or 32 MiB, and no query time bound (report §7, C6).

Per-entity state stays in **cells** (Durable Objects, each with its own SQLite). That is celld's core strength and unaffected by this change.

**This exploration asks: should the new app skip D1 entirely and put shared relational workloads in Postgres?**

## 2. Goals and non-goals

**Goals**
- Build no D1 subsystem: no binding, reserved class, `__d1_run`, D1 executor (celld's lives in `storage.rs`), `d1` CLI, or deploy-config support.
- Give applications a supported, efficient path to Postgres.

**Non-goals**
- Running Postgres inside the platform, or making the platform a Postgres host.
- Changing per-cell SQLite (Durable Object storage). That stays.
- D1 compatibility, migration or export tooling. There are no D1 users or data.

---

## 3. Design

### 3.1 The role split after the change

| Data | Where it lives | Why |
|---|---|---|
| Per-entity state (a room, user, document, agent, tenant shard) | **Cell SQLite** (C1) | Co-located with its code, synchronous, scales out by having many cells, RPO=0 through celld's output gate |
| Shared, app-wide relational data (accounts, catalogs, reporting, cross-entity queries) | **Postgres**, external | Concurrent writers, mature tooling, no one-writer ceiling |
| Blobs | R2 (C7) / fleet bucket | Unchanged |
| Simple key-value | KV (C4) or cell SQLite | Unchanged |

### 3.2 How applications reach Postgres: three options

**Option A: drivers over TCP sockets (for a JS/V8 runtime like celld's; no new subsystem).**
- celld already supports outbound TCP with `connect()` and `startTls()` (B8). Postgres drivers that support Workers sockets (for example `postgres.js`, or `pg` with its Workers socket adapter) can connect directly. Postgres's TLS negotiation (send `SSLRequest`, read `S`, then upgrade) maps onto `startTls()`.
- **Limitations:**
  - **Sockets cannot outlive their event** (B8). Each request opens a new Postgres connection, paying TCP plus TLS plus authentication (several round trips) on every request, which can cause connection storms. Mitigate with an **external pooler** (PgBouncer, or the provider's own pooler) close to Postgres.
  - **B8 hardening gaps** from the report: no connect timeout, no-op `cancel()`/`abort()`, and a `startTls()` hang if a read is in flight when it is called. Fix these (§5) before recommending this path for production.

**Option B: a pooled Postgres binding in celld, in the style of Cloudflare's Hyperdrive.**
- A node-local connection pool, managed by the host, keeps warm, authenticated TLS connections to Postgres. Workers get a binding (for example `env.PG.connectionString`) that their driver connects through, and the pool multiplexes requests onto warm connections.
- **Pro:** no per-request connection setup and no storms. This also matches Cloudflare's Hyperdrive API, which celld lists as **No** today (`docs/cloudflare-compat.md`).
- **Con:** a transaction pooler must understand enough of the Postgres wire protocol: startup and authentication passthrough or SCRAM, session reset, and transaction boundaries. That is a sizeable new subsystem (§5).

**Option C: a `celld:postgres` host capability (wasm-only design, doc 01 §3.6).**
- The host keeps a pool, using for example `tokio-postgres` with a pooling layer. Guests call `query` / `transaction` through a typed interface, with credentials held by the host, a per-cell connection quota, and the egress policy applied.
- This avoids wire-protocol pooling, because the host *is* the client.

**Recommendation** (follows the runtime choice in doc 01)
- **Wasm runtime:** Option C.
- **JS/V8 runtime:** Option A, with an external pooler and sockets built without celld's B8 gaps.
- **Option B** only if a pooled, Hyperdrive-style binding is wanted on a JS/V8 runtime.

### 3.3 Durability and consistency semantics change

- **D1 writes today get celld's guarantee.** No response leaves until the write is durable in the fleet (RPO=0, D3).
- **Postgres writes are governed by Postgres.** A commit acknowledgement is as durable as that server's configuration makes it: `synchronous_commit`, synchronous replicas, provider settings. celld's output gate does not cover them, and doesn't need to: Postgres is the source of truth for its own data.
- **Dual writes** (a cell writes its SQLite *and* Postgres in one operation) are not atomic. Use an **outbox**: write the intent to the cell's SQLite (which the gate covers), then apply it to Postgres from an alarm-driven retry with an idempotency key. Or keep each piece of data in exactly one store.
- **Postgres becomes an availability dependency** for the features that use it. An outage now affects those requests, whereas D1 shared celld's own failover.

### 3.4 Operational changes

- **New dependency:** a Postgres service, with its HA, backups, credentials and network path. This breaks the "celld needs only a bucket" property for apps that use it, though not for celld itself.
- **Credentials:** Option A keeps the connection string in a Worker `vars` entry today. celld has no secrets support (report E1), so plan for a secrets mechanism or host-held credentials (Options B and C).
- **`celld dev`:** apps that use Postgres need a local Postgres, for example a container. Document it; optionally have `celld dev` start one, which is an open question.
- **Networking:** egress from nodes to Postgres, ideally on the private network. TLS, with certificate verification against the system roots.

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| C6 D1 | Not built: the binding, the reserved class, `__d1_run`, the D1 executor and transaction control (celld's `storage.rs`), `d1_cli.rs`, and the D1 deploy config |
| C1 Durable Objects | Unchanged. The SQL authorizer has only its DO rules; no D1-only branches. |
| B8 TCP sockets | Hardening becomes a priority (connect timeout, working cancel/abort, the `startTls` race), because Postgres traffic uses it |
| E3 operator CLI | `operator_cell.rs` and the `/runtime/` operator route are not needed either. Their other users, the KV and Queue CLIs, are not built (doc 07); counted there. |
| E1 deploy | No `d1_databases` key; the config allowlist simply doesn't know it |
| D9 deployment | No reserved D1 class constant or manifest handling |
| Docs | A "Postgres from the app" guide instead of a D1 service page |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

Code lines, measured against the baseline report's line map; estimates are marked *(est.)*.

| | Production | Tools | Test | Examples | Docs (non-blank) |
|---|--:|--:|--:|--:|--:|
| **Not built: C6 D1** | **−1,126** | **−598** | **−35** (7 ungated) | **−49** | **−72** (`docs/services/d1.md`), plus about 16 D1 mentions in other docs |
| Not built: D1 references inside other features (deploy config, CLI dispatch, protocol, reserved-class constants) | −20 to −30 *(est.)* | (included) | – | – | – |
| **Not-built subtotal** | **≈ −1,150** | **≈ −600** | **−35** | **−49** | **≈ −85** |
| Added (Option A): B8 hardening (connect timeout, cancel/abort, `startTls` race) | +50 to +150 *(est.)* | – | +100 to +200 *(est.)* | – | – |
| Added (Option A): Postgres guide and example | – | – | – | +40 to +60 *(est.)* | +60 to +100 *(est.)* |
| **Net with Option A** | **≈ −1.0k to −1.1k** | **≈ −0.6k** | ≈ +0.1k to +0.2k | ≈ 0 | ≈ 0 |
| Alternative: Option B (pooled, Hyperdrive-style binding) instead | **+1,500 to +3,000** *(est.)* more | +0 to +100 | +800 to +1,500 *(est.)* | +40 to +60 | +80 to +150 |
| Alternative: Option C (wasm `celld:postgres` capability) instead | **+500 to +1,200** *(est.)* more | – | +300 to +600 *(est.)* | +40 to +60 | +60 to +100 |

**Where these lines are in celld** (what the new app doesn't build).
- **Production (1,126):**
  - `storage.rs` 827: the D1 executor, transaction control, fault hooks, meta snapshot;
  - `js/harness.js` 261: `D1Database`, `D1PreparedStatement`, sessions, the reserved D1 cell class;
  - `fleet.rs` 12, `js/storage_ops.rs` 10, `js/bootstrap.rs` 6, `engine_api.rs` 5, `deploy.rs` 4, `js.rs` 1.
- **Tools (598):** `d1_cli.rs` 445, `deploy.rs` 152 (D1 config and migrations), `main.rs` 1.

**Still needed.** The DO SQL authorizer and the raw-FFI SQLite cursor code used by DO SQL. `operator_cell.rs` and the `/runtime/` route are also unnecessary once KV and Queues are skipped; doc 07 §5.1 counts them.

### 5.2 Effect on doc 01

Doc 01 §5.2 listed KV/D1/R2 cell logic as "rewritten or dropped". With this change, D1 is **dropped**, so its share of the JS cell logic (261 lines) never needs a Rust rewrite. Doc 01's estimate shrinks accordingly; Option C is the wasm-era replacement.

---

## 6. Risks and open questions

**Risks**
1. **Per-request connections with Option A.** Without an external pooler, a traffic burst becomes a connection storm on Postgres. The docs must make the pooler a requirement, not an option.
2. **Weaker durability coupling.** Writes leave celld's RPO=0 envelope. Apps that write both cell SQLite and Postgres need the outbox pattern.
3. **The TCP-socket gaps celld has (B8)** must not be repeated. They become production-critical once Postgres traffic depends on sockets.

**Open questions**
- Should `celld dev` start a local Postgres for apps that declare one, or leave it to the developer?
- Is a pooled, Hyperdrive-style binding (Option B) worth its cost, if the app keeps a JS/V8 runtime?
- What secrets mechanism holds Postgres credentials (today only plain `vars` exist)?

## 7. Build order

1. Leave D1 out of the platform design: no binding, reserved class, executor, CLI or config key.
2. Pick the Postgres access path for the app's runtime:
   - **wasm (doc 01):** build the `celld:postgres` host capability with host-side pooling (Option C);
   - **a JS/V8 runtime:** harden TCP sockets (connect timeout, working cancel/abort, no `startTls` read race), and require an external pooler (Option A).
3. Write the Postgres guide and example: the pooler requirement, the outbox pattern, TLS settings, credentials.

## 8. Next steps

- [ ] Decide the access path (Option A or C) together with the runtime decision of doc 01.
- [ ] If Option A: confirm which Postgres drivers work over the app's socket API, including TLS upgrade, and design the sockets without celld's B8 gaps. Add an `examples/postgres`.
- [ ] Decide on a secrets mechanism for database credentials.
- [ ] Write the "Postgres from the app" guide: the pooler requirement, the outbox pattern, the TLS settings.
