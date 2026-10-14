# 02 — Server-Sent Events instead of WebSockets

**Status:** Explored (design sketch, nothing prototyped yet)
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Related:** [01 — Wasm-only runtime](01-wasm-only-runtime.md). It is independent of doc 01's runtime choice.

---

## 1. Premise

Today celld implements real time with WebSockets (feature B6): 3,653 production lines plus about 430 lines of output-gate frame barriers counted under D3. That covers:

| File | Production lines | Purpose |
|---|--:|---|
| `main/websocket.rs` | 1,039 | Ingress accept and proxy |
| `js/websocket.rs` | 668 | WebSocket ops inside the isolate |
| `ws_registry.rs` | 583 | Socket registry |
| `main/peer_tunnel.rs:642-749` | ~100 | WebSocket splicing in the peer tunnel |

It also depends on `fastwebsockets =0.8.1`, pinned exactly because celld uses its `unstable-split` feature.

In the wasm-only design (01), WebSockets have no WASI standard. They would need a custom `celld:websocket` interface.

**This exploration asks: should the default real-time primitive be Server-Sent Events (SSE) for server→client push, with ordinary HTTP requests for client→server messages?**

## 2. Goals and non-goals

**Goals**
- Push events to clients with low latency the moment an event happens in a cell.
- Survive cell ownership moves (rebalance, drain, failover) without losing events. Clients should not need custom resume logic.
- Fit plain HTTP and `wasi:http`, with no new framing protocol on the host.
- Keep the durability rule: no client sees an event the cell has not durably committed.

**Non-goals**
- Replacing WebSockets for every workload. Low-latency bidirectional or binary apps (games, CRDT editors) keep WebSockets as an optional capability (§6).
- Cloudflare compatibility. Like 01, this assumes it is no longer required.

---

## 3. Design

### 3.1 Protocol shape

- **Server → client.** `GET /…/events` returns `200 text/event-stream`. Each event is sent as:

  ```
  id: <epoch>-<seq>
  event: <type>
  data: <payload>
  ```

  A `: keep-alive` comment goes out every N seconds, so proxies and load balancers don't cut the stream as idle.
- **Client → server.** Ordinary `POST` requests to the cell. Each one goes through normal routing, the output gate and status codes, and gets an explicit acknowledgement. Requests can carry idempotency keys. Concurrent POSTs are unordered, so clients that need ordering send sequence numbers.
- **Reconnect and resume.** The client reconnects automatically with `Last-Event-ID`. The server replays from the event log (§3.2) and then continues live. The `retry:` field tells the client how long to wait before reconnecting.

### 3.2 Event IDs and the replay log

- **Each cell keeps an append-only event log in its own SQLite**, for example a table `_events(epoch, seq, type, data, at)`. It is replicated like any other cell data (D3), so it survives node loss.
- **The event ID is `<epoch>-<seq>`.**
  - `epoch` is the cell's ownership epoch (D2). It increases on every activation and takeover, so IDs stay monotonic across ownership moves without any coordination.
  - `seq` increases within an epoch.
- **Replay** is a range query: `WHERE (epoch, seq) > Last-Event-ID`.
- **Retention** is bounded by count or age, configurable per app. If a client's `Last-Event-ID` is older than the retained window, the server tells the client to fully resync: either a dedicated `event: reset` or HTTP 409 on reconnect.

### 3.3 Model A — the cell holds the stream and stays resident (default)

**Decision recorded:** keeping a cell resident while it has subscribers is acceptable. Instant notification needs a live cell to deliver the event, and a resident cell is the simplest way to guarantee one.

**How it works**
- The client's SSE request is routed to the cell's owner node, as today (D5).
- The cell handler keeps the response stream open and registers it as a subscriber.
- Whenever the cell processes an event (a POST, an alarm, a queue message, a call from another cell), it appends the event to the log and writes it to every open subscriber stream.
- While at least one stream is open, the eviction predicate (01 §3.10) treats the cell as active.

**Costs and limits**
- **Residency.** Each cell with subscribers counts against the residency cap (`CELLD_MAX_RESIDENT_CELLS`) and node memory. celld's V8 runtime (the reference) holds about 1,000 resident cells on an 8 GB node (`docs/README.md`); a wasm cell would be much smaller (01 §3.4).
- **Memory pressure.** Shedding today skips cells with active work or live host WebSockets (`README.md`). The same would apply to cells holding SSE streams. Under pressure, the node would have to close streams (clients reconnect and resume) or refuse new subscribers.
- **Ownership moves.** Rebalance moves only hibernated cells (`docs/limitations.md`), so a cell with open streams is never rebalanced. A drain or failover closes the streams. Clients reconnect with `Last-Event-ID`, land on the new owner, and get a replay **without losing events**. That improves on today's WebSocket 1012 closes, where in-flight messages are lost unless the app built its own resume.
- **Fan-out.** All of a cell's subscriber connections terminate on its owner node. A cell with 10k subscribers puts 10k connections on one node. Model B (§3.5) removes that limit.

