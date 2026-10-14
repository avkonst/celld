# 12 — Durability, restore and single-writer options (decided: V9)

**Status:** **Decided (2026-10-14): V9** (§9). The rest of this document is the options catalogue the decision was made from, kept as reference.
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Deployment context:** Kubernetes on Google Cloud today, but **the design must not depend on GCP**. Cloud options are listed alongside self-hosted ones, behind one generic contract (§5).
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md). Part of the **area D review**.

---

## 1. The three independent choices

1. **Durability (R):** how a committed write becomes safe, and when.
2. **Restore and move (S):** where a recovered or moved cell gets its data.
3. **Single writer (L):** what guarantees that only one node writes a cell (or shard) at a time.

**Code reference.** celld's storage and coordination subsystem is D2 ownership (2,979) + D3 replication (12,557) + D4 fleet log (7,030) = **22,566 production lines**. Every variant below is sized against that.

**Latency reference.** Figures are **indicative ranges** from typical published benchmarks and field experience, not measurements. Validate on the target hardware with `fio --fsync=1` plus a SQLite commit benchmark.
- **Self-hosted profile:** enterprise NVMe with power-loss protection (PLP), 10–25 GbE.
- **Cloud profile:** rows marked as cloud.

Two times per write:
- **commit**: when the app gets control back;
- **no-loss durable**: when the write survives the failure the option covers. That is when a gated response can be released.

---

## 2. Durability options (R)

| ID | Option | Who replicates | Commit (app continues) | No-loss durable, p50 | No-loss durable, p99 | Covers | RPO |
|---|---|---|---|---|---|---|---|
| — | SQLite commit without fsync (WAL, `synchronous=NORMAL`) | – | **5–50 µs** (page cache) | not durable until a later fsync | – | Process crash only | – |
| **R1** | Local fsync per commit, enterprise NVMe (PLP) | – | 20–100 µs | = commit | 0.1–0.5 ms | Process and OS crash | **> 0** on disk or host loss |
| R1 | … consumer NVMe or SATA SSD (no PLP) | – | 0.5–5 ms | = commit | 5–20 ms | Process and OS crash | > 0 |
| R1 | … async ship to S3 or peers | App | – | 10 ms – 1 s lag | – | Host loss, minus the lag | > 0 |
| **R2** | Bucket-sync: ack after the commit delta PUT. **LAN** object store (MinIO, Garage, Ceph RGW on NVMe) | App | 5–50 µs + gated | **3–10 ms** | 15–50 ms | Host, disk, rack (store-dependent) | 0 |
| R2 | … **cloud** S3 / GCS in-region | App | 5–50 µs + gated | 20–60 ms | 100–300 ms | Host, disk, zone | 0 |
| **R3** | Own quorum replication (majority follower fsync), same rack | App | 5–50 µs + gated | **0.1–0.5 ms** | 0.5–2 ms | Host, disk | 0 |
| R3 | … cross-rack | App | 5–50 µs + gated | 0.3–1 ms | 1–3 ms | Host, disk, rack | 0 |
| R3 | … cloud cross-zone | App | 5–50 µs + gated | 1–3 ms | 5–10 ms | Host, disk, zone | 0 |
| **R4** | Replicated block volume: DRBD/LINSTOR (protocol C) or OpenEBS Mayastor (NVMe-oF) | Storage software | fsync = durable | **0.1–0.5 ms** | 0.5–2 ms | Host, disk, rack (replica placement) | 0 |
| R4 | … Longhorn (classic "v1" engine) | Storage software | fsync = durable | 1–3 ms | 5–20 ms | Host, disk, rack | 0 — **field feedback (2026-10-14): low throughput.** v1 routes every write through a user-space engine per volume and TCP to the replicas. The SPDK-based v2 data engine is reported generally available since Longhorn 1.12 (May 2026, per conference slides). It needs a dedicated CPU core per node and 2 MiB huge pages. No published v1-versus-v2 benchmark found: **benchmark before use.** |
| **R5** | Ceph RBD (NVMe, BlueStore, 3× replication) | Storage software | fsync = durable | 0.5–2 ms | 3–10 ms | Host, disk, rack | 0 |
| R5 | … CephFS fsync | Storage software | fsync = durable | 1–5 ms | 10–30 ms | Host, disk, rack | 0 |
| **R6** | JuiceFS on a LAN object store (+ Redis, TiKV or etcd metadata) | Storage software | fsync = durable | 5–20 ms | 30–100 ms | Store-dependent | 0 |
| R6 | … on cloud S3 / GCS | Storage software | fsync = durable | 30–100 ms | 200–500 ms | Zone | 0 |
| **R7** | **Cloud zone-redundant disk:** GCP Hyperdisk Balanced High Availability / regional Persistent Disk (2 zones, synchronous); Azure ZRS (3 zones, synchronous) | Cloud | fsync = durable | **1–3 ms** | 5–15 ms | Host, disk, **zone** | 0 |
| **R8** | **Cloud zonal disk:** GCP Hyperdisk / PD zonal, AWS EBS gp3 (io2: 0.2–0.5 ms) | Cloud | fsync | 0.5–1 ms (zonal) | 2–5 ms | Host (re-attach in zone), disk (within zone) | 0 within the zone; **> 0** across zones unless R1-style async ship |

