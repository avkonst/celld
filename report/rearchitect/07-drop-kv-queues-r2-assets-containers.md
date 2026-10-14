# 07 — Drop KV, Queues, R2, Static assets and Containers (C4, C5, C7, C10, C12)

**Status:** Explored. The decision not to build these is recorded; the replacement guidance is a sketch.
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated: these features are simply never built.
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Related:**
- [05 — Drop D1](05-drop-d1-use-postgres.md): same approach. With D1 also gone, the shared operator CLI plumbing is not needed either (§5.2);
- [06 — Cron as a library](06-cron-as-library.md): the pattern for moving service logic into app libraries;
- [04 — Dynamic cells](04-dynamic-cells.md) and [01 — Wasm-only runtime](01-wasm-only-runtime.md).

---

## 1. Premise

**Decision recorded:** the new app does not build five of celld's Cloudflare service bindings:

| ID | Service | What it does in celld today | Verdict (report) |
|---|---|---|---|
| C4 | Workers KV | A key-value namespace as one cell; values over 1 MiB go to the bucket under `kv/` | Solid |
| C5 | Queues | A queue broker as one cell (leases, retries, dead-letter queue); consumer dispatch | Solid |
| C7 | R2 | Object storage in the fleet bucket under `r2/`; node-local multipart uploads | Solid |
| C10 | Static assets | Serves deployed files with `_headers` / `_redirects` and a disk cache | Solid |
| C12 | Containers & Sandbox | A Docker/Podman container per Durable Object, network fencing, image tars in the bucket | Experimental |

**Why.**
- With Cloudflare compatibility no longer a goal (01), these services re-implement things that mature external systems already do better: Redis or Postgres, message brokers, S3-compatible storage, CDNs, and container orchestrators.
- Each one adds platform surface, configuration, CLIs, data formats in the bucket, and failure modes that someone must maintain.
- celld's distinctive value is the **cell**: a named, single-writer object with its own SQLite, alarms and RPO=0 durability, plus the engine underneath. That stays.

**What remains after this doc and doc 05.** C1 Durable Objects, C2 Alarms, C8 Workflows, and C3 Facets / C11 Dynamic Workers, which become dynamic cells in doc 04. C9 Cron becomes an app library (doc 06).

## 2. Goals and non-goals

**Goals**
- Build none of the five subsystems: no bindings, reserved classes, host operations, CLIs, deploy configuration, or bucket formats.
- Give each use case a replacement (external service or app library).
- Keep everything the remaining features need, even where celld implements it inside one of these features (§5.2).

**Non-goals**
- Building replacements into the platform. Replacements are external services, or app-level libraries on top of cells (§3.6).
- Migration, deprecation or export tooling. There is nothing to migrate.
- Changing cells, alarms, Workflows, or the engine (area D).

---

## 3. Replacements, per service

### 3.1 C4 Workers KV

**Replace with:**
- **Cell SQLite**, for KV-shaped data that belongs to one entity. A table in the entity's own cell is simpler and transactional.
- **Postgres** (doc 05) or **Redis/Valkey**, for shared app-wide KV, caches or sessions.
- **An optional app library** (`celld-kv`): a namespace as one cell with a `kv` table, which is exactly what celld KV does internally today. It could reuse `crates/logic/kv.rs`, which is pure logic.

**Lost:** the strongly consistent namespace binding with expiration and metadata, unless the library provides it.

### 3.2 C5 Queues

**Replace with:**
- **An external broker:** NATS JetStream, RabbitMQ, SQS or Kafka. Cells publish over HTTP or TCP, and a consumer process or Worker reads.
- **Or an app-level `celld-queue` library**, following doc 06's pattern. `crates/logic/queue.rs` (246 production lines) is already pure: leases, generations, settlement fencing, retry timing. The broker logic in `harness.js` (about 1,470 lines of JS) would need porting to the library's language. That is a significant job, so plan it only if in-cell queues are wanted.
- **For simple fan-out within a cell:** a pending-work table plus an alarm.

**Lost:** at-least-once delivery inside celld, with leases, retries and dead-letter queues, and with no external dependency.

### 3.3 C7 R2

**Replace with:**
- **Apps use S3-compatible object storage directly**, with their own bucket and credentials, over HTTP (an S3 client or SigV4 over `fetch` / `wasi:http`).
- **Optional, wasm design:** a host capability `celld:blob` backed by a configured object store, with credentials held by the host. Treat it as a separate future doc, not part of this one.

**Lost:**
- a binding that reuses the fleet's credentials;
- R2 conditional operations (`onlyIf`);
- the R2 API shape.

