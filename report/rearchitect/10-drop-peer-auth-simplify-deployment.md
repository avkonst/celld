# 10 — Drop D8 (peer authentication); simplify D9 (deployment adoption and generations)

**Status:** Explored. D8 dropped (decision recorded); D9 simplification sketched.
**Date:** 2026-10-14
**Context:** a green-field app inspired by celld (see the [README](README.md)). Nothing is migrated; celld is the reference design.
**Baseline:** celld v0.6.2 (`90b4301`); see [`../celld-feature-report.md`](../celld-feature-report.md)
**Related:**
- [01 — Wasm-only runtime](01-wasm-only-runtime.md): code trust, component loading;
- [06 — Cron as a library](06-cron-as-library.md): the `on-deploy` hook;
- the coming area-D docs on coordination and placement, which may replace D9's polling with a coordinator watch.

This is the first document of the **area D review**.

---

## 1. Premise

### 1.1 D8: peer authentication in celld

D8 signs requests between nodes with HMAC-SHA256. The shared fleet secret lives in the bucket (`fleet/peer-auth.json`). Each request carries a nonce, a ±30 s clock window and an in-memory replay cache (`peer_auth.rs`, plus the identity and window predicates in `logic/peer.rs`). It covers:
- the establishment of the peer tunnel;
- fleet-log appends, seal and tail (D4);
- the handoff and control routes;
- the reserved-cell operator route. That route is not built anyway (doc 07).

The baseline report rated it **Beta**:
- responses are not authenticated;
- calls inside the tunnel are unsigned;
- `/peer/log/*` reads up to about 1 GiB before verifying the signature;
- there is no key rotation;
- the replay cache is per-process.

celld already **requires a private network** between nodes (`docs/security.md`). D8 is a second, partial layer on top of it.

**Decision recorded:** D8 is not built. Node-to-node trust comes from the network layer (§3.1).

### 1.2 D9: deployment adoption and generations in celld

D9 answers *which code version a node runs, and how it switches without dropping requests*:

| Concept | In celld |
|---|---|
| **Deployment** | `celld deploy` writes immutable code to a content-addressed prefix, then moves the pointer `deploy/current.json`. Version = the first 16 hex characters of a SHA-256 over sorted modules, metadata and the asset index (`protocol.rs`). |
| **Generation** | Everything a node *derives* from one deployment: compiled code, isolate pools, the Durable Object class registry, the service-binding graph, asset resolvers, cron schedules (`generation.rs`). A node holds one current generation through a snapshot, so **a request finishes on the generation it started on**. |
| **Adoption** | Nodes poll the pointer every 30 s (`CELLD_DEPLOY_POLL_S`), build the new generation, and swap it in place. Resident cells move to the new code at safe points between events; the move is forced after 60 s (`CELLD_DEPLOY_MAX_AGE_S`). `POST /reload` forces a rebuild. |
| **Swap pump** | Pure decisions in `logic/lib.rs`: which cells to stop, and in what order, so the swap never runs a cell on two generations. |
| **Format gate** | Each node publishes the newest bucket format it reads. Nobody writes a format a live peer can't read (`logic/format.rs`), which keeps rolling upgrades of the platform safe. |
| **Wake-format migration** | `wake_format.rs`: fleet initialization plus a resumable migration of the alarm wake index **from celld v0.4.1's format** |

**Why it can shrink in the new app:**
- **Most of the generation graph disappears** with the features that aren't built: service bindings (A4), assets (C10), cron (C9), queues (C5), Dynamic Worker loaders (C11, now dynamic cells).
- **A wasm deployment is simple:** a component hash, a manifest and capability grants.
- **There are no legacy formats to migrate,** so the wake-format migration goes entirely.

## 2. Goals and non-goals

**Goals**
- Keep D9's two guarantees:
  - **a request finishes on the code it started on;**
  - **a cell never runs on two code versions at once.**
- Replace D8 with network-level identity that is stronger and maintained elsewhere.
- Keep a minimal **protocol-version check** between nodes, which is compatibility, not authentication.