**Not recommended:** NFS-style shared filesystems (Filestore, EFS, Azure Files, plain NFS) for SQLite, because of file-locking and fsync semantics.

**Group commit:** with batching, many concurrent commits share one flush or round trip. Added per-request latency is then about the flush time plus up to one batch interval (typically 0–1 ms), while throughput scales with concurrency.

---

## 3. Restore and move options (S)

| ID | Option | Speed | Works with |
|---|---|---|---|
| **S1** | **Re-attach or re-open the same storage** (no copy) | Seconds to minutes: the re-attach or fencing time | R4, R5, R6, R7, R8 (R8 within its zone) |
| **S2** | **Promote a peer** that already holds a full, current copy (a warm standby) | ~Instant; costs N× storage | R3, when followers keep whole databases |
| **S3** | **Restore from S3/GCS:** snapshot + deltas, optionally paged fault-in (celld) | Proportional to database size; paged restore makes the first request fast | R1, R2, R3, R8 (cross-zone), and as disaster-recovery backup for all |
| **S4** | **Local cache, then peer, then S3** (celld) | Fast when the cache is warm | R1, R2, R3 |

---

## 4. Single-writer options (L)

| ID | Option | Infrastructure | Fencing token | Takeover time | Notes |
|---|---|---|---|---|---|
| **L1** | **Bucket CAS leases** (celld), per shard | Object store only | Epoch, in object keys and followers' accept checks | ≈ lease TTL (celld: 10 s) | The object store must support conditional writes (`If-Match` / `If-None-Match`). **Verify** for the chosen store. |
| **L2** | **etcd, or the Kubernetes Lease API** | etcd (part of Kubernetes) | Lease revision / epoch | ≈ TTL, typically 5–15 s | Watches give push-based routing and deploy notifications |
| **L3** | **Embedded Raft** (e.g. `openraft`) for the ownership map | None (built in) | Raft term / index | Seconds | We own a consensus implementation |
| **L4** | **Storage-level exclusivity** | Comes with R4, R5, R7, R8 | **The storage refuses the second writer** | = storage or orchestrator failover time | DRBD single-primary with quorum; Kubernetes RWO attach; RBD exclusive-lock plus client blocklist; cloud single-writer disk attach. **Watch Kubernetes force-detach delays** on partitioned nodes, which can be minutes unless non-graceful-shutdown handling is configured. |
| **L5** | **Static assignment** (manual failover) | None | – | Manual | Development and small installs only |

**Rule:** L1–L3 are only safe with a **fencing token enforced where the data is written** (object keys, follower checks, the output gate). L4 is enforced by the storage itself.

---

## 5. Generic contract for volume-based options

R4, R5 and R7 (and R8 within a zone) share one contract. The app depends on **the contract**, not on a product:

> **A replicated volume with exclusive attach:** fsync returns only when the write is durable on the configured replicas; at most one node can write the volume at a time; and on node failure, the volume can be attached to another node.