**Variant A′ — the host holds the stream.** The host keeps the open response and the cell publishes into it. The instance can then be evicted between events while clients stay connected; the next event activates the cell (microseconds in wasm) and it publishes again. Delivery latency is the same, so this is an optimization to apply only if residency becomes the bottleneck. Model A stays the default.

### 3.4 Why "some cell must exist to notify" holds in both models

An event only happens because something reached the cell: a request, an alarm, a queue delivery, another cell's call. The cell must run to process that event, so a live cell is always present at publish time. The design choice is *who holds the client connections between events*:

| Model | Who holds the client connections between events | Cell residency |
|---|---|---|
| **A** | The cell instance | Resident while subscribers exist (accepted) |
| **A′** | The host on the cell's owner node | Can be evicted; re-activates per event |
| **B** | A separate SSE gateway tier | Fully decoupled from subscribers |

### 3.5 Model B — pub/sub proxy (SSE gateway)

For fan-out at scale, or to decouple client connections from cell placement, put a **pub/sub proxy** in front.

```
client ──SSE──▶ SSE gateway (web server proxy) ◀──subscribe── pub/sub
                                                                 ▲
cell ──publish(topic, event)─────────────────────────────────────┘
client ──POST──▶ normal celld ingress ──▶ owner cell (unchanged)
```

**How it works**
1. The client opens SSE on **any** gateway node, for example `GET /events?topic=room:42`, authenticated with a token the app issued (§3.7).
2. The gateway subscribes to `room:42` in the pub/sub layer, unless it already has a subscriber on that topic. It then fans each event out to all of its local client streams.
3. The cell publishes once per event through a host import, `celld:pubsub/publish(topic, event)`, after the event is durable (§3.6). This holds whatever the cell's residency, whichever node owns it, and however many clients subscribe.
4. **Gap-free reconnect.** The gateway uses subscribe-then-backfill:
   - subscribe to the live topic first, buffering events;
   - fetch the backlog after `Last-Event-ID` from the owner cell, using an ordinary request served from its event log (§3.2);
   - send the backlog, drain the buffer, drop duplicates by ID, then continue live.

**Pub/sub backend options**

| Option | Pros | Cons |
|---|---|---|
| **In-fleet**, over the existing signed peer transport (D5/D8). The owner node pushes to the gateway nodes that subscribe to a topic. | Keeps celld's "no external dependencies besides the bucket" property. | Needs a subscription registry (which gateways want which topic). It must be kept in memory and refreshed, not stored in the bucket, which is too slow. |
| **External broker**: NATS, Redis pub/sub, or Kafka when the stream itself needs persistence. | Mature, scalable fan-out. | A new operational dependency. Usually at-most-once delivery, which the event log plus backfill fixes. |

**Benefits**
- Cells stay evictable and movable. Rebalance and drain never disconnect clients, so there are no reconnect storms.
- A cell publishes once, however many clients subscribe. Fan-out scales horizontally in the gateway tier.
- Clients connect to any gateway, with no routing to the owner.
- TLS, HTTP/2 and keep-alives are handled in a tier sized for long-lived connections.

**Costs**
- A new component and a subscription layer.
- An extra hop: typically a few milliseconds on a private network.
- Authorization moves to the gateway (§3.7).
- Ordering. Each topic has a single publisher (the owning cell), so order within a topic follows the cell's order if the transport preserves per-publisher order. After an ownership move, the epoch in the ID keeps IDs monotonic, and the gateway drops anything older than what it has already sent.

**Topics.** By default a topic is one cell, for example `cell:<scope>`. An app can also publish to named topics; that needs a cross-cell ACL model (§3.7).

### 3.6 Durability gate

Today's rule (`crates/logic/output_gate.rs`) is that nothing revealing unproven state leaves the process. An SSE event is egress, so it is treated the same way:
- **Model A / A′:** a stream write waits for the cell's durability proof, as WebSocket frames do today.
- **Model B:** `publish` waits for the proof before the event leaves the node.

A client therefore never sees an event that a crash could take back, and the event log and the live stream always agree.

### 3.7 Transport and security concerns

