# 03 — HTTP/2 on the public edge

**Status:** Explored (design sketch). Step 1 is a deployment requirement only; step 2 is not prototyped.
**Date:** 2026-10-14
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Related:** [02 — SSE instead of WebSockets](02-sse-instead-of-websockets.md), which needs this. Independent of [01](01-wasm-only-runtime.md).

---

## 1. Premise

SSE (doc 02) uses one long-lived HTTP response per subscriber. Over HTTP/1.1 a browser allows only about **6 connections per origin**, so a few tabs with SSE streams exhaust the limit and block every other request to that origin. HTTP/2 multiplexes many streams over one connection and removes the limit.

**celld's ingress today:**
- The public and internal listeners both serve with `hyper::server::conn::http1::Builder`, in `serve_http_connection` (`crates/celld/main.rs:3248-3330`). The hyper features are `client`, `server` and `http1` only.
- **The listeners have no TLS.** celld expects a load balancer or a private overlay in front (`crates/celld/startup.rs` warns about running "without built-in TLS").
- The `h2` crate is already in the dependency graph, through hyper and reqwest. It is pinned to ≥0.4.18 because of a remotely triggerable resource-exhaustion advisory (comment in the root `Cargo.toml`).

**A key constraint:** browsers speak HTTP/2 only over TLS (negotiated by ALPN). They never use cleartext HTTP/2 ("h2c").

## 2. Goals and non-goals

**Goals**
- Remove the browser connection limit for SSE and other long-lived responses.
- Keep every existing HTTP/1.1 client working: Kubernetes probes, load-balancer health checks, webhooks, IoT, `curl`, h1-only libraries.
- Keep celld's request semantics identical over both protocols: URL construction, forwarded-header policy, body limits, cancellation, graceful drain.

**Non-goals**
- An HTTP/2-only public edge (§6).
- TLS termination inside celld (option C, §7).
- HTTP/2 for the internal peer transport (a candidate for its own doc, §7).
- HTTP/3 / QUIC.

---

## 3. Design: two steps

### 3.1 Step 1, now: TLS + HTTP/2 at the load balancer (option A)

**A deployment requirement, with zero celld code.**

```
browser ──TLS + HTTP/2 (ALPN h2)──▶ load balancer ──HTTP/1.1──▶ celld public listener
```

- The browser's connection limit applies only to the browser↔load-balancer connection. Once that connection is h2, every tab's SSE streams and requests multiplex over it.
- The load balancer opens one upstream HTTP/1.1 connection per concurrent request or stream. celld already serves that today.

**Requirements for the load balancer**
1. Terminate TLS and advertise ALPN `h2` (and `http/1.1`) to clients.
2. Forward `X-Forwarded-Host` and `X-Forwarded-Proto`, and set `CELLD_TRUST_FORWARDED_HEADERS` on nodes so Worker URLs keep their public host and scheme (`docs/security.md`, "Set the forwarded-header policy").
3. Let long responses run: no response timeout on SSE paths, and an idle timeout longer than the SSE heartbeat interval (doc 02 §3.7).
4. Don't buffer streamed responses. Disable response buffering for `text/event-stream`.
5. Keep HTTP/1.1 upgrades to celld working if WebSockets are still offered (doc 02 §6). Load balancers normally send WebSocket upgrades upstream over HTTP/1.1.

**Node sizing.** Every open SSE stream holds one upstream TCP connection on a celld node. celld raises `RLIMIT_NOFILE` to its hard limit and warns below 8,000 (`startup.rs:118-158`). Size the file-descriptor limit for peak subscribers per node plus normal traffic. Step 2 reduces this.

**Recorded in doc 02** as a deployment requirement (doc 02 §3.7).

### 3.2 Step 2, next: dual-stack HTTP/1.1 + h2c on the public listener (option B)

Serve **both** protocols on the same public port. A load balancer that can speak h2c upstream can then multiplex many client streams over a few connections to each node. HTTP/1.1 clients are unaffected.