| Environment | Satisfied by |
|---|---|
| GKE (today) | Hyperdisk Balanced HA / regional PD (R7, zone-redundant), or zonal Hyperdisk/PD (R8, zone-scoped) |
| Self-hosted Kubernetes | LINSTOR/DRBD (Piraeus), OpenEBS Mayastor, Longhorn (R4); Ceph RBD via Rook (R5) |
| Azure / AWS | Azure ZRS disks (R7); EBS (R8, zonal only: AWS has no zone-redundant block disk) |

Moving to another environment then means swapping the storage class, not changing the app. **Check how the platform's CSI driver handles forced failover** (attach and detach timing, fencing) in each environment.

---

## 6. Valid pairings (R × L)

| | L1 bucket CAS | L2 etcd / K8s Lease | L3 Raft | L4 storage exclusivity |
|---|---|---|---|---|
| **R1** local + async ship | ✓ (epoch on shipped objects) | ✓ | ✓ | – |
| **R2** bucket-sync | ✓ | ✓ | ✓ | – |
| **R3** own quorum | ✓ (followers check epoch) | ✓ | ✓ (Raft can also *be* the replication) | – |
| **R4** replicated block (DRBD, Mayastor, Longhorn) | – | for scheduling only | – | **✓ primary fence** |
| **R5** Ceph RBD / CephFS | – | for scheduling only | – | **✓** (RBD exclusive-lock + blocklist) |
| **R6** JuiceFS | ✓ | ✓ | ✓ | (its own sessions; still needs a fencing token) |
| **R7** cloud zone-redundant disk | – | for scheduling only | – | **✓** (single-writer attach; force-attach in another zone on failover) |
| **R8** cloud zonal disk | for the cross-zone fallback to S3 (epoch on shipped objects) | ✓ (scheduling and cross-zone fallback) | ✓ | **✓ within the zone** (re-attach) |

---

## 7. Variants (reasonable combinations)

"Size" is the new app's storage and coordination code; "vs celld" is against the 22,566 lines above. Sizes are estimates. "Gated flush" is §8.

