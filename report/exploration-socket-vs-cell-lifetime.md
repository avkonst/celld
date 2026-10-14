# Exploration: socket lifetime vs cell lifetime

Status: exploration, not a plan. Nothing here is implemented. The claims about
celld describe v0.6.2. A claim marked *unverified* comes from reading the code,
and no test has confirmed it.

This page answers one question: can Rust compiled to wasm and running in celld
use services outside Cloudflare, such as Postgres and Kafka, and can it keep
long-lived connections and timers? It records what celld does today, why, and
the options for changing it.

## How a Rust guest runs today

celld embeds V8 and runs Wrangler bundles. A Rust application is a `.wasm`
module behind a JavaScript entry module. The guest reaches the outside world
only through the JavaScript APIs that the harness implements. See
[WebAssembly](../docs/wasm.md).

- [workers-rs](https://github.com/cloudflare/workers-rs) is the usual
  toolchain. The `worker` crate is a runtime dependency compiled into the
  wasm. It wraps the JavaScript APIs through `wasm-bindgen`. `worker-build` is
  a build tool: it runs `wasm-bindgen` and writes the `shim.mjs` entry module.
- celld does not require workers-rs. A module from `wasm-bindgen` with an
  entry module that you write, or a raw wasm module with
  `#[no_mangle] extern "C"` exports, works too. celld accommodates workers-rs
  in a few places: it reads Proxy exports in `js/bootstrap.rs`, gives bindings
  the constructor names that the SDK checks for, and returns a compiled module
  from a `.wasm` import in `js/modules.rs`.
- No WASI host exists. Rust code that needs `std::net`, threads, or a real
  filesystem does not work. All I/O goes through JavaScript.

Some of the wrapped APIs are web standards, and the others are Cloudflare
APIs:

| Web standards | Cloudflare |
| --- | --- |
| `fetch`, `Request`, `Response`, `Headers`, streams | Bindings: KV, R2, D1, Queues, service bindings, RPC |
| `WebSocket`, `URL`, `TextEncoder`, `AbortController` | Durable Objects: state, storage, SQL, alarms, hibernation |
| `crypto.subtle`, `setTimeout`, Cache API | `connect()` from `cloudflare:sockets` |
| | `scheduled`, `queue`, and `alarm` handlers, Workflows, `HTMLRewriter` |

celld implements all of these itself in [harness.js](../crates/celld/js/harness.js),
[sockets.js](../crates/celld/js/sockets.js), and the Rust ops in
[js.rs](../crates/celld/js.rs). It contains no Cloudflare code, so the fork
can extend any of them.

## Outbound connectivity today

| Path | Status | Notes |
| --- | --- | --- |
| `fetch()` | Works | Any HTTP service. |
| `reqwest` in the guest | Should work, untested | On wasm32, reqwest calls the global `fetch()`. The `Request` constructor ignores `mode` and `credentials`, and it throws only on `cache`, so the guest must not call `.cache()`. The wasm build has no `blocking` client, proxy settings, TLS settings, or connection-pool settings, and its futures are not `Send`. |
| `connect()` TCP | Works | `secureTransport` `on` and `starttls` both work. celld blocks no ports, and the fleet network controls egress. |
| Postgres | Works | `tokio-postgres` `connect_raw` over a `worker::Socket`. No Hyperdrive. |
| Kafka | Needs a workaround | `rdkafka` wraps C code that needs threads, so it does not build for `wasm32-unknown-unknown`. Use an HTTP proxy (Confluent REST Proxy, Redpanda HTTP Proxy), write a Kafka wire-protocol client over `connect()`, or run a client in a container. |

### TLS trust is fixed in the host

The guest never runs the TLS handshake. On wasm, reqwest passes the request to
`fetch()`, and celld runs it with its own native client. Each outbound path
uses the Mozilla roots that `webpki-roots` compiles into the binary:

- `fetch()`: the default `reqwest::Client` with the `rustls-tls` feature,
  `redirect_counting_client` in [js.rs](../crates/celld/js.rs).
- `connect()`: `build_tls_connector` in [tcp.rs](../crates/celld/js/tcp.rs).
- Outbound WebSockets: `tls_config` in [ws_client.rs](../crates/celld/ws_client.rs).

celld ignores the system trust store and `SSL_CERT_FILE`. No configuration adds
a root: the one hook, `test_extra_tls_root`, exists only in internal test
builds. celld sends no client certificate and has no `mtls_certificates`
binding. A private CA or mTLS therefore does not work out of the box.

Options:

1. **TLS in the guest.** Open a plaintext `connect()` and run rustls in wasm,
   with the CA and certificate bundled through `include_bytes!` or a secret.
   This needs a crypto provider that builds for wasm (`ring` with clang for
   wasm32, or `rustls-rustcrypto`), a custom `TimeProvider` because
   `SystemTime::now()` panics on `wasm32-unknown-unknown`, and getrandom's
   JavaScript backend. The handshake and encryption count against the CPU
   limit. HTTP then needs hyper's `client::conn` instead of `fetch()`.
2. **Node configuration (recommended).** Add an extra CA file through an
   environment variable, or `rustls-native-certs`, to the three builders above.
   The change is about 30 lines, and `fetch()`, reqwest, and `worker::Socket`
   keep working unchanged. The operator sets it, not the application.
3. **Application configuration.** Implement Cloudflare's `mtls_certificates`
   binding, and possibly a celld field for CA roots. This needs configuration
   parsing, a deploy upload, and a client configuration for each request.

## Timers

- `setTimeout` and `setInterval` are host timers, implemented around the
  `__op_timer` op in [harness.js](../crates/celld/js/harness.js).
  `node:timers/promises` exists too. In Rust, `worker::Delay` awaits one
  delay, and a loop of delays makes an interval.
- In a Durable Object, a pending timer keeps the event alive after the handler
  returns, so it needs no `waitUntil`. In a stateless Worker, a timer ends with
  the request and its `waitUntil` work.
- A timer lives in memory. Hibernation, a drain, failover, rebalancing, or
  eviction under memory pressure discards it. It is *unverified* whether an
  armed interval prevents idle eviction under `CELLD_IDLE_EVICT_S`.
- For a schedule that must survive, re-arm an alarm in `alarm()`. celld stores
  the alarm in the bucket before `setAlarm()` returns, and the alarm fires on a
  hibernated cell. Cron Triggers and Workflow `step.sleep` are the other
  durable schedules.

## Socket lifetime

[cloudflare-compat.md](../docs/cloudflare-compat.md#tcp-sockets) states: "A socket
cannot outlive its event. A Durable Object must reconnect during a later
event."

### This is a gap, not a compatibility requirement

In workerd, the I/O of a Durable Object belongs to the object, not to one
request, so an object can keep a TCP connection across requests. workerd ties
sockets to the request only for stateless Workers. For Durable Objects, celld
is stricter than Cloudflare, so lifting the limit there improves
compatibility.

### Why celld closes the socket

TCP was built as a resource of one event
([tcp.rs](../crates/celld/js/tcp.rs), module comment):

- A socket lives in a process-global registry, and the opening event records
  its id in `IoContext.tcp_sockets`.
- `IoContext::close_sockets` in [js.rs](../crates/celld/js.rs) frees every
  recorded id when the event retires. This is the only cleanup point, so an
  abandoned socket cannot leak.
- The socket ops use `asyncrt::enqueue`, which ties each op to the driver of
  its event and cancels it when the event retires.

An outbound Durable Object WebSocket follows a different design. A host task
holds the socket outside any event, the cell stays resident while the socket is
open, and the socket closes when the cell moves
([ws_registry.rs](../crates/celld/ws_registry.rs),
[limitations](../docs/limitations.md#websockets)). TCP could follow the same
pattern.

*Unverified:* a Durable Object event stays alive while it has pending I/O, and
the socket ops find a socket by id without checking which event owns it. A
read loop in one event might therefore keep a socket open while other events
write to it. This behavior is not a contract. It keeps the object active
forever, and eviction kills it. Do not build on it.

### Option L1: the Durable Object owns its sockets, and events pull data

- Record socket ids on the cell runtime instead of the event. Close them on
  eviction, reset, abort, and move.
- Any later event of the object can read and write. Data that arrives between
  events waits in kernel buffers, and TCP flow control applies.
- The hard part is `socket.readable` in
  [sockets.js](../crates/celld/js/sockets.js). A pending read whose event
  retires is cancelled, and the stream must recover so that the next event can
  read again.
- Decide the residency rule: keep the cell resident as an outbound WebSocket
  does, or close the socket on idle eviction.
- Estimate: several days to two weeks. Most of the time goes into lifecycle
  tests for eviction, reset, drain, facets, and the egress gate.

### Option L2: incoming data wakes the object

- A host task reads the socket and delivers data as a new event, for example a
  celld-only `socketData()` handler, as hibernatable WebSockets do.
- This needs a new event type, ordering through the gates, backpressure, and
  persistence rules. Cloudflare has no such API.
- Estimate: several weeks. This option fits a Kafka consumer that must react
  without polling.

### Trade-offs of long-lived sockets

Pros:

- One connection per object skips the TCP, TLS, and SCRAM handshakes on every
  event.
- Prepared statements and session state survive between events.
- `LISTEN/NOTIFY`, Kafka group membership, and pub/sub become possible.
- Code that relies on sockets owned by a Durable Object on Cloudflare runs on
  celld.

Caveats:

- The application still must reconnect. A kernel socket cannot move to another
  node, and the cell moves on drain, failover, rebalancing, and eviction under
  memory pressure. Store the intent of the connection, and replay it.
- A cell that holds a socket stays resident. Ownership balancing moves only
  hibernated cells, so many such cells pin memory and unbalance the fleet.
- The connection count grows with the number of resident objects. Postgres
  `max_connections` becomes the limit.
- Durable Object events interleave at each `await`, so two events can mix
  their protocol messages on one socket. The application must serialize access,
  or share one client that pipelines safely, such as `tokio-postgres`.
- With L1, nothing reads between events. A Kafka consumer heartbeat times out
  unless an alarm drives it. An idle Postgres connection does not need one.
- Keep the current rule for stateless Workers: their isolates are pooled and
  have no owner to attach a socket to.

## Connection pooling: the better model for Postgres

One connection per Durable Object suits a few long-lived objects. In general a
pool on each node is better, and each event leases a connection from it.
Because a socket ends with its event, the event retirement already releases the
lease. Cloudflare solves the same problem with Hyperdrive.

| Option | Changes to celld | Notes |
| --- | --- | --- |
| P1: PgBouncer next to each node | None | The guest connects to `127.0.0.1:6432` with plaintext TCP and `trust` or md5 authentication. Total connections = nodes × `pool_size`. Credentials stay in the application. Works today. |
| P2: a pooler inside celld behind a Hyperdrive-compatible binding | Large: a few weeks | `"hyperdrive": [...]` in `wrangler.json`, and the operator holds the database credentials. `connect(env.DB.connectionString)` reaches the in-process pooler through an in-memory stream. Existing `pg` and `postgres.js` applications work. Database TLS and mTLS live here. Needs the Postgres startup and authentication messages, for example from `pgwire`. |
| P3: a lease API such as `env.PG.acquire()` | Medium | Not recommended. It is celld-only, and drivers expect a byte stream that starts with a handshake. P2 gives the same semantics through the standard protocol. |

Session pooling holds one lease for each socket, so for at most one event. It
keeps full Postgres semantics, and because events are short it gives most of
the reuse. Transaction pooling multiplexes more, but it breaks session state.
Make transaction pooling opt-in.

Caveats:

- An event that holds a lease across a slow `await` exhausts the pool. Add a
  wait queue with a deadline, a maximum hold time, and limits for each cell or
  script.
- An outbound write waits on the output gate for the object's earlier local
  writes, and that wait lengthens the lease. On a single node every write waits
  for the bucket, so this delay is noticeable.
- Durable Object storage and Postgres do not commit atomically.
- `LISTEN/NOTIFY` needs a dedicated connection, best one for each node with
  fan-out to cells. This is a separate feature.

## A richer Rust API without Cloudflare compatibility

Two layers:

1. **Runtime.** Add the capability as a celld-only JavaScript module, for
   example `celld:sockets` or `celld:tls`. Candidates: TLS options for
   `connect()` and `fetch()`, sockets owned by a Durable Object, durable
   intervals, and pooled database connections.
2. **Guest.** Add a `celld-worker` crate beside `worker` instead of forking it.
   workers-rs re-exports `wasm_bindgen` and `js_sys`, and most of its types
   expose the underlying JavaScript object, so extension traits such as
   `StateExt` can bind the new APIs. Fork only to change existing types.

## A WASI engine instead of V8

WASI defines system interfaces for wasm, such as sockets, HTTP, clocks, and
files, that the host implements without JavaScript glue. In October 2026, WASI
0.2 is stable, and Rust targets it as `wasm32-wasip2`. WASI 0.3 was released on
11 June 2026 and adds native async. Recent Wasmtime releases enable it by
default. Guest toolchains, Rust among them, were still catching up at release.

### Capabilities

| | V8 and workers-rs (today) | WASI on wasmtime |
| --- | --- | --- |
| Languages | JS/TS; Rust and others through JS glue; Python through Pyodide | Rust, C/C++, Go, Python; JS only with a bundled engine |
| Existing Workers applications | Run unchanged | Do not run |
| Network | `fetch()`, `connect()` | `wasi:http`, `wasi:sockets` TCP and UDP, DNS lookup; `std::net` |
| TLS settings | Fixed in the host | No standard interface yet; rustls in the guest is straightforward |
| Threads | None | None; cooperative threads are expected in 0.3.x |
| Native crates | Only through shims backed by JS | Pure-Rust crates work more often; tokio networking on wasip2 is *unverified*; `wstd` exists; C libraries that need threads still fail |
| Durable Objects, KV, R2, Queues | Full Cloudflare API | Only the `celld:*` interfaces you define |
| Socket lifetime | One event | Any lifetime that the host chooses |

### Performance

- Startup: wasmtime instantiates an ahead-of-time compiled module from a
  pooling allocator in microseconds. A V8 isolate needs milliseconds plus the
  11.5k-line harness, and celld pools isolates to hide that cost.
- Memory: an instance costs the memory that it touches. An isolate carries a JS
  heap and the harness, so wasmtime fits more resident cells on a node.
- Compute: TurboFan and Cranelift are close, and the result depends on the
  workload. Benchmark before deciding.
- Chatty I/O: wasmtime wins. Today each operation crosses `wasm-bindgen` glue,
  copies into JavaScript buffers, converts string encodings, resolves promises,
  and sometimes encodes JSON (`op_tcp_connect` parses JSON arguments). With
  WASI the guest calls Rust directly.

### Constraints

- No Cloudflare compatibility, no npm ecosystem, and no path back to Cloudflare.
- The standard is young: WASI 0.3 is four months old, Rust support is still
  settling, no TLS interface exists, and debugging tools are weaker.
- WASI does not express Durable Object semantics. The input gate, the output
  gate, and egress gated on durability need a host implementation, including a
  custom `wasi:sockets` implementation. wasmtime's HTTP crate has a hook for
  outgoing requests that can apply the gate.
- Every feature must ship twice, or ship on one engine only.
- The new host interfaces become the attack surface, as the V8 ops are today.

### What porting would take

Reusable without change: ownership, LTX replication, the bucket, routing, wake
entries, the `logic` crate, [storage.rs](../crates/celld/storage.rs),
[actor.rs](../crates/celld/actor.rs), and
[runtime.rs](../crates/celld/runtime.rs). Earlier work already marks
[engine_api.rs](../crates/celld/engine_api.rs) and
[ws_registry.rs](../crates/celld/ws_registry.rs) engine-neutral.

To replace: about 22k lines of Rust that call V8 (`js.rs` with 12.7k lines,
`pool.rs`, and `js/*.rs`, which together register about 380 host functions),
the 11.5k lines of `harness.js`, and the event lifecycle: retirement, op
lifetimes, and gate claims.

Phases, estimated for one engineer who knows the codebase:

1. **MVP, 2–3 months.** A new deployment type: a component for a `celld:cell`
   world. It exports `handle-request` (shaped like `wasi:http`) and `alarm`,
   and it imports `wasi:http` outgoing, `wasi:sockets`, clocks, random,
   `celld:storage` (key-value and SQL on the existing storage layer), and
   `celld:alarm`. Run one call at a time for each instance, which is stricter
   than input gates and correct. Enforce the output gate at the response and
   on each outbound write. Sockets owned by the cell, pooling, and TLS settings
   are host interfaces from the start.
2. **2–4 more months.** Calls to other cells, KV, R2, Queues, cron,
   hibernatable WebSockets, and concurrency inside an instance through 0.3
   async.
3. **Likely not worth the cost.** Parity with V8 for JS RPC, Workflows, facets,
   Dynamic Workers, tail Workers, and containers.

A cheaper middle step, a few weeks: keep V8 and translate WASI components to
JavaScript with `jco`, mapping `wasi:*` onto `fetch()` and `connect()`. Guest
code written against standard WASI then runs today and moves to wasmtime later.
It brings no performance or semantic gain. Synchronous WASI 0.2 calls need
JSPI, and its support in celld's V8 build is *unverified*.

## Recommendations

1. For Postgres now: run PgBouncer in session mode on loopback on each node
   (P1). It needs no change to celld.
2. For a private CA: add an extra CA file in the node configuration to the
   three TLS builders (TLS option 2).
3. Next runtime feature: sockets owned by a Durable Object (L1), with the
   residency rules of outbound WebSockets. Apply it to Durable Objects only.
4. Self-contained pooling: a Hyperdrive-compatible pooler inside celld (P2).
   Build it if P1 becomes a burden to operate or the operator must control
   database TLS.
5. Expose celld-only capabilities as `celld:*` JavaScript modules plus a thin
   `celld-worker` crate. Do not fork workers-rs.
6. Delay a WASI engine. Reconsider it if Rust-only applications dominate, glue
   overhead shows up in profiles, or residency density becomes the bottleneck.

## Open questions

- Does an armed `setInterval` prevent idle eviction under `CELLD_IDLE_EVICT_S`?
- Does reqwest on wasm32 work end to end against celld's `fetch()`?
- Does the cross-event socket write in a Durable Object, described above,
  actually work?
- Does tokio support networking on `wasm32-wasip2`, and what is the state of
  WASI 0.3 support in Rust?
- Does the embedded V8 build enable JSPI?

## Sources

- [WASI P3 release](https://wasi.dev/releases/wasi-p3)
- [Bytecode Alliance: WASI 0.3](https://bytecodealliance.org/articles/WASI-0.3)
- [Rust wasip2 networking PR](https://github.com/rust-lang/rust/pull/129638)
- [Cloudflare TCP sockets](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)
- [workers-rs](https://github.com/cloudflare/workers-rs)
