#!/usr/bin/env python3
"""Aggregate classified rows (from loc.py) into report tables (markdown + JSON)."""
import json, sys, collections

rows = json.load(open(sys.argv[1]))
out_json = sys.argv[2]

NAMES = {
 'A': 'Workers runtime (V8 engine)', 'B': 'Runtime APIs (Web platform & Node)',
 'C': 'Cloudflare services (bindings)', 'D': 'Distributed cell engine (fleet)',
 'E': 'Operations & tooling', 'Z': 'Cross-cutting (build, general docs, legal)',
 'A1.engine': 'V8 isolate engine core', 'A2.modules': 'Module system & bootstrap',
 'A3.host': 'Cell runtime host & isolate pool', 'A4.rpc': 'Service bindings & JS RPC',
 'A5.wasm': 'WebAssembly & workers-rs (Rust)', 'A6.python': 'Python Workers (Pyodide)',
 'B1.fetch': 'Fetch / Request / Response / bodies', 'B2.streams': 'Streams & encoding',
 'B3.webstd': 'Other web standards (URL, events, …)', 'B4.crypto': 'Web Crypto & node:crypto',
 'B5.node': 'Node.js compatibility', 'B6.websocket': 'WebSockets (incl. hibernation)',
 'B7.htmlrewriter': 'HTMLRewriter', 'B8.sockets': 'TCP sockets', 'B9.cache': 'Cache API',
 'C1.do': 'Durable Objects (storage, SQL, gates)', 'C2.alarms': 'Alarms & wake discovery',
 'C3.facets': 'Durable Object Facets', 'C4.kv': 'Workers KV', 'C5.queues': 'Queues',
 'C6.d1': 'D1', 'C7.r2': 'R2', 'C8.workflows': 'Workflows', 'C9.cron': 'Cron Triggers',
 'C10.assets': 'Static assets', 'C11.dynamic': 'Dynamic Workers & Tail Workers',
 'C12.containers': 'Containers & Sandbox',
 'D1.core': 'Decision core & cell lifecycle', 'D2.ownership': 'Ownership leases & fencing',
 'D3.replication': 'SQLite replication & restore (LTX)', 'D4.fleetlog': 'Fleet durability ensemble (node log)',
 'D5.routing': 'Routing, ingress & peer transport', 'D6.placement': 'Placement, pressure & drain',
 'D7.objstore': 'Object-store backends (S3/GCS/Azure)', 'D8.security': 'Peer authentication',
 'D9.deployment': 'Deployment adoption & generations',
 'E1.deploy': '`celld deploy`', 'E2.dev': '`celld dev`', 'E3.opscli': 'Operator CLI & diagnose',
 'E4.node': 'Node process, CLI & operator API', 'E5.telemetry': 'Telemetry',
 'E6.managed': 'Managed control plane (celld.dev)',
 'Z.build': 'Build, CI & lint config', 'Z.general': 'General docs', 'Z.legal': 'Licenses/NOTICE',
 'Z.lock': 'Cargo.lock (generated)',
}
ORDER = list(NAMES)
def l1(f): return f.split('.')[0][0]
def key(f): return ORDER.index(f) if f in ORDER else 999

# Per-feature, per-class counters
C = collections.defaultdict(lambda: collections.Counter())
lang_class = collections.defaultdict(collections.Counter)
crate = collections.defaultdict(collections.Counter)
unknown = set()
for path, ln, feat, cls, kind, lang in rows:
    if feat not in NAMES: unknown.add(feat)
    c = C[feat]
    c[(cls, kind)] += 1
    if kind == 'code':
        lang_class[lang][cls] += 1
        top = '/'.join(path.split('/')[:2]) if path.startswith('crates/') else path.split('/')[0]
        crate[top][cls] += 1
if unknown:
    print('UNKNOWN FEATURES', unknown); sys.exit(1)

def g(f, cls, kind='code'):
    return C[f][(cls, kind)]

result = {'features': {}, 'lang_class': lang_class, 'crate': crate}
for f in sorted(C, key=key):
    result['features'][f] = {f'{cls}.{kind}': n for (cls, kind), n in C[f].items()}
json.dump(result, open(out_json, 'w'), indent=1, default=dict)

# Console summary
tot = collections.Counter()
for f in C:
    for (cls, kind), n in C[f].items(): tot[(cls, kind)] += n
print('TOTALS by class (code/comment/blank):')
for cls in ['prod', 'tool', 'test', 'dev', 'build', 'doc', 'legal', 'generated']:
    print(f"  {cls:9} code={tot[(cls,'code')]:7} comment={tot[(cls,'comment')]:6} blank={tot[(cls,'blank')]:6}")
print()
print(f"{'feature':16} {'prod':>7} {'p.cmt':>6} {'tool':>6} {'test':>6} {'ex':>5} {'doc':>5}")
for f in sorted(C, key=key):
    print(f"{f:16} {g(f,'prod'):7} {g(f,'prod','comment'):6} {g(f,'tool'):6} {g(f,'test'):6} {g(f,'dev'):5} {g(f,'doc'):5}")
print('\nLANG x CLASS (code lines):')
for lang, c in sorted(lang_class.items()):
    print(' ', lang, dict(c))
print('\nCRATE x CLASS (code lines):')
for k, c in sorted(crate.items()):
    print(' ', k, dict(c))
