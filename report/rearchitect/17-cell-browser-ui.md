# 17 — Cell browser UI: browse cell state, watch live events

**Status:** Proposed. Defaults recorded; open questions in §4.
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). celld has no UI: only `celld cell list/inspect` and the celld.dev explorer (E6, not built).
**Builds on:** [16](16-operations-and-tooling.md) (operator API, mTLS), [14](14-lifecycle-placement-and-object-store.md) (cell states, crashes), [13](13-routing-ingress-and-peer-transport-options.md) (forwarding to owners), [02](02-sse-instead-of-websockets.md) (SSE).

---

## 1. What it shows

| View | Content |
|---|---|
| **Fleet** | Nodes: state, load, pressure level, cell counts by state, code version |
| **Cells** | Filter by class, node, state (Running, Warm, Absent, Quarantined) and crash count; search by cell key. Each row: owner, claim generation, size, last activity, code version. |
| **Cell detail** | Metadata, alarms, crash history; SQLite schema and **read-only, paged rows**; `celld-steps` runs (doc 08); recent requests; its SSE event log (doc 02) |
| **Live events** | A stream of platform events, filterable by cell, class, node and kind:<br>• lifecycle: claim, restore, start, evict, release, crash, quarantine;<br>• requests: method, path, status, latency;<br>• alarms fired; bundle uploads; node joins, drains and restarts. |

It is a state browser and live tail, not a metrics dashboard. Trends and alerts belong in OpenTelemetry tools (doc 16 §3).

## 2. Design

| Topic | Default |
|---|---|
| **Serving** | Static assets embedded in the node binary and served by the operator API (doc 16). Any node can serve the UI. |
| **Data path** | The UI calls one node. That node reads the bucket for claims and the node list. It forwards cell-detail calls to the cell's owner over the peer transport (doc 13), and fans list and fleet calls out to peers. |
| **Live events** | Each node keeps an in-memory ring buffer of recent events (default 10k), filters them on the node, and streams them as **SSE**. The serving node merges its peers' streams for the browser. Resume uses `Last-Event-ID` within the buffer; nothing is persisted (OTel holds history). |
| **Waking cells** | Browsing never wakes or moves a cell. Warm cells are read through a read-only SQLite connection, with no instance. Absent cells show bucket metadata only. |
| **Row access** | A separate permission, off by default. Read-only queries with row and byte limits; large and binary values truncated. Every read is written to an audit log. This is the access control celld.dev's explorer lacked. |
| **Auth** | No login in the platform. The UI sits behind an authenticating proxy (OIDC, e.g. oauth2-proxy or the mesh's authorization), which passes a verified identity to the operator API. Two roles: **viewer** (metadata, events) and **data reader** (rows). |
| **Write actions** | Releasing a cell from quarantine only (as in the CLI), for an operator role. Nothing else in v1. |

## 3. Data access rules

- **Limits:** per-request rows (default 100) and bytes (1 MiB), and a query timeout (2 s), run in the owner's read path so the cell's own traffic comes first.
- **No arbitrary SQL in v1:** table browsing with filters and sorting on indexed columns. A free-form read-only SQL console is an open question.
- **Event stream limits:** one fan-in stream per browser session, per-session filters applied on the nodes, and a cap on events per second, with drop counts shown.

## 4. Open questions

1. **Frontend stack:** server-rendered HTML with htmx and its SSE extension (no JS build, smallest), or a small TypeScript SPA (richer tables and live views)? Recommended: htmx first.
2. **SQL console:** add read-only free-form SQL for data readers, or keep table browsing only?
3. **App-defined views:** should apps register custom inspectors for their own tables, e.g. by reflectapi schema annotations?
4. **Row-level redaction:** do apps need to mark sensitive columns that the UI never shows?

## 5. Impact on the codebase (LOC)

New; celld has no equivalent (0 removed). Estimates only. The operator API base and its authentication are counted in doc 16.

| | Added *(est.)* |
|---|--:|
| Event bus, ring buffer, SSE stream per node | +200 to +400 |
| Fan-out and fan-in across nodes (lists, fleet, merged event stream) | +200 to +400 |
| SQLite browsing endpoints (schema, paged rows, limits, audit) | +200 to +400 |
| Embedded asset serving, roles | +50 to +100 |
| **Backend (Rust)** | **+650 to +1,300** |
| Frontend (htmx templates; a TypeScript SPA would be about double) | +1,000 to +2,000 |
| **Total** | **+1,650 to +3,300** |

## 6. Build order

1. Per-node event bus and the SSE live tail, with filters; one node first.
2. Fleet and cell lists from the bucket plus a fan-out to peers.
3. Cell detail, with forwarding to the owner; schema and paged rows behind the data-reader role, with the audit log.
4. Merged event stream across nodes.
5. Quarantine release action.
