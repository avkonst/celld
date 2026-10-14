# 13 — Routing, ingress and peer transport (D5, decided: C1 with K1)

**Status:** **Decided.** K1 header cell key, O1 any-node ingress with forwarding, T2 HTTP/2 between nodes over the mTLS mesh (combination C1). The load-balancer hash hint is an optimization (§6.2), not a decision. The options catalogue (§2–§4) is kept as reference.
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md). Part of the **area D review**.
**Related:**
- [02 — SSE instead of WebSockets](02-sse-instead-of-websockets.md): long-lived streams through the forwarder;
- [03 — HTTP/2 on the public edge](03-http2-ingress.md);
- [09 §3.1 — cell-to-cell calls over REST](09-small-features-and-host-interfaces.md);
- [10 — D8 dropped](10-drop-peer-auth-simplify-deployment.md): mTLS between nodes;
- [12 §9 — V9 (decided)](12-durability-and-single-writer-options.md): per-cell claims, claim on first request, node list.
- [14 — lifecycle, placement, object store](14-lifecycle-placement-and-object-store.md): hold-and-retry while an owner restarts; pressure-aware first placement.

---

## 0. Decision

| Choice | Decided | Why |
|---|---|---|
| **Cell key at the edge** | **K1: header `X-Cell: <class>/<instance>`** now. **K4 later, possibly:** an app-defined rule derived from a **reflectapi schema annotation** that marks the routing field (§6.3). Both may coexist. | No effect on app URLs; trivial to extract and fence; API clients set it easily |
| **Reaching the owner** | **O1: any node accepts and forwards to the owner** | Follows from V9: an unknown cell is claimed by the node that receives its first request, and other nodes find the owner from the claim (doc 12 §9). No router tier, no ring to keep in sync with ownership. |
| **Peer transport** | **T2: HTTP/2 between nodes, over the mTLS mesh** | `REFUSED_STREAM` and `GOAWAY` make "not processed" provable, which the retry rule needs; one multiplexed connection per peer pair replaces celld's tunnel pool |
| **Retry rule** | **Re-send only when provably not processed** (§4); `421` "not owner" before the handler runs | Fixes celld's pooled-tunnel resend risk |
| **Load-balancer hash on the cell key** | **Optimization, nothing to decide** (§6.2) | Turns most forwards into local hits when the load balancer supports it; correctness never depends on it |

Combination: **C1** (§5). C2 (router tier), C3 (ring-aligned ownership) and O4 (redirects) are not built.

## 1. What D5 does in celld

D5 is 2,372 production lines: `main.rs` 1,278, `main/peer_tunnel.rs` 541, `logic/lib.rs` 311, `logic/http.rs` 98, and smaller parts. WebSocket splicing is counted under B6 (doc 02).

- **Any node accepts a public request.** The Worker code decides which Durable Object to call, so **the edge doesn't know the cell**.
- **Owner resolution.** The core uses a cached remote route (with the owner's lease deadline). Otherwise it reads the ownership record and the node lease from the bucket.
- **Forwarding.** The request goes to the owner over a **peer tunnel**: an HTTP/1 upgrade on `/peer/tunnel`, with plain HTTP/1 inside, and a pool of 64 idle tunnels per peer and kind (`main/peer_tunnel.rs`).
- **Retry policy** (`logic/routing.rs`):
  - `NotOwner` and `NeverConnected` are retried once;
  - `Ambiguous` (the request may have been delivered) is not retried.
- **Security fences.**
  - Host and authority validation, because the Worker's `request.url` is built from the `Host` header (`logic/http.rs`).
  - Cell-scope charset checks (`logic/cell.rs`).
  - Body limits: 1 GiB public; peer bodies 1 GiB + 1 MiB; control bodies 64 KiB.
- **Known risk (baseline report, D5).** `send_inner` re-sends a buffered body on a fresh tunnel after **any** pooled-tunnel error, without knowing whether the request was delivered (`main/peer_tunnel.rs:428-468`). That bypasses the "ambiguous attempts are not retried" rule, so a non-idempotent request can run twice.

**What changes in the new app:**
- **The edge knows the cell.** A4 RPC is gone (cell-to-cell calls are REST, doc 09), and requests carry the cell key.
- **Peer trust comes from mTLS** (doc 10).
- **WebSocket splicing is gone** (doc 02).
- **Owner lookup reads V9 claims** instead of celld's ownership records and node leases (doc 12 §9).

## 2. Choice 1: how the edge identifies the cell — decided: K1 (K4 possibly later)

| Option | Example | Pros | Cons |
|---|---|---|---|
| **K1** Header — **chosen** | `X-Cell: <class>/<instance>` | Doesn't affect URLs; easy for API clients | Browsers can't set it on navigation or `EventSource`; CDNs ignore it unless configured |
| **K2** Path prefix | `/c/<class>/<instance>/…` | Works everywhere, including browsers and SSE; CDN-cacheable by path | Leaks into app URLs; the app sees the prefix (strip it, or keep it) |
| **K3** Host | `<instance>.<class>.app.example` | Clean URLs; natural tenant isolation (cookies and origins per cell or tenant) | Wildcard DNS and TLS; one origin per cell multiplies browser connections (HTTP/2 helps only per origin) |
| **K4** App-defined mapping — **possible later** | A rule maps request → cell key. Later form: declared in the app's reflectapi schema (§6.3). | Full flexibility; browsers need no header | Runs code before routing (a stateless hop), or needs a declarative rule language |

**Applies to all options:** the cell key must pass the cell-scope charset fence (`logic/cell.rs`). It is a path component and a storage key, so it is a security boundary.

**K1 consequences:**
- **Browser SSE.** `EventSource` can't set headers. Browser clients open SSE streams with `fetch()` and a streaming body reader, which can set `X-Cell` (doc 02's resume IDs work the same way). K4 later removes this constraint.
- **Missing or invalid header:** `400` before any owner lookup.
- **Requests without a cell** (health, readiness, operator routes) use their own paths and never reach routing.