Presigned URLs, which celld never supported, come for free with a real S3 provider.

### 3.4 C10 Static assets

**Replace with:**
- **A CDN or static host in front of celld:** S3 plus CloudFront, Cloudflare Pages, nginx or Caddy. The load balancer routes static paths there, and dynamic paths to celld.
- `_headers` and `_redirects` rules move into CDN or proxy configuration.
- **Small sites** can embed files in the app bundle and serve them from a cell or Worker handler.

**Lost:**
- the single-artifact deploy of Worker and site together, with version-skew protection between them during rollouts;
- `env.ASSETS.fetch()`;
- `run_worker_first` routing inside celld.

### 3.5 C12 Containers & Sandbox

**Replace with:**
- **Container infrastructure outside celld:** Kubernetes, Nomad, ECS or Fly. Cells call container services over HTTP or TCP (`fetch`, `cloudflare:sockets`, or `wasi:http` / `wasi:sockets` in doc 01).
- **For per-object sandboxes:** an external sandbox service (Firecracker-based or similar), or Kubernetes jobs created per request, addressed by a cell that records the sandbox ID in its SQLite.

**Lost:**
- the one-container-per-Durable-Object lifecycle tied to cell ownership (start, `sleepAfter`, stop on move);
- `@cloudflare/containers` and `@cloudflare/sandbox` compatibility;
- the "kernel boundary" option that the Dynamic Workers docs point to. Strong isolation of untrusted code then relies on wasm and process separation (01 §3.2.2).

### 3.6 Common pattern: what moves to libraries rather than outside services

This follows the precedent of doc 06 (cron). Where celld's implementation is already pure logic on top of a cell, it can live on as an **optional app library** instead of a platform feature:
- `crates/logic/kv.rs` → `celld-kv`;
- `crates/logic/queue.rs`, plus a port of the harness broker → `celld-queue`.

Neither library is required by this doc. They are listed so the logic isn't simply lost. Each would be its own follow-up doc with its own LOC table.

---

## 4. Integration with the existing engine

Relative to celld's architecture, the new app **omits** the following. Each row says what in celld it does not build.

| Area | Change |
|---|---|
| A1–A4 runtime | Remove the KV/Queue/R2/assets/container bindings from `env` construction (`bootstrap.rs`), the host operations in `js.rs`, and the harness sections |
| A3 pool | Remove queue-producer admission (`pool.rs`) and queue consumer dispatch (`WorkerJob::Queue`) |
| D1 core | Remove the reserved classes `__KvNamespace` and `__Queue` from the generation registry |
| D3 replication | Remove the R2 egress channel from the output gate (`Channel::R2`) |
| D4 fleet log | Remove queue-specific log batching (`queue_batching.rs`). **Keep** the `bucket.rs` blob helpers it uses (`list_page`, `get_blob`, `BlobRead`, `BlobRange`, `BlobConditions`), even though they were written for R2 (§5.2). |
| D5 ingress | Remove the asset resolver path before the Worker (`main.rs` asset dispatch) |
| D6 placement | Remove container memory reservations from pressure accounting, and the `fleet_class_instances` capacity counting (`ownership_store.rs`) |
| D7 object store | Remove the R2 blob types and key encoding from `bucket.rs` (except the helpers above) |
| D9 deployment | No asset index, image tars, queue attachments or queue catalog in the manifest and protocol types |
| E1 deploy | No asset upload, image build or save (no `docker` CLI needed), queue-attachment CAS, or KV/R2/queue config. The config allowlist simply doesn't know these keys. |
| E6 managed | No asset transfer in the managed deploy agent (`control_plane.rs`) |
| E3 operator CLI | No `operator_cell.rs` and no authenticated `/runtime/` operator route. Their only users are the D1, KV and Queue CLIs, none of which exist (§5.2). |
| Node requirements | No Docker/Podman socket, no nftables or `NET_ADMIN`, and no asset disk cache (`CELLD_ASSET_CACHE_*`) |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

Code lines, measured against the baseline report's line map; estimates are marked *(est.)*. Test counts include ungated test hooks.