- **Deployment requirement: TLS and HTTP/2 at the load balancer.** Browsers allow about 6 HTTP/1.1 connections per origin, so a few tabs with SSE exhaust it. celld's ingress is HTTP/1 only and has no TLS, so **SSE deployments must put celld behind a load balancer that terminates TLS and speaks HTTP/2 to browsers** (ALPN `h2`). The load balancer may keep HTTP/1.1 to celld. It must also:
  - forward `X-Forwarded-Host` and `X-Forwarded-Proto`, with `CELLD_TRUST_FORWARDED_HEADERS` set on nodes;
  - impose no response timeout on SSE paths, and an idle timeout longer than the heartbeat interval;
  - not buffer `text/event-stream` responses;
  - leave room for one upstream connection per open stream when sizing node file-descriptor limits.

  This needs no celld code; see [03 — HTTP/2 on the public edge](03-http2-ingress.md), step 1. Step 2 of doc 03 adds dual-stack HTTP/1.1 + h2c on the public listener, so load balancers can multiplex streams to nodes.
- **Idle timeouts.** Send heartbeat comments more often than the shortest proxy idle timeout. Check that celld's own connection timeouts (30 s header-read/idle, `main.rs:3292-3298`) apply only to idle connections, never to an open stream. If cells *consume* SSE as clients, `CELLD_FETCH_TIMEOUT_S` (default 120 s) cuts outbound streams today (report §7, B1); exempt streaming responses or raise the timeout.
- **Auth.** Browser `EventSource` cannot set custom headers. Use cookies, a short-lived token in the query string, or a fetch-based SSE client. In Model B the gateway checks topic access: the app issues a signed token naming the topics, and the gateway verifies it without calling the cell.
- **Binary payloads.** SSE is text (UTF-8), so binary data must be base64-encoded (about +33%). Apps with binary-heavy protocols should use the WebSocket capability (§6).

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| D5 routing | Model A: SSE requests route to the owner as today, as long-lived responses. Model B: gateway nodes need no routing for subscribers; POSTs route as today. |
| D1 lifecycle / eviction | Model A: open streams count as activity. Model A′/B: they do not. |
| D6 placement | Model A: subscriber-holding cells are not rebalanced, and drain closes their streams. Model B: no interaction. |
| D3 output gate | Stream writes and publishes become gated egress channels. They replace the WebSocket frame barriers (`ws_registry.rs:839-1265`). |
| D2 epochs | Reused directly as the event-ID prefix. |
| E4 node | Optional gateway role (Model B). HTTP/2 comes from the load balancer now, and from dual-stack h1/h2c on the public listener later (doc 03). |

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

All figures are code lines, excluding comments and blanks, measured against the baseline report's line map. Rows marked *(est.)* are estimates.

| | Production | Test | Examples | Docs (non-blank) |
|---|--:|--:|--:|--:|
| **Removed: WebSockets dropped entirely** | **≈ 4,280** (3,961 measured + ~320 est.) | 187 | 74 | ~24 |
| Kept, unless managed mode (E6) also changes: `ws_client.rs` | −162 | – | – | – |
| **Added: Model A**, the default (§3.3) | **+200 to +400** *(est.)* | +100 to +200 *(est.)* | +~40 *(est.)* | +~60 *(est.)* |
| **Net with Model A** | **≈ −3.7k to −3.9k** | ≈ −0.1k | ≈ −0.03k | ≈ +0.04k |
| Optional: add A′, the host-held stream registry (§3.3) | +300 to +600 *(est.)* | +150 to +300 *(est.)* | – | – |
| Optional: add Model B, the SSE gateway (§3.5) | +1,500 to +3,000 *(est.)* with in-fleet pub/sub; +800 to +1,500 *(est.)* with an external broker client | +500 to +1,000 *(est.)* | – | – |

**What gets removed** (measured, production code lines):

| What | Production lines | Notes |
|---|--:|---|
| B6 WebSockets as mapped in the report | 3,653 | `main/websocket.rs` 1,052; `js/websocket.rs` 668; `ws_registry.rs` 666; `harness.js` 508 (hibernation API and delivery); `actor.rs` 173; `ws_client.rs` 162; `js.rs` 152; `logic/lib.rs` 107; `main/peer_tunnel.rs` 82 (WebSocket splice); `main.rs` 63; and smaller pieces. The per-file counts include test lines. |
| WebSocket output-gate frame barriers (`ws_registry.rs:839-1265`, mapped to D3) | 214 | Plus 39 test lines |
| WebSocket close-code helpers and `WebSocketCloseScanner` (`logic/schedule.rs:16-140`, mapped to A3) | 94 | |
| WebSocket-related lines inside other features | ~320 *(est.)* | Drain and rebalance 1012-close handling (D6), hibernation bookkeeping (D1), facet sockets (C3), routing (D5), dispatch arms in `main.rs`. This is a text-match lower bound. |

**What stays, unless managed mode also changes.**
- `ws_client.rs` (162 lines) and the `fastwebsockets =0.8.1` dependency stay, because the managed control-plane presence channel (`control_plane.rs`) is itself a WebSocket client.
- Moving presence to SSE or long-polling would remove both.

