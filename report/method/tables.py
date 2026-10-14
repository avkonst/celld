import json,collections,sys
sys.argv=['x','full.json','agg2.json']
exec(open('aggregate.py').read().split("# Per-feature, per-class counters")[0])  # NAMES, ORDER
rows=json.load(open('full.json'))
C=collections.defaultdict(collections.Counter)
for p,ln,f,cls,kind,lang in rows: C[f][(cls,kind)]+=1
def g(f,cls,kind='code'): return C[f][(cls,kind)]
def fmt(n): return f"{n:,}" if n else "–"
feats=[f for f in ORDER if '.' in f and not f.startswith('Z')]
out=[]
hdr="| Feature | Prod code | Prod comments | Tools code | Test code (gated + ungated) | Examples | Docs (non-blank) | **All code** |\n|---|--:|--:|--:|--:|--:|--:|--:|"
tot=collections.Counter()
for L in 'ABCDE':
    fs=[f for f in feats if f[0]==L]
    sub=collections.Counter()
    out.append(f"\n#### {L} — {NAMES[L]}\n\n"+hdr)
    for f in fs:
        v=dict(prod=g(f,'prod'),pc=g(f,'prod','comment'),tool=g(f,'tool'),test=g(f,'test')+g(f,'testu'),testu=g(f,'testu'),ex=g(f,'dev'),doc=g(f,'doc'))
        allc=v['prod']+v['tool']+v['test']+v['ex']
        for k in v: sub[k]+=v[k]
        sub['all']+=allc
        t=fmt(v['test']) + (f" ({v['testu']} ungated)" if v['testu'] else "")
        out.append(f"| {f} {NAMES[f]} | {fmt(v['prod'])} | {fmt(v['pc'])} | {fmt(v['tool'])} | {t} | {fmt(v['ex'])} | {fmt(v['doc'])} | {fmt(allc)} |")
    t=fmt(sub['test'])+(f" ({sub['testu']} ungated)" if sub['testu'] else "")
    out.append(f"| **{L} subtotal** | **{fmt(sub['prod'])}** | **{fmt(sub['pc'])}** | **{fmt(sub['tool'])}** | **{t}** | **{fmt(sub['ex'])}** | **{fmt(sub['doc'])}** | **{fmt(sub['all'])}** |")
    for k in sub: tot[k]+=sub[k]
out.append(f"\nAcross A–E: prod {tot['prod']:,} · tools {tot['tool']:,} · test {tot['test']:,} ({tot['testu']} ungated) · examples {tot['ex']:,} · feature docs {tot['doc']:,} · all code {tot['all']:,}")
# L1 summary
out.append("\n\nL1 SUMMARY")
out.append("| Area | Prod code | Share of prod | Tools | Test | Examples |\n|---|--:|--:|--:|--:|--:|")
P=sum(g(f,'prod') for f in feats)
for L in 'ABCDE':
    fs=[f for f in feats if f[0]==L]
    pr=sum(g(f,'prod') for f in fs)
    out.append(f"| {L} {NAMES[L]} | {pr:,} | {100*pr/P:.1f}% | {sum(g(f,'tool') for f in fs):,} | {sum(g(f,'test')+g(f,'testu') for f in fs):,} | {sum(g(f,'dev') for f in fs):,} |")
# grand class totals
out.append("\n\nCLASS TOTALS")
T=collections.Counter()
for f in C:
    for (cls,kind),n in C[f].items(): T[(cls,kind)]+=n
for cls in ['prod','tool','test','testu','dev','build','doc','legal','generated']:
    out.append(f"{cls}: code {T[(cls,'code')]:,} comment {T[(cls,'comment')]:,} blank {T[(cls,'blank')]:,} total {T[(cls,'code')]+T[(cls,'comment')]+T[(cls,'blank')]:,}")
print('\n'.join(out))