## 3. Choice 2: how a request reaches the owner — decided: O1

| Option | How | Extra hop | Needs | Pros | Cons |
|---|---|---|---|---|---|
| **O1** Any-node ingress + forward (celld) — **chosen** | Any node accepts, looks up the owner in a cache, forwards over the peer transport | **Yes**, with probability ≈ (N−1)/N for N nodes and a random load balancer (90% at 10 nodes); ≈ 0 with the hash hint (§6.2) | Owner lookup (cache + V9 claim) | Dumb load balancer; always correct; no extra tier | +0.1–0.5 ms intra-DC per forwarded request; forwarding code; both nodes hold the connection |
| **O2** Smart router tier | A thin proxy (our own, or Envoy with an external lookup) keeps an **owner directory** and routes directly. Falls back to O1 forwarding when stale. | ~None (stale entries only) | Directory feed: coordinator watch, periodic bucket refresh, or volume-attach events | No hop; nodes serve only their own cells | A tier to run and scale; directory staleness handling; one more failure domain |
| **O3** Consistent hashing at the load balancer, with ring-aligned ownership | The load balancer hashes the cell (or shard) key onto a ring. Nodes own exactly the shards the ring assigns them. | ~None (during rebalancing only) | Shard → node assignment that **matches the ring**; leases for fencing | No directory, no hop, no extra tier | Membership changes move many shards; no load-aware placement; hot shards can't move independently; the ring and ownership must agree |
| **O4** Redirect | Wrong node answers `307` or `421 Misdirected Request` with the owner's address | 1 round trip (client-side) | Owner lookup on every node | Trivial | Bad for browsers and POST bodies; exposes topology. Only for internal or SDK clients. |

**O1 with the hash hint is not O3.** In O3, the ring *decides* ownership. In the decided design, V9 claims decide ownership and the hash only makes the *first* request (and so the claim) land on a stable node. When the ring and the claims disagree, requests are forwarded; nothing breaks (§6.2).

**Internal cell-to-cell calls** (doc 09 §3.1) use the same owner lookup. They take a **local fast path** when the target is on the same node, and otherwise go directly to the owner (no ingress hop).

## 4. Choice 3: peer transport between nodes — decided: T2

| Option | How | Retry safety | Pros | Cons |
|---|---|---|---|---|
| **T1** HTTP/1 upgrade tunnel pool (celld) | `/peer/tunnel` upgrade, plain HTTP/1 inside, pool of idle tunnels per peer | **Weak.** A failure after write is ambiguous, and celld's pooled resend broke at-most-once. | Byte-faithful request forwarding; proven in celld | Port and connection pressure ("~1k calls/s" without the pool); the resend risk; the pool is ours to maintain |
| **T2** HTTP/2 over the mTLS mesh — **chosen** | One multiplexed connection per peer pair, using hyper's HTTP/2 client and server | **Strong.** `REFUSED_STREAM` and `GOAWAY` (last-stream-id) tell the sender a request was *not* processed, so only those cases are retried. | No tunnel pool; flow control per stream; graceful drain built in | HTTP/2 tuning (doc 03 limits); head-of-line blocking on a lossy link, rare in-DC |
| **T3** HTTP/3 / QUIC | Multiplexed over UDP | Strong (like T2) | No TCP head-of-line blocking | Less mature in the Rust server stack; no real benefit in-DC |