**Non-goals**
- Multi-version canary rollouts. celld had a `Rollout.percent` field it never honoured; nothing is built here until needed.
- Cross-platform-version rolling upgrades with format changes on day one. A format version field and a gate stub exist, and become real when the first format change happens.

---

## 3. Design

### 3.1 D8 replacement: trust from the network

| Layer | Recommendation |
|---|---|
| **Node-to-node transport** | **mTLS with per-node certificates**: a service mesh, or SPIFFE/SPIRE-issued identities. This gives authenticated *and* encrypted peer traffic, plus a node identity usable in logs. A WireGuard or Tailscale overlay is the minimum acceptable alternative: encryption and network membership, but no per-request identity. |
| **Coordinator** (etcd or Postgres, if chosen in the coming coordination doc) | Its own mTLS / client certificates |
| **Operator API** | Bind it to the private interface. If it must be reachable more widely, put it behind the mesh's authorization rather than an app-level HMAC. |
| **Protocol compatibility** | Keep the `x-cells-peer-version`-style header check, about 20–40 lines, moved to the routing layer (D5). It refuses peers on another protocol version, as today. |

**What is lost:**
- the bucket-held fleet secret;
- HMAC request signing;
- nonce and replay protection;
- protection against an attacker *inside* the private network who has no mesh certificate. mTLS covers that case better than D8 did.

**What is gained:**
- no shared secret in the bucket, so bucket write access no longer implies peer impersonation;
- response authentication, which D8 lacked;
- certificate rotation handled by the mesh.

### 3.2 D9 in the new app

| Piece | celld (production lines) | New app |
|---|--:|---|
| **Deployment record** (`protocol.rs`) | 137 | `{version, component_sha256, manifest, grants, required_features}`, written once under a content-addressed prefix, with a CAS-moved pointer. Code trust per doc 01 §3.2.1. |
| **Deployment loading** (`fleet.rs` part) | 216 | Load the manifest and component bytes by hash, and verify the digest. Compilation and caching follow doc 01's code-trust modes. |
| **Generation** (`generation.rs`) | 272 | `{version, compiled component, manifest, grants}`. No binding graph, asset resolvers, cron schedule or queue catalog. |
| **Swap pump** (`logic/lib.rs` part) | 184 | **Kept, as pure decisions:** stop resident cells at safe points and restart them on the new component. Stores on the old generation drain their in-flight calls. |
| **Runtime swap** (`runtime.rs`, `cell_runtime.rs`, `actor.rs`, `cell_host.rs` parts) | 158 | Folded into doc 01's per-cell store lifecycle: new stores instantiate the new component; old stores are dropped once idle |
| **Adoption trigger** (`main.rs` part) | 192 | Poll the pointer (as celld) **or** take a push from a coordinator watch, if one is chosen in the coordination doc. Keep a forced-swap deadline and a `reload` operator call. The `on-deploy` hook (doc 06) runs after adoption. |
| **Wake-format migration** (`wake_format.rs`) | 159 | **Not built.** There is no legacy format. Fleet initialization becomes writing a format marker, about 20–40 lines. |
| **Format gate** (`logic/format.rs`, types) | 19 | **Kept as a stub:** a format version in leases or node records, and a refusal to write a newer format than all live nodes read. Activated by the first format change. |

**Guarantees, unchanged:**
- In-flight calls finish on their generation. That's a store-level property in wasm: the store holds its component until it is dropped.
- A cell switches code only between events, at a safe point decided by the swap pump.