```
browser ──TLS + h2──▶ load balancer ──h2c (prior knowledge) or HTTP/1.1──▶ celld public listener (auto-detect)
```

**Work items**

1. **Switch the builder, on the public surface only.**
   - Replace `http1::Builder` with `hyper_util::server::conn::auto::Builder` for `HttpSurface::Public`. It detects the HTTP/2 connection preface and otherwise falls back to HTTP/1.1.
   - Keep `http1::Builder` for `HttpSurface::Internal` and `HttpSurface::Recovery`: the peer tunnel depends on HTTP/1 `Upgrade` (§7).
   - Enable hyper's `http2` feature and hyper-util's `server-auto` feature. Check the feature names against the pinned versions.
2. **Carry today's protections over to HTTP/2.**

   | HTTP/1.1 today | HTTP/2 equivalent |
   |---|---|
   | `header_read_timeout(30 s)`, which also bounds idle keep-alive | A keep-alive ping interval and timeout, plus a connection idle bound |
   | One request in flight per connection | `max_concurrent_streams` (start around 100–250) |
   | Header size from the h1 parser | `max_header_list_size` |
   | – | Flow-control windows: `initial_stream_window_size`, `initial_connection_window_size`. Memory per connection is roughly streams × window, so size them deliberately. |
   | – | `max_pending_accept_reset_streams` and a fixed `h2` version, against "rapid reset" (2023) and CONTINUATION floods (2024) |
   | `CELLD_MAX_REQUEST_BODY_BYTES` per request | Unchanged: it applies per stream |

3. **Fix how the request URL's host is chosen.** `request_url` (`main.rs:1862-1904`) takes the host from `X-Forwarded-Host` (if trusted), otherwise the `Host` header, otherwise `celld.local`. HTTP/2 carries the host in the `:authority` pseudo-header, which hyper exposes as `parts.uri.authority()`. Without a fix, h2 requests that arrive without trusted forwarded headers produce `http://celld.local/…`.
   - Insert `parts.uri.authority()` between the forwarded host and `Host`.
   - Apply the same `well_formed_host` validation. It is a security fence (`crates/logic/http.rs`).
   - Keep the forwarded-header precedence unchanged.
4. **Extend the response-header filter.** `forwards_worker_header` (`main.rs:331-344`) drops `connection` and `transfer-encoding`. On HTTP/2 responses, also drop `keep-alive`, `proxy-connection` and `upgrade`, and `te` unless its value is `trailers`. HTTP/2 forbids these. Either hyper rejects or strips them, or the client errors. Check hyper's current behaviour and make the filter explicit either way.
5. **Re-check per-connection assumptions.** One connection now carries many requests.
   - `ConnectionWorkerRequests` / `IngressAbortGuard` (`actor.rs:660-720`) abort every request of a connection when its transport ends. That stays correct for HTTP/2.
   - Per-stream cancellation (`RST_STREAM`) reaches celld when hyper drops that stream's service future. Confirm that dropping an armed `IngressAbortGuard` publishes the abort for that one request, so a single cancelled stream doesn't wait for the whole connection to end.
   - Graceful drain: `connection.graceful_shutdown()` sends `GOAWAY` on HTTP/2 and lets in-flight streams finish. Re-check the 2 s connection grace (`CONNECTION_DRAIN_GRACE`, `main.rs:61`). One h2 connection can carry many long SSE streams, which then all close together at the deadline. That's acceptable, because clients resume with `Last-Event-ID` (doc 02 §3.2).
   - Connection-error accounting (`record_connection_error`) should record the protocol.
