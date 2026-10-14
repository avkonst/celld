# 15 — Change data capture to Kafka, through the bucket (new platform feature)

**Status:** Explored (design sketch). **Decided:** changes reach Kafka **through the V9 bundle stream in the bucket**, sent by a separate CDC reader; 1–4 s of latency is acceptable; after-state only; tables and columns declared per app; app outboxes kept separate. **Open until the feature is built:** the capture mechanism (§3.3).
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). **This is a new feature: celld has no change data capture**, so nothing in celld is replaced and the code impact is purely added.
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md).
**Related:**
- [12 §9 — V9 (decided)](12-durability-and-single-writer-options.md): the bundle stream, snapshots and manifests, garbage collection, idle cleanup, restore and takeover;
- [14 — lifecycle, placement and pressure](14-lifecycle-placement-and-object-store.md): crash handling, release and handoffs;
- [10 — D9 simplified](10-drop-peer-auth-simplify-deployment.md): the deployment manifest, which carries the CDC declarations;
- [11 — cell concurrency](11-cell-concurrency-component-model-async.md): host-owned transaction boundaries;
- [02 — SSE](02-sse-instead-of-websockets.md) and [08 — durable steps](08-workflows-as-durable-steps.md): the app-level `_events` log and `_outbox`, which stay separate from this feature.

---

## 1. Premise

Each cell keeps its state in its own SQLite database. That is good for per-entity work, but anything that needs data from **many cells at once** has nowhere to read it from:
- queries across entities (lists, search, reporting);
- analytics and the warehouse;
- other services reacting to changes.

Without platform support, every app would build this itself: an outbox table, a delivery loop, retries, and handling for moves between nodes. The baseline docs only provide app-level pieces: doc 02's `_events` log and doc 08's `_outbox`, each written and driven by the app.

V9 (doc 12 §9.5) already uploads every node's committed changes to the bucket in per-node bundles, in order. Those bundles carry SQLite pages, not rows, but a node can add a small **logical section** of changed rows to the bundle it uploads anyway.

**This exploration asks: should the platform capture row changes from tables an app declares, carry them in the bundles, and have a separate reader deliver them to Kafka, with no CDC code in the app?**

## 2. Goals and non-goals