**Simplifications:**
- No multi-script deployment graph. One app is one component (plus dynamic cells' own code, doc 04).
- No per-binding loading branches.

---

## 4. Integration with the existing engine

| Area | Change |
|---|---|
| D4 fleet log | Its `SignedPeerTransport` becomes a plain transport over the mesh or overlay (the signing call sites go) |
| D5 routing / peer tunnel | Tunnel establishment needs no HMAC. The protocol-version header check moves here. |
| D2 ownership / handoff | Handoff and control routes are protected by the network layer only |
| E3 operator CLI | `celld diagnose`'s challenge-signed probe (`peer_probe.rs`, Ed25519) is separate from D8. Keep or drop it in the area E review. |
| E4 node | No fleet-secret bootstrap (`load_or_create` of `fleet/peer-auth.json`). The node's mesh identity is configured instead. |
| D9 | Simplified as in §3.2 |
| Deployment requirement | **mTLS mesh (preferred) or WireGuard/Tailscale overlay between nodes is mandatory** |

---

## 5. Impact on the codebase

### 5.1 Code impact summary (LOC)

Code lines, measured against the baseline report's line map; estimates are marked *(est.)*.

| | Production | Docs (non-blank) | Notes |
|---|--:|--:|---|
| **Not built: D8 peer authentication** | **−367** | −124 (`docs/security.md`, attributed to D8) | `peer_auth.rs` 350, `logic/peer.rs` 15, `main.rs` 2 |
| Not built: signing and verification plumbing at the call sites (`main.rs`, `actor.rs`, peer tunnel, fleet log, `fleet.rs`) | −50 to −150 *(est.)* | – | 33 lines call `peer_auth::` directly; the surrounding branches add more |
| Kept: peer protocol-version check, moved to D5 | +20 to +40 *(est.)* | – | |
| Added: mesh / overlay deployment guide | – | +40 to +80 *(est.)* | |
| **D8 net** | **≈ −377 to −497** | ≈ −0.04k to −0.08k | |
| **Replaced: D9 as in celld** | **−1,337** | – | generation 272, loading 216, adoption 192, swap pump 184, wake-format migration 159, protocol 137, runtime swap 158, format gate 19 |
| Added: deployment record and loading (component + manifest + grants) | +180 to +270 *(est.)* | – | |
| Added: generation and swap pump (pure decisions kept; runtime swap folded into the doc 01 store lifecycle) | +300 to +500 *(est.)* | – | |
| Added: adoption trigger (poll or watch), forced-swap deadline, reload | +100 to +150 *(est.)* | – | |
| Added: fleet format marker and format-gate stub | +40 to +60 *(est.)* | – | |
| **D9 net** | **≈ −357 to −717** (−1,337 + 620 to 980) | – | |
| **Total (D8 + D9)** | **≈ −0.73k to −1.21k** | | |

### 5.2 Effect on the combined estimate

The README's combined estimate previously **kept** D8 and D9, because area D wasn't reviewed. This doc moves them:
- D8 out (−367 measured, plus plumbing);
- D9 replaced (−1,337 measured, +620 to +980 new).

## 6. Risks and open questions

**Risks**
1. **No defence in depth inside the network** if the mesh is misconfigured. Mitigate with a startup check that refuses to serve peer routes without mTLS, or without an interface bound to the overlay.
2. **Swap correctness** is still subtle: never run a cell on two generations, and finish in-flight calls. Keep the swap pump in the pure core and cover it in the simulator, as celld does.

**Open questions**
- mTLS mesh or overlay as the **required** baseline? mTLS is recommended.
- Pointer poll versus coordinator watch for adoption? This depends on the coordinator decision in the coming coordination doc.
- Keep `celld diagnose`'s signed probe? This goes to the area E review.

## 7. Build order

1. Peer transport over mTLS (or overlay), with the protocol-version header check.
2. Deployment record, loading and generation for wasm components; the swap pump in the pure core.
3. The adoption trigger: start with polling, and switch to a coordinator watch if one is chosen.
4. The format marker and gate stub.

## 8. Next steps

- [ ] Choose the node identity mechanism: service mesh, SPIFFE/SPIRE, or overlay only.
- [ ] Specify the deployment record schema (`version`, `component_sha256`, `grants`, `required_features`, format version).
- [ ] Carry celld's swap-pump invariants into the new pure core, with simulator coverage.
- [ ] Continue the area D review: concurrency on wasm, SQLite durability via VFS capture, coordination and placement.
