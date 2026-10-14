# 14 — Cell lifecycle, placement and pressure, object store (D1, D6, D7)

**Status:** Decided, except the open questions in §5.
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). Baseline: celld v0.6.2 (`90b4301`).
**Builds on:** [12 §9 — V9](12-durability-and-single-writer-options.md) (claims, restore, cleanup, rebalancing mechanism) and [13 — routing C1](13-routing-ingress-and-peer-transport-options.md) (forwarding, retry rule).

---

## 1. Scope

| Feature | celld production lines | celld readiness |
|---|--:|---|
| D1 Decision core & cell lifecycle | 4,074 | Solid |
| D6 Placement, pressure & drain | 4,060 | Beta |
| D7 Object-store backends | 1,161 | Solid |

## 2. D1: cell lifecycle

| Topic | Decision |
|---|---|
| **States** | Three levels of presence: **Running** (live store), **Warm** (store dropped; claim and file kept on disk), **Absent** (no claim here). About 12 states instead of celld's 20; node leases and fleet-log recovery are gone with V9. |
| **Decision core** | One serial pure core (as in celld). It never does I/O or waits, and requests to running cells bypass it. Shard by cell hash only if measurement shows one core saturating. |
| **Invariants** | Checked in release builds. A per-cell failure fences and quarantines that cell; only a node-wide failure aborts the process. |
| **Crash handling** | An app crash is a wasm trap, so the process survives. Then: drop the instance; roll back any open transaction; return `500` to the request that crashed; transparently re-run once any other in-flight request that hadn't committed or sent anything; return `500` (not replay-safe) to the rest. SSE clients resume with `Last-Event-ID`; a crashed alarm is retried with backoff. |
| **Crash ladder** | Per cell, in a 10 min window: 1–2 crashes → restart immediately; 3–4 → back off; 5+ → **quarantine** (`503`, alarms paused). It ends on a new code version, an operator command, or 1 h probation. The count is stored in the claim, so it moves with the cell. |
| **Crash reports** | Log + metric per class, version and trap kind; optional reporter cell; optional app hook `on-crash-recovery`. |
| **Activation limits** | Pluggable policies chosen in configuration. Default `fixed`: 256 concurrent warm and 32 concurrent cold activations, at most 1 GiB downloading; priority requests > alarms > rebalancing; a request waits at most 10 s, then gets `503`. `unbounded` is available; `adaptive` comes later. **No slow-start mode:** under V9 a new node gets only unclaimed cells and the ones rebalancing hands it. |

## 3. D6: placement, pressure, shutdown

| Topic | Decision |
|---|---|
| **Pressure signal** | Exact per-cell memory (wasm `ResourceLimiter`) plus the process's own cgroup; disk pressure is handled by V9 cleanup. |
| **Pressure ladder** | Soft (80%): evict idle instances (Running → Warm), stop claiming new cells, accept no incoming moves. Hard (95%): `503` for requests that need an activation; release Warm cells to peers. |
| **First placement** | `claim-local` first: the node that sees the first request claims the cell. Then `pressure-aware`: a node under pressure forwards the first request to the least loaded peer, which claims it (one hop, no re-forward). |
| **Load information** | A response header on peer traffic, plus a load summary in the node record every 30–60 s (≈ $0.43 per node per month). |
| **Rebalancing policy** | Node load = its highest share of memory, disk and request rate; cell count only breaks ties; CPU later. Move only when nodes differ by more than 15 points; at most 30 moves per minute per node; 10 min cool-down per cell. Prefer Warm cells and cells without SSE streams. |
| **Shutdown** | `preStop` delay → `GOAWAY` → refuse forwarded requests with `503` before any handler runs → finish in-flight requests. The ingress nodes hold requests for the restarting owner and retry with backoff for up to 30 s (bodies buffered up to 1 MiB), then return `503` with `Retry-After`. Modes: **`in-place`** (cells wait for the pod to return with its disk), **`hot`** (hand off the busy cells one by one: hold, finish, snapshot, release, `421`), **`all`** (for scale-down). |
| **Drain token** | Not built. A StatefulSet rolling update and a `PodDisruptionBudget` (`maxUnavailable: 1`) serialize restarts. |
| **Readiness** | Ready once disk reconciliation, the bucket probe, the peer transport and the node record are done. No fleet-wide settling check. |