**Goals**
- An app declares which tables of which cell class go to CDC, and which columns to include or exclude. It writes no CDC code.
- Every committed change to a declared table reaches Kafka, across crashes, Kafka outages, reader outages, cell moves and restores.
- **Kafka never sees a change that a restore could take back.** Only changes already in the bucket are sent.
- Nodes don't talk to Kafka. Kafka's latency and outages never affect cells.
- Messages fit **log-compacted topics**: the latest value per key is the row's current state.
- Delivery within **1–4 s** of the commit (bundle interval, plus the upload, plus the reader's poll).

**Non-goals**
- Sub-second delivery.
- **Before-images.** Messages carry the row after the change, or only the primary key for a delete.
- **Every intermediate version.** If a row changes several times within one bundle interval, consumers get the latest version (§3.4). This is state replication, not an event log.
- **App outboxes and event logs.** Doc 08's `_outbox` and doc 02's `_events` stay app-level, with their own semantics. They are never mixed into this feature.
- **Transactions across rows or cells.** Consumers see per-key order, not transaction boundaries.
- **Postgres data** (doc 05). Postgres has its own logical replication (and Debezium) for that.
- **Brokers other than Kafka.** Anything that speaks the Kafka protocol works (Apache Kafka, Strimzi, Redpanda, managed Kafka on any cloud).

---

## 3. Design

### 3.1 Overview

```
node, in the cell's transaction (host-owned tx boundary, doc 11)
  ├─ app writes to a declared table
  └─ platform upserts (table, pk) into _cdc_pending           ← same transaction (§3.3)

node, when it builds the next bundle (doc 12 §9.5, every 0.5–2 s)
  ├─ at the bundle's cut: read pending keys + current values (included columns)
  ├─ write them as the bundle's CDC section                    ← no extra PUT
  └─ bundle PUT acknowledged → delete those pending rows

CDC reader (separate process, the only Kafka client)
  ├─ follows each node's bundles in order (node list + next seq; no LIST)
  ├─ ranged GET of the CDC section only
  └─ Kafka transaction: the bundle's messages + the reader's cursor
```

### 3.2 Declarations

CDC is declared in the deployment manifest (doc 10), per cell class and table:

```toml
[[cdc]]
class   = "Shop"
table   = "orders"
include = ["status", "total", "updated_at"]   # or: exclude = ["notes", "customer_email"]
topic   = "shop.orders"                       # optional; default "<app>.<class>.<table>"
```

- **Exactly one of `include` or `exclude`.**
  - With `include`, columns the app adds later are **not** sent until declared. This is the safe choice for PII.
  - With `exclude`, new columns are sent automatically.
- **Primary key columns are always sent**, as the message key. A declared table must have an explicit PRIMARY KEY; the host checks this when it first captures the table and reports a declaration error otherwise.
- **Tables are created by the app at runtime** (Durable Object style), so deploy can only check the declaration's syntax. The host checks tables and columns against the live schema, and again whenever the schema changes (`PRAGMA schema_version`). An unknown column becomes a metric and a log line, not a failed write.
- **Dynamic cells** (doc 04) have no deployment manifest of their own. They are out of scope for the first version (§6).

### 3.3 Capture: knowing which keys changed (open)

The platform needs to learn, **inside the app's transaction**, which rows of declared tables changed, so that it can record their keys in the same commit. Two mechanisms are possible. **The choice is left open until the feature is built.**

| | **Cap-B: generated triggers** | **Cap-C: a host hook** |
|---|---|---|
| How | At deploy and on each schema change, the host creates `AFTER INSERT/UPDATE/DELETE` triggers on each declared table. Each trigger upserts `(table, pk)` into `_cdc_pending`. | SQLite's preupdate hook calls the host on every row change; the session extension is a packaged form of it. The host collects `(table, pk)` in memory during the transaction. Since the host owns `COMMIT` (doc 11: storage imports are synchronous and raw `BEGIN`/`COMMIT` is blocked by the authorizer), it writes the pending rows just before committing. |
| For | Plain SQL; any SQLite build; easy to inspect | Nothing in the app's schema except `_cdc_pending`; nothing to regenerate on schema changes; less work per row |
| Against | Triggers must be regenerated when the schema changes, and the app can see (or break) them; extra SQL work per row | Rust and FFI code in the host; SQLite must be built with the hook enabled |
| Code *(est.)* | +200 to +400 | +200 to +400 |

Both mechanisms handle a primary key that changes in an update. They mark both the old key (sent as a delete) and the new key (sent as an upsert).

Example of a Cap-B trigger:
```sql
CREATE TRIGGER _cdc_orders_upd AFTER UPDATE ON orders BEGIN
  INSERT INTO _cdc_pending(tbl, pk, seq) VALUES ('orders', <pk of NEW>, <next seq>)
  ON CONFLICT DO UPDATE SET seq = excluded.seq;
  -- plus a row for <pk of OLD> when the key changed
END;
```

### 3.4 The pending table

```sql
CREATE TABLE _cdc_pending (
  tbl TEXT    NOT NULL,
  pk  BLOB    NOT NULL,        -- encoded primary key
  seq INTEGER NOT NULL,        -- per-cell counter at the latest change of this key
  PRIMARY KEY (tbl, pk)
);
```

**Why it's needed, even though bundles are built from the database:** a bundle can always be rebuilt from the database after a crash, because the pages are there. A list of *which rows changed* cannot be rebuilt from pages. If the keys lived only in node memory, a crash between a commit and the next bundle would lose those changes. With the pending row in the same transaction, the commit itself records "this key must be sent".

**Properties**
- **It holds keys, not data.** Row values are read when the bundle is built, so the table never duplicates the app's data.
- **It is small.** Repeated changes to one key collapse into one row, and rows live only until the bundle that carries them is uploaded, roughly one bundle interval.
- **It moves with the cell.** It is ordinary cell data, in bundles and snapshots.
- **`seq`** is a per-cell counter kept in the platform metadata table (doc 12 §9.3). Together with the claim generation it forms the message version `<gen>-<seq>` (§3.7).

**The trade-off:** a key that changes several times within one bundle interval produces one message with the latest values. A key deleted and re-inserted within the interval produces only the upsert. This is correct for compacted topics and state mirrors, and is not suitable for an audit trail. An audit trail is a job for the app's own event log (doc 02) or outbox (doc 08).

### 3.5 Node side: the CDC section of a bundle

V9's bundle is a header index followed by each changed cell's page segment (doc 12 §9.5). CDC adds **one contiguous section at the end, with its own entry in the header index**, so the reader fetches it with one ranged GET and never downloads pages.

**Building it**
1. For each cell in the bundle that has pending rows, the builder reads the pending keys and the current included columns of each key **in the same read transaction that defines the bundle's cut**. The values therefore match exactly the commits the bundle's pages contain, never a later commit that a restore could lose.
2. Each entry holds `cell, gen, seq, table, pk`, plus the values for an upsert or nothing for a delete. If the row exists, the entry is an upsert; if not, a delete.
3. **Predecessor marker.** In a cell's first entry under a new claim, the node adds where the cell's history came from: the previous owner's identity, incarnation and last bundle seq. The node knows these from the manifest or snapshot it restored from. The reader uses the marker to keep order across moves (§3.6).

**After the PUT is acknowledged,** the node deletes exactly what it carried: `DELETE FROM _cdc_pending WHERE tbl = ? AND pk = ? AND seq = <carried seq>`. If the key changed again in the meantime, its `seq` differs and the row stays for the next bundle. The delete can use `relaxed` durability (doc 12 §9.2); if a crash loses it, the key is carried again with the same version, which is harmless.

**Recovery and lifecycle**
- **App crash** (a wasm trap, doc 14 §2): the open transaction is rolled back, and its pending rows with it (Cap-C drops its in-memory key buffer).
- **Node start:** a background scan finds cells whose pending rows were already uploaded but not yet deleted, and includes them in the next bundle.
- **Release** (idle cleanup, hard pressure, handoffs; doc 12 §9.1 step 7, doc 14 §3): the existing "wait for the stream to be complete" step also covers the CDC section, so nothing new waits on Kafka. Any pending rows left in the final snapshot are carried again by the next owner, with the same version.
- **Takeover and lossy restores** (doc 12 §9.6): the commits after the last uploaded bundle are lost, and so are their pending rows. Those changes were never sent, so Kafka and the cell still agree. **No rewind handling is needed.**

### 3.6 The CDC reader

A separate process (a mode of the node binary, reusing its bucket client and bundle format) is the only Kafka client in the platform. Nodes need no Kafka client or credentials.

**Following the stream**
- The reader learns node identities and their incarnations from the node list (`nodes/<identity>.json`), as nodes do.
- Per identity and incarnation it keeps a cursor: the next bundle seq. It polls with a GET for that object; a 404 means "not yet", and it retries after a poll interval (e.g. 0.5–1 s). There are no LISTs.
- For each bundle: a ranged GET of the header, then a ranged GET of the CDC section.
- **Incarnation change:** the reader finishes the old incarnation, then moves to the new one. It knows the old incarnation is finished from `prev_incarnation_last_bundle` once incarnation fencing lands (doc 12 §9.4 Option B). Until then, it moves when the next seq is absent and the node record names a newer incarnation.

**Order across moves.** Kafka compaction keeps the last message *written* for a key, not the one with the highest version. A cell moving from node A (generation g) to node B (generation g + 1) must therefore have A's entries written before B's.
- When the reader meets a predecessor marker in B's stream, it doesn't emit that cell's entries until its cursor for A has passed the marked bundle. Normally that is already true, because A released the cell before B claimed it.
- The reader keeps no per-cell state for this; the marker carries all it needs.

**Exactly once into Kafka**
- **One Kafka transaction per bundle:** the bundle's messages, plus the reader's cursor record (identity, incarnation, seq) in a compacted cursor topic.
- **On restart** the reader reads its cursors from that topic and continues; nothing is skipped or written twice.
- **One transactional ID per identity stream.** Kafka fences an older reader instance that is still running, so a duplicate reader can't write.
- **Consumers** read with `isolation.level=read_committed`.

The remaining duplicates are the harmless kind from §3.5: the same key carried again with the **same version** after a crash before the pending delete.

**Bundle garbage collection.** V9 deletes a node's bundles below a watermark (doc 12 §9.5). That watermark must now also stay below the reader's cursor for that identity.
- The reader writes its cursor to `cdc/cursors/<identity>.json` in the bucket every 30–60 s, so nodes never have to read Kafka.
- A slower bucket copy is safe: its cursor only lags, so nodes keep bundles longer.
- If the reader is down, bundles simply accumulate in the bucket (§6).

**Deployment.**
- **To start:** one replica (a Kubernetes Deployment). If it restarts, the lag grows for a few seconds, and nothing is lost.
- **For larger fleets:** split identities across several replicas.

**Cost.** About one bundle per node per second means two ranged GETs per bundle, plus polls that find nothing. That is roughly 5–10 million GETs per node per month, **about $2–4 per node per month** at $0.40 per million (doc 12 §9.8). There are no extra PUTs.

### 3.7 Message format

| Part | Content |
|---|---|
| **Topic** | One per declared table; default `<app>.<class>.<table>`, overridable in the declaration |
| **Key** | `{"cell": "<cell id>", "pk": {<pk columns>}}`, so the same row always has the same key, which compaction relies on |
| **Value** | Upsert: JSON object of the primary key plus included columns (SQLite `BLOB` as base64). Delete: **empty value** (a tombstone). |
| **Headers** | `celld-op` (`upsert` \| `delete`), `celld-version` (`<gen>-<seq>`), `celld-schema` (`PRAGMA schema_version`), `celld-code` (deployment version) |
| **Partition** | Chosen by **cell**, not by the whole key, so all of one cell's messages in a topic stay in order relative to each other |

**Ordering:** per cell within a topic, including across moves (§3.6). There is no order across cells or across topics. `gen` is the V9 claim generation, so versions increase across moves.

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| C1 cell storage | `_cdc_pending` and the CDC `seq` counter are platform tables in the cell database. Capture by triggers or the host hook (§3.3). The authorizer hides `_cdc_pending` from guest SQL. |
| D1 lifecycle (doc 14 §2) | A trap rolls back pending rows with the transaction. Nothing else changes. |
| D2–D4 (V9, doc 12) | The bundle gains a CDC section and a header entry for it. The bundle builder reads CDC values at the bundle's cut. New claims carry a predecessor marker. Bundle GC also respects the reader's cursor. Takeover and lossy restores need nothing extra. |
| D5 routing (doc 13) | No change |
| D6 placement, pressure, shutdown (doc 14 §3) | No change: the existing "stream complete" step before a release covers CDC |
| D9 deployment (doc 10) | `[[cdc]]` declarations in the manifest; Cap-B regenerates triggers when the generation changes |
| E1 deploy tool | Validates `[[cdc]]` syntax (class, table, include/exclude, topic) |
| E4 node | A new run mode: the CDC reader |
| E5 telemetry | Nodes: pending rows, CDC section size. Reader: lag per identity (bundles and seconds), produce errors, transaction aborts. Declaration errors. |
| Doc 09 secrets | Kafka credentials, needed by the reader only |
| Doc 02 / doc 08 | No change: `_events` and `_outbox` stay app-level and separate |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

celld has no change data capture, so **nothing is removed**. Everything below is new code *(est.)*.

| | Production | Tools | Test | Examples | Docs (non-blank) |
|---|--:|--:|--:|--:|--:|
| Removed (celld has no CDC) | 0 | 0 | 0 | 0 | 0 |
| Capture, Cap-B **or** Cap-C (§3.3), including the pending table and `seq` counter | +200 to +400 | – | +100 to +200 | – | – |
| Declarations: manifest schema, live-schema checks, column filter | +100 to +250 | +50 to +150 | +50 to +100 | – | – |
| Node side: CDC section at the bundle's cut, predecessor marker, delete after the PUT, startup scan, GC respects the reader cursor | +200 to +400 | – | +150 to +300 | – | – |
| CDC reader: node-list following, per-identity cursors and polling, ranged GETs, ordering across moves, Kafka transactions with the cursor topic, bucket cursor for GC, metrics | +700 to +1,300 | – | +200 to +400 | – | – |
| Consumer guide (keys, tombstones, compaction, versions, `read_committed`) and an example | – | – | – | +40 to +80 | +80 to +150 |
| **Net** | **≈ +1.2k to +2.4k** | **≈ +0.05k to +0.15k** | ≈ +0.5k to +1.0k | ≈ +0.04k to +0.08k | ≈ +0.08k to +0.15k |

The Kafka client library (e.g. `rdkafka`, which bundles librdkafka and supports transactions) is a dependency of the reader and is not counted. Nodes don't link it.

**In the README's combined estimate**, the production and tools ranges above are added to the totals. The production range covers both capture mechanisms.

---

## 6. Risks and open questions

**Risks**
1. **Reader outage or lag.** Cells are unaffected, but delivery stops and bundles accumulate in the bucket, because garbage collection waits for the reader. Alert on reader lag; storage cost grows slowly.
2. **Silent omissions.** With `include`, a new column isn't sent until it is declared. With `exclude`, a new sensitive column is sent automatically. Each app must choose deliberately.
3. **The zombie case** (doc 12 §9.4 Option A): a second process running the same identity could write conflicting bundles until incarnation fencing exists. The reader would follow whichever bundle objects exist.
4. **Bundle format coupling.** The reader depends on the bundle layout, so the CDC section needs a version and the reader must reject versions it doesn't know (the format gate, doc 10).

**Open questions**
- **Capture mechanism:** Cap-B (triggers) or Cap-C (host hook), §3.3. Open until the feature is built.
- **Long reader outages:** should garbage collection wait forever, or should there be a retention cap? Past the cap, the reader would re-send the affected cells' state from their snapshots.
- **Value format:** JSON, or Avro/Protobuf with a schema registry?
- **Dynamic cells (doc 04):** may a parent grant CDC to a child, and where would the child's declarations live?
- **Topic creation:** does the platform create topics (with compaction enabled) on deploy, or does the operator?
- **Reader scaling:** at what fleet size do identities need to be split across reader replicas, and how are they assigned?

## 7. Build order

1. The pending table, the `seq` counter and the capture mechanism (decide Cap-B or Cap-C first), with simulator tests for primary-key changes and repeated updates to one key.
2. `[[cdc]]` declarations in the manifest and deploy tool, the live-schema checks and column filtering.
3. Node side: the CDC section at the bundle's cut, the header entry, the predecessor marker, the delete after the PUT, and the startup scan. Tests: a crash between commit and bundle, a crash between PUT and delete, a key changing while a bundle uploads, a lossy restore (nothing un-sent may appear).
4. The reader: following identities and incarnations, ranged GETs, Kafka transactions with the cursor topic, restart from cursors. Tests: reader crash mid-transaction, two reader instances (fencing), a cell moving between nodes while the reader lags on the old one.
5. Garbage-collection integration through the bucket cursor.
6. Metrics, the consumer guide and an example consumer.

## 8. Next steps

- [ ] Decide Cap-B or Cap-C when the feature is scheduled; prototype both on one table to compare cost per row.
- [ ] Specify the CDC section of the bundle format (entries, predecessor marker, version) together with doc 12's bundle header.
- [ ] Check the Kafka client: transactions in `rdkafka`, and a librdkafka build in the node image.
- [ ] Settle the value format, topic creation, and the long-outage retention policy.
- [ ] Specify the `[[cdc]]` manifest schema alongside the deployment record schema (doc 10).
