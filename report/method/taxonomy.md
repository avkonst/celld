# celld feature taxonomy (fixed — use these IDs exactly)

Repo: /home/andrey/projects/celld. Three crates: `crates/logic` (pure sans-IO
decision core), `crates/celld` (effect executor, V8 host, CLIs), `crates/ltx`
(vendored SQLite→object-store LTX replication).

Rust and Python application code runs as WebAssembly INSIDE the V8 JS runtime
(Rust via the workers-rs JS shim + .wasm; Python via Pyodide). So almost all
runtime code is shared. Only code that exists *specifically* for wasm modules /
workers-rs shims goes to A5.wasm, and only Python/Pyodide-specific code goes to
A6.python.

## A — Workers runtime (V8 engine)
- A1.engine   V8 isolate engine core: Worker/WorkerIsolate structs, turn
              execution & microtask pumping, op-table plumbing/registration,
              IoContext/request-context machinery, CPU watchdog, heap limits,
              promise/resolver plumbing, timer host ops, console/log ops,
              process exit, generic abort/cancellation plumbing, generic event
              begin/settle/finish, waitUntil, default-export fetch handler
              invocation, global scope setup, unsupported-builtin proxies.
- A2.modules  Module system & bootstrap: ES module resolution/compilation,
              builtin module registry, bootstrap/prelude, compatibility flags
              (`worker_compat`), env/bindings object construction.
- A3.host     Cell runtime host & isolate pool: pool.rs, logic/isolate.rs,
              runtime.rs (cell runtime materialization: StartRuntime/Publish/
              StopRuntime), cell_runtime.rs, cell_host.rs, engine_api.rs,
              host_channels.rs, host_services.rs, CellJob queueing to isolates.
- A4.rpc      Service bindings & JS RPC: service-binding fetch/RPC,
              WorkerEntrypoint/named entrypoints, RpcTarget, stubs & pipelining,
              structured-clone RPC envelope, streams/AbortSignals over RPC,
              ctx.exports, ctx.props, Fetcher get/put/delete helpers,
              request-context confinement of stubs.
- A5.wasm     WebAssembly & workers-rs specific: .wasm import/compile/cache,
              deploy-side wasm discovery/upload (`no_bundle`, `wasm-v1`),
              workers-rs shim Proxy-wrapper class resolution.