| # | Variant | R / S / L | Size | vs celld | RPO: process crash / node loss / disk loss / rack or zone loss | Commit | No-loss durable (p50) | Failover | Infrastructure |
|---|---|---|--:|--:|---|---|---|---|---|
| **V0** | celld today (reference) | write-all fleet log + bucket race / S4 / L1 per cell | 22.6k | – | 0 / 0 / 0 / 0 | µs | ~25 ms fleet, ~90 ms bucket (cloud lab) | ~TTL + restore (~11 s tail in the lab) | Bucket |
| **V1** | Local fsync + async S3 | R1 / S3, S4 / L1 or L2 per shard | ~3.6k–7k | −15.6k to −19.0k | 0 / **lag** / **lag** / **lag** | 0.02–0.1 ms (PLP) | not covered | TTL + restore | Bucket |
| **V2** | Bucket-sync + gated flush | R2 / S3, S4 / L1 or L2 | ~4k–7.5k | −15.1k to −18.6k | 0 / 0 / 0 / 0 (if the store spans racks or zones) | µs + gated | **3–10 ms LAN**; 20–60 ms cloud | TTL + restore | Bucket (latency matters) |
| **V3** | Own quorum + gated flush | R3 / S2, S4 / L1, L2 or L3 | ~7k–13k | −9.6k to −15.6k | 0 / 0 / 0 / 0 (followers in other racks or zones) | µs + gated | **0.1–1 ms** self-hosted; 1–3 ms cross-zone | Seconds (promote a follower) | Bucket (+ etcd for L2) |
| **V4** | **Replicated volume (§5 contract) + gated flush** | R4 or R7 / S1 / L4 (+ L2 for scheduling) | **~0.8k–3.3k** (gated flush 0.3k–0.8k + shard→node glue and optional cold tiering 0.5k–2.5k) | **−19.3k to −21.8k** | 0 / 0 / 0 / 0 (R7: zone; R4: rack, per replica placement) | µs + gated | **DRBD/Mayastor 0.1–0.5 ms**; Hyperdisk HA 1–3 ms; Longhorn 1–3 ms | Re-attach + pod restart: **to be measured** (tens of seconds tuned, minutes on defaults) | Storage layer or cloud disks (+ Kubernetes) |
| **V5** | Ceph RBD + gated flush | R5 / S1 / L4 | ~0.8k–2.8k | −19.8k to −21.8k | 0 / 0 / 0 / 0 | µs + gated | 0.5–2 ms | Fence (blocklist) + map: seconds to tens of seconds | **Ceph: heavy** |
| **V6** | JuiceFS + gated flush | R6 / S1 / L2 | ~0.8k–2.8k | −19.8k to −21.8k | 0 / 0 / 0 / 0 | µs + gated | 5–20 ms LAN; 30–100 ms cloud | Re-open: fast | JuiceFS + metadata store + object store |
| **V7** | Hybrid: V1 by default, per-write "durable" opt-in that waits for V2 or V3 durability | R1 + R2 or R3 / S3, S4 / L1 or L2 | ~5k–10k | −12.6k to −17.6k | Default: as V1. Opt-in writes: 0. | 0.02–0.1 ms default | Opt-in: as V2 or V3 | As V1 / V3 | Bucket |
| **V8** | Zonal cloud disk + async S3 (cross-zone fallback) + gated flush | R8 + R1 shipping / S1 in zone, S3 cross-zone / L4 in zone + L1 or L2 across zones | ~3.9k–7.8k | −14.8k to −18.7k | 0 / 0 (re-attach in zone) / 0 within the zone / **lag** on zone failover (or wait for the zone) | µs + gated | 0.5–1 ms (zonal) | In zone: re-attach. Across zones: restore from S3. | Cloud zonal disks + bucket |
| **V9** | **✅ DECIDED: identity-owned disk + fsync per commit + per-node bundle stream + per-cell claims** (§9) | Operator's choice of local NVMe, zonal disk or R7 / S1 (same identity) + S3 lazy / ownership by node identity + per-cell claims; optional takeover | **~3.2k–6.8k** base (+0.1k–0.3k incarnation later, +0.4k–0.8k optional takeover) | **−15.8k to −19.4k** (base) | By disk: R7 0/0/0/0; zonal 0/0/0/**lag** on zone loss; local NVMe 0/**lag**/**lag**/**lag** | `disk` (default): 0.02–0.1 ms NVMe, 0.5–1 ms zonal, 1–3 ms Hyperdisk HA. `relaxed`: 5–50 µs. | = commit (`disk`); `remote`: + bundle interval + PUT | Identity returns via Kubernetes (§9.7). Optional takeover from S3. | Disks + bucket (+ Kubernetes) |

**Observations**
- **Least code with no data loss: V4.** It is portable through the §5 contract: GKE Hyperdisk HA today; **LINSTOR/DRBD or Mayastor** when self-hosted. **Longhorn is demoted:** field feedback reports low throughput on the v1 engine, and v2 (SPDK) is unbenchmarked. Costs:
  - a storage layer (or cloud disks) to run;
  - a failover time to measure and tune;
  - inactive cells on replicated SSD, unless cold tiering is added.