6. **WebSocket upgrades.** h1 clients keep upgrading as today (`fastwebsockets::upgrade::is_upgrade_request`, `main.rs:2846`). WebSockets over HTTP/2 (RFC 8441 extended CONNECT) are not offered. If WebSockets remain as doc 02's optional capability, they are reached through HTTP/1.1 upstream connections, which is what load balancers do by default.
7. **Configuration.** Add `CELLD_HTTP2=0|1`, default `1` once released; `0` serves HTTP/1.1 only, as today. Add bounds for the h2 limits, e.g. `CELLD_H2_MAX_STREAMS`. Validate them strictly, like every other `CELLD_*` setting (E4).
8. **Observability.** Count connections and requests per protocol, and expose them on `/state`. Log h2 protocol errors and resets separately from h1 connection errors.
9. **Tests.** Run the conformance and fleet-lab workloads with h2c clients. Add specific cases:
   - URLs built from `:authority`, with and without forwarded headers;
   - forbidden response headers;
   - per-stream cancellation;
   - `GOAWAY` during drain;
   - the stream-concurrency cap;
   - rapid-reset resistance.

**What step 2 adds over step 1.** Load balancer→node connections drop from one per open stream to a few multiplexed connections. That frees file descriptors and sockets on nodes holding many SSE subscribers. Nodes can also be exposed to h2c-speaking internal clients directly.

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| D5 routing / ingress | Only the public listener's builder, `request_url` and the header filter change. Routing to owners and the peer tunnel are unchanged. |
| D8 security | `well_formed_host` now also validates `:authority`. Forwarded-header trust is unchanged. New h2 abuse limits (§3.2 item 2). |
| E4 node | New `CELLD_HTTP2` and h2-limit environment variables; per-protocol counters on `/state`. |
| D6 placement / drain | Drain sends `GOAWAY`. Shutdown phases are otherwise unchanged. |
| Doc 02 (SSE) | Step 1 is its deployment requirement. Step 2 removes the per-stream upstream connection cost. |

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

All figures are code lines, excluding comments and blanks. No code is removed; step 2 replaces a few lines in place.

| | Production | Test | Docs (non-blank) |
|---|--:|--:|--:|
| **Step 1 (option A):** deployment requirement | **0** | 0 | +~30 (deployment section in `docs/README.md` / `docs/security.md`) |
| **Step 2 (option B):** builder switch, h2 settings and limits | +40 to +80 *(est.)* | | |
| Step 2: `:authority` fallback in `request_url` | +10 to +20 *(est.)* | | |
| Step 2: forbidden-header filter for HTTP/2 | +10 to +20 *(est.)* | | |
| Step 2: `CELLD_HTTP2` and limit variables, help text | +40 to +80 *(est.)* | | |
| Step 2: per-protocol counters, `/state`, error classification | +30 to +60 *(est.)* | | |
| Step 2: per-stream cancellation and drain fixes found in the audit | +20 to +60 *(est.)* | | |
| **Step 2 total** | **+150 to +320** *(est.)* | +200 to +400 *(est.)*, in the external suites | +40 to +80 |
| Step 2: replaced in place | −10 to −20 | – | – |
| **Net (both steps)** | **≈ +130 to +310** | ≈ +200 to +400 | ≈ +70 to +110 |

**Dependency impact.** Enable the hyper `http2` and hyper-util `server-auto` features. No new crates: `h2` is already compiled in and pinned.

### 5.2 Code touched (step 2)

| File | Where | Change |
|---|---|---|
| `crates/celld/main.rs` | `serve_http_connection` (3248-3330) | Auto builder for `HttpSurface::Public`; h2 settings |
| `crates/celld/main.rs` | `request_url` (1862-1904) | `:authority` fallback |
| `crates/celld/main.rs` | `forwards_worker_header` (331-344) | HTTP/2 forbidden headers |
| `crates/celld/actor.rs` | `IngressAbortGuard` (660-720) | Audit and possibly fix per-stream abort on drop |
| `crates/celld/env_vars.rs`, `main/cli.rs` | | New variables and help text |
| `Cargo.toml` (workspace) | `hyper`, `hyper-util` features | `http2`, `server-auto` |

## 6. Why not HTTP/2-only at the public edge