**T2 details:**
- **Encryption and identity come from the mesh** (doc 10). The node speaks h2c to its local mesh proxy, or HTTP/2 over TLS with mesh-issued certificates when the mesh runs without sidecars. Either way the app code sees HTTP/2.
- **Protocol-version check** (doc 10 §3.1) travels as a header on every forwarded request.
- **Connection count:** one connection per peer pair (a second one only if the stream limit is reached). Peer stream limits and window sizes follow doc 03's HTTP/2 limits, sized for intra-fleet traffic.
- **Drain:** a node leaving sends `GOAWAY`; streams above last-stream-id are provably unprocessed and re-sent to the new owner.

**Retry rule (fixes celld's known risk):** forward again only when the request **provably wasn't processed**:
- a connect failure;
- `REFUSED_STREAM`, or a stream ID above `GOAWAY`'s last-stream-id;
- an explicit `421` "not owner" reply, sent *before* the handler ran.

Everything else is **ambiguous** and goes back to the client (`502`/`503`), which retries with its `Idempotency-Key` (doc 09).

## 5. Combinations

| # | Combination | Extra-hop latency | Moving parts | Status |
|---|---|---|---|---|
| **C1** | K1 + **O1** + **T2** | +0.1–0.5 ms on (N−1)/N of requests; ≈ 0 with the hash hint | Nodes only | **Decided** |
| C2 | K1 + **O2** + T2 (O1 as fallback) | ~0 | Router tier + directory feed | Not built |
| C3 | K1 + **O3** + T2 (O1 as fallback) | ~0 | Load-balancer config + ring-aligned ownership | Not built; the hash hint (§6.2) gets most of its latency benefit without ring-aligned ownership |
| C4 | Any of the above + **O4** for internal SDK clients only | 1 client round trip on a miss | Small | Not built; revisit if SDK clients need it |

## 6. Design

### 6.1 Request path

```
client ──▶ load balancer ──▶ any node (ingress)
                                │ 1. parse + fence X-Cell (400 if missing/invalid)
                                │ 2. owner cache hit?          ──▶ owner = cached node
                                │    miss: read V9 claim        ──▶ owner = claim.owner
                                │    no claim: claim it (doc 12) ──▶ owner = self
                                │ 3. owner == self?  ──▶ serve locally
                                │    else forward over T2 (marked as forwarded)
                                ▼
                             owner node
                                │ owns the cell?  ──▶ run handler; response leaves after its durability proof
                                │ not owner       ──▶ 421 before running anything (never forwards again)
                                ▼
ingress on 421 / connect failure / REFUSED_STREAM / above GOAWAY:
    invalidate cache, re-read claim, forward once more; otherwise 503 to the client
    (owner restarting: hold and retry with backoff up to a bound, doc 14 §3)
```

- **One forward hop at most.** A forwarded request carries a marker. A node never forwards a forwarded request, so routing loops are impossible. It replies `421` instead.
- **Owner cache.** Keyed by cell, filled from claims, invalidated on `421`, on a peer's `GOAWAY`, and when the node list marks a node `draining` or `removed` (doc 12 §9).
- **Streaming.** Request and response bodies stream through the forwarder with backpressure. SSE responses (doc 02) are long-lived and must not hit idle timeouts (doc 03 §3.1).
- **Limits:** a public body limit, a peer body limit, and a small control-body limit, as in celld.
- **Host/authority fence:** keep `logic/http.rs`'s validation, adding `:authority` for HTTP/2 (doc 03).
- **Durability gate:** a forwarded request's response leaves the *owner* only after its durability proof (doc 12). The forwarder adds nothing.

### 6.2 Optimization: load-balancer hash on the cell key (nothing to decide)

**What:** configure the load balancer to pick the backend by a **consistent hash of `X-Cell`** (ring-hash or Maglev). The same cell's requests then reach the same node.

**Why it works with V9:** the first request for an unknown cell is claimed by the node that receives it (doc 12 §9). With the hash, that node is the hash target, so the cell's later requests arrive where the cell lives. Forwarding drops from ≈ (N−1)/N of requests to the cases below.

**It is only a hint. Correctness never depends on it:**
- **Membership changes** (scale up or down, a node replaced) move about 1/N of keys on the ring. Their cells stay where they are (claims don't move) and the requests are forwarded until rebalancing or a later placement moves them.
- **Cells moved for load** (D6 pressure and drain, V9 rebalancing) no longer match the hash. Requests are forwarded.
- **Hot cells** can't be split by the hash; they never could be in O1 either.
- **Possible later:** V9 rebalancing could prefer the hash target when it moves a cell anyway. Not needed for v1.

**Requirements for the load balancer:**
- L7 routing, with a consistent hash on a request header;
- backends are **pods directly**, not a `ClusterIP` Service, whose kube-proxy spreading would undo the hash.

**Generic implementations** (no GCP dependency):

| Load balancer | Configuration |
|---|---|
| Envoy, or Gateway API implementations built on it (Envoy Gateway, Istio) | `ring_hash` or `maglev` load-balancing policy with a header hash policy on `X-Cell` |
| ingress-nginx | `nginx.ingress.kubernetes.io/upstream-hash-by: "$http_x_cell"` |
| HAProxy | `balance hdr(X-Cell)` with `hash-type consistent` |

**To check on GKE (optimization only):** whether the GKE Ingress or Gateway exposes a header-based consistent hash (backend-service `localityLbPolicy` `RING_HASH`/`MAGLEV` with `consistentHash.httpHeaderName`) for the load-balancer class we use, and with which backend type (NEGs). If it doesn't, put one of the generic options above behind it, or run without the hint.

### 6.3 Later: K4 via a reflectapi schema annotation

The app's reflectapi schema marks the input field that carries the cell key, for example a path parameter. At deploy time the platform derives a **declarative extraction rule** from the schema and the deployment record stores it (doc 10). No app code runs before routing.

- **Allowed sources:** path parameters, query parameters and headers. **Body fields are excluded,** because they would force the ingress to buffer and parse the body before routing.
- **K1 stays valid.** An explicit `X-Cell` header wins, so API clients don't change.
- **Hash hint with K4:** the load balancer can't evaluate the rule. Either the rule is mirrored into load-balancer configuration (a path-based hash), or K4 requests run without the hint.
- **Size:** about +150 to +400 lines *(est.)*, for the rule, its extraction and the schema hook. Not counted until built.

## 7. Impact on the codebase

### 7.1 Code impact summary (LOC)

Against celld's D5: **2,372** production lines (measured). Estimates are marked *(est.)*.

| | Production | Notes |
|---|--:|---|
| **Replaced: D5 as in celld** | **−2,372** | `main.rs` 1,278, `main/peer_tunnel.rs` 541, `logic/lib.rs` 311, `logic/http.rs` 98, smaller parts |
| Added: forwarder (streaming, one-hop marker, `421` handling) | +400 to +800 *(est.)* | |
| Added: owner cache over V9 claims (fill, invalidation on `421`, `GOAWAY` and node-list changes) | +150 to +300 *(est.)* | The claim itself is counted in doc 12 |
| Added: HTTP/2 peer client and server on hyper, with drain | +150 to +300 *(est.)* | |
| Added: pure retry policy | +60 to +100 *(est.)* | Reuses `logic/routing.rs` decisions |
| Added: K1 cell-key extraction, fence and host/authority validation | +100 to +200 *(est.)* | Charset fence from `logic/cell.rs` |
| **Decided (C1): new D5** | **≈ +0.9k to +1.7k** | |
| **Net vs celld's D5** | **≈ −0.7k to −1.5k** | |
| Hash hint (§6.2) | 0 | Load-balancer configuration plus a deployment-guide section |
| Later, not counted: K4 via reflectapi annotation (§6.3) | +150 to +400 *(est.)* | |

**Not built, for reference:** C2 router tier (+1.0k to +2.0k over C1), C3 ring-aligned ownership (+0.2k to +0.5k), keeping T1's tunnel pool (+0.5k to +0.6k), O4 redirects (+50 to +100).

### 7.2 Effect on the combined estimate

The README's combined estimate replaces D5 (−2,372 measured) with C1 (+900 to +1,700).

## 8. Build order

1. HTTP/2 peer client and server over the mTLS mesh, with the protocol-version header and `GOAWAY` drain.
2. K1 extraction and fence; owner resolution from V9 claims (claim on first request, doc 12) with the owner cache.
3. The forwarder: streaming bodies, the one-hop marker, `421` before the handler, and the pure retry rule. Simulator coverage for every retry case, including an ambiguous failure that must **not** be re-sent.
4. Load-balancer hash hint in the deployment guide (generic options; the GKE check).
5. Later, if wanted: K4 rules from the reflectapi schema.

## 9. Open questions and next steps

**Open questions**
- Is GKE's header-based consistent hash available for our load-balancer class (§6.2)? An optimization only.
- Which reflectapi annotation form for K4, and when (§6.3)?

**Next steps**
- [ ] Measure forwarding overhead with T2 on GKE (pod-to-pod HTTP/2 over the mTLS mesh), with and without the hash hint.
- [ ] Check the GKE Ingress/Gateway hash support (§6.2).
- [ ] Specify the `421` reply and the forwarded marker in the peer protocol.