App deploys swap code without restarting pods (doc 10). Pods restart only for platform upgrades and Kubernetes maintenance.

## 4. D7: object store

| Topic | Decision |
|---|---|
| **Backends** | S3 and GCS. S3-compatible stores (MinIO first) for self-hosting and testing, through the S3 backend. Azure only if requested. |
| **Error contract** | As in celld: only a clean `412`/`409` means "not written"; everything else is ambiguous. |
| **Startup probe** | Each node checks the store before serving, and refuses to start if a check fails (development override only). It adds to celld's single-writer checks: **16 concurrent create-if-absent writes to one key, exactly one must win**, and **a write must show up in an immediate listing**. |
| **Conformance suite** | The same checks at scale, with fault injection: in CI against single-node MinIO and a GCS emulator; on demand against S3, GCS and any S3-compatible store a deployment wants to use. |

**MinIO note (checked 2026-10-14):**
- The community edition is archived and source-only (AGPLv3); its prebuilt images stopped in October 2025.
- A bug reported in March 2026 let conditional PUTs through without read quorum, which the race check catches.
- Single-node MinIO is fine for testing. A self-hosted production store has to pass the probe and the suite first.

## 5. Open questions

1. **Default shutdown mode:** `in-place` (recommended: simplest, no data movement) or `hot`?
2. **Self-hosted production store:** MinIO is unmaintained; candidates are SeaweedFS, Garage and Ceph RGW, each checked with the probe.
3. **Defaults to confirm by measurement:** the crash window and ladder thresholds, activation caps, the 30 s hold (measure StatefulSet pod restart time on GKE, on the same machine and with a disk re-attach), and the rebalancing pace and deadband.

**Risk:** the replay of untouched requests is safe only if every side-effecting host import marks its task as touched. The default for any import not on an allow list is "touched".

## 6. Impact on the codebase (LOC)

Against D1 + D6 + D7: **9,295** production lines (measured). All additions are estimates. Claims, restore, cleanup and the rebalancing mechanism are counted in doc 12; eviction in doc 01; forwarding in doc 13.

| | Removed | Added *(est.)* |
|---|--:|--:|
| **D1:** state machine 600–1,200; executor with fast path 400–800; activation policies 100–250; crash handling 300–600; invariant checks 50–100 | −4,074 | +1,450 to +2,950 |
| **D6:** pressure 350–700; load summary 100–200; placement policies 100–250; rebalancing policy 200–400; shutdown hold-and-retry 100–250; `hot`/`all` modes 200–400; readiness 50–100 | −4,060 | +1,100 to +2,300 |
| **D7:** S3 + GCS backends 600–900; stricter probe 150–300 | −1,161 | +750 to +1,200 |
| **Total** | **−9,295** | **+3,300 to +6,450** |
| **Net** | | **≈ −2.8k to −6.0k** |

Tests: +0.7k to +1.4k (simulator scenarios, conformance suite). Later, not counted: `adaptive` activation, CPU in the signal, attributing host crashes to cells, automatic rollback (+0.3k to +0.7k).

## 7. Build order

1. S3 and GCS backends, the stricter probe, and the conformance suite on MinIO in CI.
2. The lifecycle state machine and executor in the pure core, with simulator coverage.
3. The fast path for running cells; `fixed` activation limits.
4. Crash handling, the ladder and quarantine; then reports and the recovery hook.
5. The pressure ladder.
6. Shutdown `in-place` with hold-and-retry; then `hot` and `all`.
7. Placement: `claim-local`, then the load summary, `pressure-aware` and rebalancing.