**What gets added in Model A:**
- the per-cell event log table, range-query replay and `Last-Event-ID` parsing;
- the reset contract for clients beyond the retention window;
- heartbeat writer;
- gated stream writes, one egress channel in the existing output gate;
- counting open streams as activity in the eviction predicate.

### 5.2 Component-level view

| What | Today | With SSE |
|---|--:|---|
| WebSocket ingress, proxy, registry and isolate ops (B6) | 3,653 lines | Removed, or kept only behind the optional capability (§6) |
| WebSocket output-gate frame barriers (in D3) | 214 lines | Replaced by gating stream writes (smaller) |
| Peer-tunnel WebSocket splice (inside B6) | 82 lines | Removed. An SSE response is ordinary HTTP through the tunnel. |
| `fastwebsockets =0.8.1` pinned dependency | used | Removed only if the WebSocket capability is dropped **and** managed presence moves off WebSockets |
| New: event log and replay helper | – | Small: one table plus a range query |
| New (A′): host-held stream registry | – | Small: one direction, append-only |
| New (B): gateway, subscription registry, publish import | – | Medium: a new component |

## 6. WebSockets as an optional capability

Keep WebSockets as a separately granted capability (01 §3.3) for apps that need:
- **low-latency bidirectional traffic** at high message rates (games, collaborative cursors);
- **binary protocols** (CRDT sync such as Yjs, compact game state);
- **ecosystem protocols** built on WebSockets (graphql-ws, MQTT over WS, Phoenix channels).

Nothing else in the platform should depend on it, so it can be built later, or never. WebTransport is the longer-term option for this class of app.

## 7. Pros and cons summary

**Pros of SSE**
- A plain streaming HTTP response. It fits `wasi:http` with no new protocol stack, and removes WebSocket framing, splicing and the pinned unstable dependency.
- Built-in reconnect plus `Last-Event-ID`. Combined with the per-cell event log and epoch-based IDs, cell moves become lossless and invisible to clients.
- Client messages are real requests, with acknowledgements, retries, idempotency and normal routing and durability semantics.
- Proxy- and CDN-friendly, multiplexed over HTTP/2, with simpler backpressure (one direction, bounded).
- Natural for notifications, feeds, dashboards, agent progress and LLM token streaming.

**Cons of SSE**
- Text-only payloads (base64 for binary).
- Per-message HTTP overhead and no ordering for client→server traffic, so chatty interactive apps fare worse.
- Needs HTTP/2 to avoid browser connection limits; celld's ingress is HTTP/1 today.
- Idle-timeout management (heartbeats) and `EventSource` header limitations.
- Model A pins residency per subscribed cell and concentrates fan-out on the owner node. Model B fixes both, at the cost of a new tier.
- Existing WebSocket-based protocols and libraries do not carry over.

## 8. Risks and open questions

**Risks**
1. **Cells with many subscribers under Model A.** A popular cell concentrates connections on one node. The mitigation is a per-cell subscriber cap, with Model B for broadcast-style apps.
2. **Event log growth.** Retention must be bounded, and clients past the window need the reset contract (§3.2).
3. **Subscription registry consistency in Model B (in-fleet).** A stale registry drops live events to a gateway. Backfill on reconnect recovers them, but live latency suffers until the registry catches up.

**Open questions**
- Is per-cell topic enough, or do apps need named cross-cell topics from day one? That decides the ACL model.
- For Model B, in-fleet pub/sub or an external broker? The first keeps the zero-dependency property; the second scales and ships faster.
- What should the default heartbeat interval and event-log retention be?
- Should the gateway role (Model B) run inside celld nodes or as a separate deployment?

## 9. Build order

1. The per-cell event log and the `<epoch>-<seq>` ID scheme.
2. Model A: gated stream writes, open streams counted as activity, and drains or moves closing streams for clients to resume.
3. The deployment requirement: TLS and HTTP/2 at the load balancer (§3.7; doc 03, step 1). Then dual-stack h1/h2c on the public listener (doc 03, step 2).
4. Measure the residency cost of subscriber-holding cells. Add A′ only if residency becomes the bottleneck.
5. Model B (gateway plus pub/sub) when a workload needs fan-out beyond one node, or cells that stay movable while clients stay connected.
6. The optional WebSocket capability (§6), only if a workload needs it.

## 10. Next steps

- [ ] Prototype the event log, replay and `Last-Event-ID` handling in one Durable Object under today's runtime, using a plain streaming response.
- [ ] Verify celld's idle and header timeouts don't cut long-lived responses, and pick the heartbeat interval.
- [ ] Decide between per-cell and named topics (ACL model).
- [ ] Choose the Model B backend (in-fleet or broker) if broadcast fan-out is in scope.