- **No extra infrastructure and no data loss:** V3 (fast) or V2 (if the object store's PUT p50 is ≤ 10–20 ms).
- **Simplest and fastest, accepting the async window on hardware loss:** V1. V7 is the same with opt-in strong writes.
- **Avoid V5 (Ceph)** unless Ceph is already operated. **V6** adds infrastructure for V2-like latency.

---

## 8. Gated flush: commit locally, release responses when the storage has replicated

This is celld's output-gate model **without our own replication protocol**. It applies to V2, V3, V4, V5, V6 and V8. With storage-layer replication, the storage's fsync is the replication point.

1. SQLite in WAL mode with `synchronous=NORMAL`: a commit writes the WAL into the page cache **without fsync** (5–50 µs).
2. **Track each cell's commit position** with `sqlite3_wal_hook`, which fires after every commit. **No custom VFS is needed.**
3. **A group flusher** fsyncs the WAL files of cells with unflushed commits (or calls `syncfs` per volume), then advances each cell's durable position. For V2 the flusher PUTs the commit delta instead.
4. **The output gate:** every egress (response, outbound call, stream or SSE write, socket write) waits until the cell's durable position covers its last commit. In the wasm design that is one `await` inside each host import.

**Guarantee:** RPO = 0 for everything any client or system has seen, as in celld.
- A commit whose response hadn't been released can be lost on power or OS failure. celld promises nothing more.
- A plain process crash loses nothing, because the page cache survives it.

**Conditions:**
- The storage's fsync must mean "replicated": DRBD protocol C, Longhorn synchronous replicas, Ceph RBD, Hyperdisk HA, Azure ZRS.
- **An fsync error must fence or restart the cell or node.** After a failed fsync the data state is unknown; this is the lesson of PostgreSQL's 2018 "fsyncgate".
- Single-writer fencing comes from L4 (or from L1/L2 with a token, for V2 and V3).

| Part | Lines *(est.)* |
|---|--:|
| Commit-position tracker per cell (`wal_hook`) | 50–100 |
| Group flusher (per-file fsync batching or per-volume `syncfs`; fsync-error policy) | 100–250 |
| Output gate (per-cell durable position, wait and notify, cell-wide gating; celld's `logic/output_gate.rs` as the reference) | 100–200 |
| Egress integration at host imports | 50–150 |
| Metrics (flush latency, queue depth, gate wait) | 30–80 |
| **Total** | **≈ 330–780** |

**Response latency added by the gate** (p50 / p99):

| Storage | p50 | p99 |
|---|---|---|
| DRBD / Mayastor | 0.1–0.5 ms | 0.5–2 ms |
| Ceph RBD | 0.5–2 ms | 3–10 ms |
| Longhorn | 1–3 ms | 5–20 ms |
| Hyperdisk HA / Azure ZRS | 1–3 ms | 5–15 ms |
| LAN object store (V2) | 3–10 ms | 15–50 ms |

The app commits in microseconds and never blocks a thread on the flush.

---

## 9. V9 (decided): identity-owned disk, fsync per commit, per-node bundle stream, per-cell claims

**Status:** **decided** (2026-10-14).
- Zombie protection relies on Kubernetes for now; incarnation fencing is a later feature (§9.4).
- Takeover mode is optional and off by default (§9.6).

### 9.1 The model
1. **Each node identity** (a StatefulSet ordinal, e.g. `node-3`) has an attached disk. **The disk type is the operator's choice:** local NVMe, a zonal network disk, or a zone-redundant disk (R7).
2. **Each commit fsyncs locally** before returning (`synchronous=FULL`). Any response or side effect after a commit is therefore after durability **in the disk's failure domain**, with no response gating (§8 isn't needed by default).
3. **The node streams changes to the bucket asynchronously,** in per-node bundles (§9.5).
4. **When a node moves, its disk moves with it** (StatefulSet + PVC re-attach), so its cells stay local.
5. **A cell not on any node** is downloaded lazily from the bucket on its first request.
6. **Unknown cell on a request:** the node tries to **claim** it in the bucket (conditional create). If someone else already holds the claim, it forwards there (doc 13).
7. **Idle cleanup** (TTL since last use, and/or disk-pressure thresholds; both configurable):
   1. stop serving the cell;
   2. wait for the stream to be complete for it, and write a final snapshot and manifest;
   3. **delete the local file;**
   4. **release the claim conditionally** (only if the generation is still ours).

   A crash between steps 3 and 4 leaves a claim with no local data, so the node re-downloads on the next request.
8. **Cell-by-cell rebalancing:** a new or underloaded node asks others to release cells. It claims them and downloads lazily; predictive pre-download is a future option.

### 9.2 Durability levels per commit (app-controlled)

| Level | Commit waits for | Latency | Survives |
|---|---|---|---|
| `relaxed` | Nothing (`synchronous=NORMAL`), with a background fsync bound (e.g. 50–200 ms) | 5–50 µs | Process crash |
| **`disk` (default)** | Local fsync | 0.02–0.1 ms NVMe; 0.5–1 ms zonal; 1–3 ms Hyperdisk HA | The disk's failure domain |
| `remote` | The stream has persisted this commit | + bundle interval + PUT (3–10 ms LAN, 20–60 ms GCS) | The disk and its whole domain |

**The API:**
- `tx(durability: relaxed | disk | remote)`;
- `storage.sync(level)` as an explicit barrier before an important side effect;
- an operator override that treats `relaxed` as `disk`.

**Durability is always a prefix** (WAL order, ordered stream), so mixing levels never leaves holes. **Note:** with request interleaving (doc 11), a `relaxed` commit read by another task can be revealed before it is durable. Document it; apps call `sync()` when it matters.

### 9.3 Ownership: claims, node records, membership

**Bucket layout:**
```
nodes/<identity>.json                      {"identity","incarnation","state":"active|draining|removed","addr",
                                            "prev_incarnation_last_bundle"?, "expires_at"? (takeover mode)}
claims/<hash>/<cell>.json                  {"owner":"<identity>","generation":N}
bundles/<identity>/<incarnation>/<seq>     immutable stream bundles (§9.5)
cells/<hash>/<cell>/snap/<gen>-<seq>.db     immutable per-cell snapshots
cells/<hash>/<cell>/manifest.json           current snapshot + "valid from bundle seq" (rarely written)
```

- **One claim check per placement, never per request.** A node checks or creates the claim when it opens the cell, then trusts its cached ownership while the cell is open. Ownership can change only through:
  - its own release;
  - its identity being marked `removed` in the node list;
  - the opt-in takeover (§9.6), which happens only after its heartbeat expired, and it self-fences before that.
- **Membership is the node list** (`nodes/*.json` with `state`):
  - **scale-down:** drain → release claims → mark `removed`;
  - **ungraceful scale-down:** claims owned by a `removed` identity are free, and any node may claim them with `generation + 1`.
- **Local files carry the claim generation and last stream position** they were written under (a platform metadata table inside the database). Ownership comes from the claim, **never from a file being on disk**:

| Claim in the bucket | Local file | Action |
|---|---|---|
| Mine, same generation as the file | present | Serve |
| Mine, same generation | absent | Re-download |
| Absent (released) | present | The file is only a cache. Re-claim with `generation + 1`; reuse the file only if its stream position equals the bucket's latest, otherwise delete it and download. |
| Someone else's | present | Delete (stale) |
| Mine, different generation | present | Delete (stale), then download |

### 9.4 Zombie protection: the same identity running twice

**Decided (2026-10-14): Option A now. Option B (incarnation) is a later feature.**

**When it happens:** a force-deleted pod, or a partitioned node whose pod Kubernetes was told is gone. It mostly matters for **local disks**. With network disks, a detached old process can no longer write its disk and crashes.

- **Option A, rely on Kubernetes (chosen for now):** StatefulSet guarantees at most one pod per identity unless someone force-deletes. Zero code. **Operations rule:** never force-delete these pods (`kubectl delete --force`); resolve a down node through auto-repair, the out-of-service taint or a remediation operator (§9.7). **Accepted risk until Option B lands:** a manual force-delete while the old process still runs on a partitioned node with a **local** disk can produce two writers for that identity's cells. With network disks, the old process loses its disk on detach and fails instead.
- **Option B, incarnation number (later feature, ≈ 0.1k–0.3k lines):**
  1. Each start conditionally bumps `incarnation` in `nodes/<identity>.json`, and records `prev_incarnation_last_bundle` (the last bundle of the previous incarnation it accepts).
  2. Readers ignore any bundle of an older incarnation above that number, and garbage collection deletes it.
  3. The old process discovers the newer incarnation on its next conditional write of its node record (or a check before publishing a bundle), stops serving, and exits.
  4. Writes it acknowledged locally after the new incarnation started are lost. That window exists only in the double-run case.

### 9.5 The stream: per-node bundles, per-cell snapshots

- **Bundle:** one immutable object per node per interval (e.g. 0.5–2 s). It is a header index of `cell, generation, from-position, to-position, offset, length`, followed by each changed cell's segment (its committed pages for that interval). **One PUT per node per interval, however many cells changed.**
- **Per-cell snapshot + manifest:** compaction writes these periodically for active cells (after N MB of changes or M minutes), and **always before releasing a claim**. A released cell is therefore restorable from its snapshot alone.
- **Recovery after losing a disk** (same identity, empty disk): per cell, take the snapshot, then apply that cell's segments from the identity's own bundles after the snapshot's sequence. Ranged GETs use the bundle header index.
- **Garbage collection:** per node, keep a watermark (the oldest bundle any unsnapshotted cell still needs) and delete below it. No LIST in steady state.
- **Loss window for disk-domain failures:** the bundle interval plus upload time (`remote` commits wait for it).

### 9.6 Optional takeover mode (opt-in)

For operators who'd rather lose the stream lag than wait for an identity to return:
- `nodes/<identity>.json` gets a heartbeat (`expires_at`), renewed every few seconds. That's ≈ $4.30 per node per month on GCS at one write per 3 s.
- The node **self-fences** (stops serving) if it can't renew before its deadline, with a margin.
- Any node may then claim that identity's cells with `generation + 1`, restoring from snapshot + bundles. **Writes after the last uploaded bundle are lost.**

**≈ +0.4k–0.8k lines.** Off by default. One backend, the bucket object, is enough; a Kubernetes Lease isn't needed.

### 9.7 When an identity is down: what Kubernetes automates (the "stuck case")

| Failure | What happens | Time |
|---|---|---|
| Process crash, node healthy | kubelet restarts the container; same pod, same disk | Seconds |
| Pod deleted, node healthy | StatefulSet recreates the identity; the disk re-attaches | Seconds to tens of seconds |
| **Node unreachable** (hung VM, partition) | The node turns `NotReady` (~40 s). Pods are evicted after `tolerationSeconds` (default 300 s). **A StatefulSet pod on an unreachable node stays `Terminating`.** Kubernetes can't confirm it stopped, so it **won't start the replacement**, and the volume stays attached. That's deliberate: it is the at-most-one-writer guarantee. | **Stuck**, until something confirms the node is gone |

**What resolves the stuck case:**
- **GKE, automated:** node **auto-repair** recreates a VM that stays unhealthy. That kills the old process (real fencing), the Node object goes away, the pod is replaced, and the disk re-attaches. The cloud's **node lifecycle** also removes Node objects whose VM no longer exists (e.g. spot preemption). Expect **minutes, roughly 5–15 end to end on defaults**.
- **Kubernetes non-graceful node shutdown:** applying the `node.kubernetes.io/out-of-service` taint immediately force-deletes the pods and force-detaches volumes. A human or automation applies it.
- **Self-hosted (no cloud controller):** run a **node-remediation operator**, e.g. the Medik8s Node Healthcheck operator with self-node-remediation or a fence agent. It fences the node (reboot or power-off) and applies the out-of-service taint. Otherwise a human must.

**What to tune:**
1. Lower `tolerationSeconds` for not-ready and unreachable on the platform's pods (e.g. 30–60 s).
2. Keep GKE auto-repair on and check its timing for the node pool.
3. Self-hosted: deploy a remediation operator with fencing.
4. **Measure** end to end: hang a node's VM, and time until its identity serves again.

**Per disk type while an identity is down:**

| Disk type | Identity comes back |
|---|---|
| R7 (Hyperdisk HA) | In either of its two zones, after force-detach |
| Zonal disk | **Only in its zone.** During a zone outage the pod stays Pending. Use R7, or opt into takeover, if that's unacceptable. |
| Local NVMe (`emptyDir` on local SSD) | Anywhere, empty; re-downloads lazily. Loses the stream lag. |
| Local persistent volume bound to a machine | Stuck until the machine returns, or an operator deletes the claim |

### 9.8 Bucket request costs and rate limits

**Prices** (GCS regional Standard and S3 Standard, checked 2026-10-14):
- writes, including **LIST** on GCS: **$5 per million**;
- reads: **$0.40 per million**;
- GCS multi-region or dual-region Class A: $10 per million.

**Limits:**
- **GCS:** about 1,000 writes and 5,000 reads per second per bucket initially; ramp up by no more than doubling every 20 minutes; **one write per second per object name**; errors are 429 or 5xx (retry with exponential backoff); sequential names create hotspots.
- **S3:** at least 3,500 writes and 5,500 reads per second per partitioned prefix; 503 Slow Down while a new prefix partitions.

**Rules for V9:**
1. **Batch the stream per node** (§9.5). Per-commit uploads would cost ~$26k/month at 2,000 commits/s and exceed GCS's initial write rate. Per-node 1 s bundles cost ~$13 per node per month.
2. **Never overwrite hot objects.** Use immutable bundles and snapshots; write manifests rarely.
3. **Hash key prefixes.**
4. **Claims scale with placements, not requests:** long idle TTLs, and cached lookups.
5. **No LIST in steady state.**
6. **Storm handling:** lazy claims, jittered backoff, gradual rebalancing.
7. **A regional bucket** in the cluster's region.

### 9.9 V9 code size *(estimates)*

| Part | Lines |
|---|--:|
| Stream: per-node bundles, per-cell snapshots and compaction, garbage-collection watermark | 1.2k–2.8k |
| Lazy restore (snapshot + ranged bundle segments) | 0.5k–1.0k |
| Claims: conditional create/delete, generation, cache per placement, "not owner" replies | 0.4k–0.8k |
| Node records + membership list + scale-down rule | 0.2k–0.4k |
| Idle and disk-pressure cleanup with safe ordering + reconciliation table | 0.4k–0.8k |
| Cell-by-cell rebalancing via release requests | 0.3k–0.6k |
| Durability levels (`relaxed`/`disk`/`remote`, `sync()`) | 0.17k–0.41k |
| **Base total** | **≈ 3.2k–6.8k** (−15.8k to −19.4k vs celld's 22,566) |
| Later feature: incarnation (§9.4 Option B) | +0.1k–0.3k |
| Optional: takeover mode (§9.6) | +0.4k–0.8k |

Request forwarding is counted in doc 13.

---

## 10. Impact on the codebase (decided: V9)

### 10.1 Code impact summary (LOC)

| | Production |
|---|--:|
| celld's storage and coordination subsystem (D2 + D3 + D4), measured | 22,566 |
| New-app size, range across variants V1–V9 | ≈ 0.8k (V4, low end) to ≈ 13k (V3, high end); V9 base ≈ 3.2k–6.8k |
| **Decided: V9 base** | **≈ 3.2k–6.8k lines → −15.8k to −19.4k against celld's 22,566** (incarnation later: +0.1k–0.3k; optional takeover: +0.4k–0.8k) |
| Alternatives considered | V9 (base): −15.8k to −19.4k · **V4: −19.3k to −21.8k** · V5/V6: −19.8k to −21.8k · V1: −15.6k to −19.0k · V2: −15.1k to −18.6k · V8: −14.8k to −18.7k · V7: −12.6k to −17.6k · V3: −9.6k to −15.6k |

**The README's combined estimate now replaces D2, D3 and D4 with V9's base estimate.** The 214 D3 lines of the WebSocket frame gate were already counted under doc 02, so 22,352 lines are newly replaced.

## 11. Open questions (for V9 implementation)

1. Is the Kubernetes CSI failover time for the chosen volume type acceptable (V4)? **Measure:** kill a node under a StatefulSet with an RWO volume; time until the pod serves again, with and without non-graceful-shutdown handling.
2. Is RPO = 0 required for every write, or is V1/V7's async window acceptable for some data?
3. What is the object store's PUT p50/p99 on the target network? It decides V2.
4. How many cells, and how many inactive? Millions of inactive cells argue for S3 tiering (V1–V3, V8), or cold tiering added to V4.
5. Shard size and count. Shards are the unit of lease, volume and move in every variant except V0.

## 12. Next steps

- [ ] Benchmark on GKE: Hyperdisk Balanced HA versus zonal Hyperdisk fsync latency (`fio --fsync=1`), and a SQLite commit benchmark with the gated flush.
- [ ] Measure GKE force-attach / RWO failover time for both disk types.
- [ ] Measure GCS (and a candidate LAN object store) PUT latency for V2.
- [ ] If self-hosted portability is a priority: benchmark LINSTOR/DRBD or Mayastor on one test cluster against the same SQLite benchmark. Include Longhorn **v2** only if its operational simplicity is wanted, and drop it if it doesn't match DRBD or Mayastor on fsync latency and throughput.
