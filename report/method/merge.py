import json,sys
rows=json.load(open(sys.argv[1]))
ov=[l.split() for l in open(sys.argv[2]) if l.strip() and not l.startswith('#')]
n=0
for r in rows:
    for p,a,b in ov:
        if r[0]==p and int(a)<=r[1]<=int(b) and r[3]=='prod':
            r[3]='testu'; n+=1
json.dump(rows,open(sys.argv[3],'w'))
print('reclassified',n,'lines to testu')