| Not built (relative to celld) | Production | Tools | Test | Examples | Docs (non-blank) |
|---|--:|--:|--:|--:|--:|
| C4 KV | −1,165 | −748 | −40 | −29 | −48 |
| C5 Queues | −3,143 | −359 | −269 | −44 | −59 |
| C7 R2 | −1,918 | −525 | −7 | −36 | −73 |
| C10 Static assets | −1,685 | −398 | −4 | −19 | −258 |
| C12 Containers & Sandbox | −2,098 | −244 | – | −154 | −186 |
| **Measured subtotal** | **−10,009** | **−2,274** | **−320** | **−282** | **−624** |
| Kept: `bucket.rs` blob helpers used by the fleet log (part of C7's 411 `bucket.rs` lines) | +80 to +150 *(est.)* | – | – | – | – |
| Not built: references inside other features (deploy, protocol, D6 accounting, D9 manifest, `main.rs` dispatch) | −150 to −400 *(est.)* | (included) | – | – | – |
| Not built: `operator_cell.rs` and the `/runtime/` operator route (only the D1, KV and Queue CLIs use them; D1 is gone too, doc 05) | −59 | −190 | – | – | – |
| Added: replacement guides and examples (Postgres or Redis for KV, a broker, S3, CDN, external containers) | – | – | – | +100 to +200 *(est.)* | +150 to +300 *(est.)* |
| **Net** | **≈ −10.1k to −10.4k** | **≈ −2.5k** | **≈ −0.3k** | **≈ −0.1k to −0.2k** | **≈ −0.3k to −0.5k** |

**Scale.** That is about **9.4%–9.7% of all production code** (107,304). The area C share alone is about 41% to 42% (24,439).

### 5.2 Shared code: what can and cannot go

| Code | Owner today | Fate |
|---|---|---|
| `operator_cell.rs` (190 tool lines) and the `/runtime/` operator route in `main.rs:2923-3004` (59 production lines) | E3, used **only** by the D1, KV and Queue CLIs | **Not built.** None of those CLIs exist in the new app (docs 05 and 07); counted in §5.1. If future operator commands need an authenticated route into cells, design it then (open question). |
| `bucket.rs` blob helpers (`list_page`, `get_blob`, blob types) | C7 | **Keep.** The fleet log (`node_log.rs:6296`, `6849-6861`) uses them. Move them under D7. |
| `crates/logic/sweep.rs` (bounded reclamation) | Shared by KV and Queues | Trivial size. Keep it as a general bound for Workflows and future libraries, or delete it. |
| `md-5` / `sha1` / `sha2` crates (R2 checksums) | Also used by B4 crypto | **Keep.** No dependency is removed by R2 alone. |
| `object_store` multipart (R2) | Also used by `celld-ltx` | **Keep** |
| `glob` (assets) | Also used by `celld dev` | **Keep** |

**Dependency impact:** no Rust crates are removed. The **node requirements** shrink: no container engine, nftables or `NET_ADMIN`. The **deploy requirements** shrink too: no `docker` CLI.

### 5.3 Combined with the other docs

These docs overlap, so their numbers don't simply add up. For example, doc 01 already counts the JS halves of these services as removed. Measured against the **baseline**, area C after docs 05 and 07:
- **removed:** C4, C5, C6, C7, C10, C12, which is 11,135 production lines;
- **remaining:** C1, C2, C3, C8, C9 and C11, which is 13,304 production lines. Of those, C3 and C11 change shape under doc 04, and C9 under doc 06.

Area C therefore drops from 24,439 to about **13.3k** production lines, **−45.6%**, before docs 04 and 06.

---

## 6. Risks and open questions

**Risks**
1. **More external dependencies for apps:** a broker, S3, a CDN, a container platform. The "a bucket is all you need" story no longer covers those use cases.
2. **Static-asset version skew.** Without the same deploy carrying Worker and assets, the app must handle skew between the CDN and the code during rollouts.

**Open questions**
- Do the optional `celld-kv` / `celld-queue` libraries (§3.6) ship, and when?
- Should a `celld:blob` host capability exist in the wasm design (separate doc)?
- Will future operator commands need an authenticated route into cells (the role `/runtime/` plays in celld)?

## 7. Build order

Nothing is built for these five features. The work is:
1. Keep the fleet log's blob helpers (`list_page`, `get_blob`, blob types) in the object-store layer (D7) from the start, not inside an R2 module.
2. Leave the reserved classes, bindings, host operations, protocol types, deploy keys and CLIs out of the platform design (§4).
3. Write the replacement guides and examples (§3.1–3.5) when the first app needs each capability.
4. Optionally build the `celld-kv` / `celld-queue` libraries (§3.6) as separate docs.

## 8. Next steps

- [ ] Write the five replacement guides (§3.1–3.5) and add matching examples.
- [ ] Define the object-store blob helpers the fleet log needs as part of D7.
- [ ] Decide on the optional libraries (§3.6), and whether an authenticated operator route into cells is needed (§5.2).