**Pros of h2-only**
- One protocol path, with no HTTP/1 keep-alive or pipelining edge cases.
- Length-prefixed framing removes classic request smuggling (Content-Length versus Transfer-Encoding). An LB that downgrades to HTTP/1 upstream can reintroduce it.
- Multiplexing, per-stream flow control (natural SSE backpressure), and header compression for chatty POSTs.
- `GOAWAY` with the last processed stream ID gives clients precise retry information.

**Cons, which decide it**
- **Browsers need TLS for h2.** h2-only at celld means TLS in celld (option C), or a TLS load balancer in front that always speaks h2c.
- **Common clients speak only HTTP/1.1:**
  - Kubernetes `httpGet` probes;
  - many LB health checks and default target protocols;
  - webhook senders, IoT and older libraries, some corporate proxies;
  - `curl`, without `--http2-prior-knowledge`.

  These would break.
- **TCP head-of-line blocking.** One lossy connection stalls all of its streams. HTTP/3 fixes this, so h2-only isn't future-proof either.
- **Uneven load spreading.** Long-lived multiplexed connections stick to one node behind an L4 load balancer. That skews ingress load, although celld forwards to the owner node anyway.
- **A larger HTTP/2-specific denial-of-service surface,** and harder debugging (binary frames).

**Decision:** dual-stack. Keep HTTP/1.1 at the public edge, and add h2c alongside it.

## 7. Risks, open questions and follow-ups

**Risks**
1. **h2 resource abuse.** Streams, resets, header blocks and SETTINGS frames can be abused. Mitigate with the limits in §3.2 item 2, and keep `h2` patched.
2. **Behaviour drift between protocols.** URL construction, headers and cancellation must match exactly. The tests in §3.2 item 9 run against both.
3. **Drain behaviour.** One h2 connection's `GOAWAY` closes many streams at the deadline. Clients must resume cleanly; for SSE that's `Last-Event-ID`.

**Open questions**
- Which load balancers in target deployments can send h2c upstream? That decides whether step 2 is worth doing early.
- Default `max_concurrent_streams` and window sizes for nodes holding tens of thousands of SSE streams.
- Should `CELLD_HTTP2` default on in the first release, or start opt-in?

**Follow-ups (candidate future docs)**
- **TLS termination in celld (option C).** rustls, ALPN `h2` + `http/1.1`, certificate files and reload, SNI, possibly ACME. Needed only if nodes face browsers directly.
- **HTTP/2 for the internal peer transport.** Here h2-only is attractive, because celld controls both ends:
  - multiplexing replaces the pool of 64 idle tunnels per peer and its ephemeral-port ceiling (`main/peer_tunnel.rs:17-21`);
  - `REFUSED_STREAM` and `GOAWAY` last-stream-ID tell the sender exactly when a request was **not** processed, which addresses the duplicate-execution risk the baseline report found in the pooled-tunnel resend (D5, `main/peer_tunnel.rs:428-468`).

  It is a peer-protocol version change, which has historically meant stop-all upgrades.

## 8. Build order

1. **Step 1:** document the load-balancer requirement (§3.1) in the deployment guide; doc 02 already states it.
2. **Step 2:** build the public listener dual-stack from the start, with the auto builder, the `:authority` fix, the HTTP/2 header filter and the h2 limits. `CELLD_HTTP2` defaults on; `0` serves HTTP/1.1 only, for diagnosis. HTTP/1.1 is always available.

## 9. Next steps

- [ ] Add the load-balancer requirement to `docs/README.md` (deployment) and `docs/security.md` (forwarded headers).
- [ ] Confirm the hyper and hyper-util feature names and the h2 builder settings available at the pinned versions.
- [ ] Prototype the auto builder on `HttpSurface::Public`, with the `:authority` fix and the header filter.
- [ ] Audit `IngressAbortGuard` drop behaviour for per-stream cancellation.
- [ ] Measure file descriptors and memory per node at a target SSE subscriber count, for step 1 versus step 2.