- A6.python   Python Workers (Pyodide) specific: python/*.js, worker.py,
              deploy/python.rs, Python-only branches in host/harness.

## B — Runtime (Web platform & Node) APIs
- B1.fetch    fetch(), Request/Response/Headers, bodies, Blob/File, FormData &
              multipart, data: URLs, redirects, outbound fetch host op,
              HTTP stream registry (http_streams.rs), response streaming.
- B2.streams  ReadableStream/WritableStream/TransformStream, byte streams,
              IdentityTransformStream, Compression/DecompressionStream (incl.
              streaming zlib backend), TextEncoder/TextDecoder (+ streams).
- B3.webstd   URL/URLSearchParams/URLPattern, atob/btoa, EventTarget/Event,
              AbortController/AbortSignal, MessageChannel, EventSource,
              structuredClone, DOMException, Cloudflare Date semantics,
              performance, navigator, setImmediate, console formatting.
- B4.crypto   Web Crypto + node:crypto (crypto.js, node_crypto.js, crypto.rs).
- B5.node     Node.js compat except crypto: buffer, stream, util, events, path,
              os, assert, async_hooks, diagnostics_channel, timers, test,
              process, fs (request VFS / bundle fs), zlib one-shot op.
- B6.websocket WebSockets: WebSocket/WebSocketPair, DO hibernation API
              (acceptWebSocket, tags, auto-response), ws_registry.rs,
              js/websocket.rs, ingress accept/proxy (main/websocket.rs),
              outbound client (ws_client.rs).
- B7.htmlrewriter HTMLRewriter.
- B8.sockets  TCP sockets (`cloudflare:sockets` connect/startTls).
- B9.cache    Cache API.

## C — Cloudflare services (bindings)
- C1.do       Durable Objects: namespace/id/stub, DurableObjectState, storage
              API (KV + SQL + transactions, sync KV), storage.rs core,
              storage_ops.rs, input gate (logic/gate.rs, input_gate_lifecycle,
              cell gates in js.rs), DO call dispatch (do_call), sqlite-vec,
              blockConcurrencyWhile, actor abort/break.
- C2.alarms   Alarms & wake discovery: setAlarm/getAlarm/deleteAlarm, alarm
              dispatch & retry (logic/alarm.rs), wake entries / arm gates
              (wake.rs, wake_entry.rs, storage/wake_record.rs, logic/wake.rs,
              logic/schedule.rs), waker role.
- C3.facets   Durable Object Facets (facet_streams.rs, facet ops, DurableObjectFacets).
- C4.kv       Workers KV (kv.js, logic/kv.rs, kv_blob.rs, kv_cli.rs).
- C5.queues   Queues (producer, consumer dispatch, queue cell, queue_policy,
              logic/queue.rs, queue_cli.rs, queue_batching.rs).
- C6.d1       D1 (D1Database, __d1_run, D1 transaction control, d1_cli.rs).
- C7.r2       R2 (r2_ops.rs, r2_store.rs, harness R2Bucket, r2_cli.rs).
- C8.workflows Workflows.
- C9.cron     Cron Triggers (logic/cron.rs, cron cell, arm_cron_schedule).
- C10.assets  Static assets (assets.rs, _headers/_redirects, asset fetch op).
- C11.dynamic Dynamic Workers (Worker Loader / Code Mode) & Tail Workers.
- C12.containers Containers & Sandbox (container.rs, docker.rs, js/container.rs).

## D — Distributed cell engine (fleet)
- D1.core     Decision core & cell lifecycle: logic `on_event`, State, types,
              actor executor (actor.rs + actor/*), effect dispatch,
              activation / idle eviction / hibernation lifecycle, cell_dispatch
              routed local execution, logic/cell.rs, sweep.
- D2.ownership Ownership leases & fencing: ownership_store.rs, node leases,
              epochs, acquire/release/renew CAS, takeover, self-fencing,
              successor handoff (internal_handoff), clean-reload certificate.
- D3.replication SQLite replication & restore: crates/ltx, ltx_repl.rs,
              replication.rs, ltx_replication.rs, logic/restore.rs,
              logic/epoch_gc.rs, output gate & durability proof
              (logic/output_gate.rs, logic/durability.rs, egress gate in js.rs).
- D4.fleetlog Fleet durability ensemble (CELLD_DURABILITY=fleet): node_log.rs,
              recovery_progress.rs, logic/log_tier.rs, logic/log_evict.rs,
              /peer/log/* endpoints, takeover log recovery.
- D5.routing  Routing, ingress & peer transport: public HTTP surface, host/URL
              handling, forwarding to owner, stale-owner retry, peer tunnel,
              logic/routing.rs, logic/http.rs, body limits, connection serving.
- D6.placement Placement & resources: rebalance (logic/rebalance.rs), memory
              pressure (memory.rs, machine.rs, logic/pressure.rs), residency
              cap, local snapshot cache (logic/cache.rs), drain token & shutdown
              handoff (logic/drain.rs, drain_token.rs), dead_node_gc,
              hot-cell overload shedding, load sampling.
- D7.objstore Object-store backends: bucket.rs (S3 / GCS / Azure conditional
              write dialects, credentials).
- D8.security Peer authentication: peer_auth.rs, logic/peer.rs, signed
              requests, replay protection, operator-request HMAC checks.
- D9.deployment Deployment adoption & generations: protocol.rs (bucket
              contract types), generation.rs, fleet.rs deployment loading,
              pointer watcher / adopt_deployment / reload, wake_format.rs,
              logic/format.rs format gate.

## E — Operations & tooling
- E1.deploy   `celld deploy`: Wrangler config parsing, esbuild bundling,
              publication to bucket (service-specific config sections may go to
              the service feature, still class `tool`).
- E2.dev      `celld dev`: dev.rs, local_store.rs, file watching, supervisor.
- E3.opscli   Shared operator CLI: cli_options.rs, cli_output.rs,
              operator_cell.rs, peer_probe.rs, `celld diagnose`, `celld cell`.
- E4.node     Node process & operator API: main()/async_main wiring, CLI
              parsing & help (main/cli.rs), startup.rs listeners, env_vars.rs,
              graceful shutdown of connections, internal operator HTTP routes
              (/state, /reload, evict), asyncrt.rs, tokio_select.rs, logging,
              lib.rs glue.
- E5.telemetry Telemetry: telemetry.rs (Parquet wide events), otlp.rs.
- E6.managed  Managed control-plane connection to celld.dev
              (control_plane.rs: connect/token/credentials, presence agent,
              explorer, deploy agent).

## Class column
- prod  (default) code that runs in a serving node or is the node itself.
- tool  operator/developer CLI command code (deploy, dev, d1/kv/r2/queue/cell
        CLIs, diagnose, connect/token commands).
Test-only code is detected automatically (cfg(celld_internal_tests) items);
do NOT mark it yourself — just give it the feature it tests.

## Spec line format (one rule per line, no '#' inside rules)
    <repo-relative-path> <start>-<end> <FEATURE_ID> [tool]
Ranges are 1-based inclusive physical lines. For each file you own, ranges
must cover EVERY line from 1 to EOF exactly once, in order, no gaps, no
overlaps. Header lines (license, `use` imports, module docs) go to the file's
dominant feature. Attribute at the granularity of top-level items (fn, impl,
struct, JS class/section); split a very long function (e.g. >150 lines) into
sub-ranges when parts of it clearly serve different features. Prefer the
feature that the code *exists for*; shared plumbing goes to the A/D/E feature
that owns it.

Validate with:
    python3 /tmp/claude-1002/-home-andrey-projects-celld/e59268b9-7e9e-4907-a35e-b4f2cc99b7ef/scratchpad/loc.py <your.spec> - --partial
It must print `problems=0`.
