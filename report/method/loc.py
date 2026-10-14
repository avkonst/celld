#!/usr/bin/env python3
"""Line classifier + feature-map aggregator for the celld LOC report.

Per line: blank / comment / code (code wins if a line has both).
Rust: items under #[cfg(celld_internal_tests)] / #[cfg(all(test, celld_internal_tests))]
(and doc comments / attributes directly above them) are tagged test.
JS: `if (typeof __test_X === "function") {...}` blocks and `__test_` lines tagged test.

Spec file lines:  <path> <range|*> <feature> [class]
  range = a-b (1-based inclusive) or * ; later rules override earlier ones for
  the lines they cover, so a file can be given a default '*' then sub-ranges.
  class in {prod, tool, dev, build, doc}; default prod. test is derived.
"""
import json, re, subprocess, sys, os
from collections import defaultdict

ROOT = '/home/andrey/projects/celld'

def lex_lines(text, lang):
    """Return list of (has_code, has_comment) per line, and a token-level mask
    used by the extent finder: list of per-char 'c' code / 'x' comment / 's' string."""
    n = len(text)
    kinds = bytearray(b' ' * n)  # 'c' code, 'm' comment, 's' string, ' ' whitespace
    i = 0
    prev_sig = ''  # last significant code char (for JS regex heuristic)
    prev_word = ''
    tmpl_stack = []  # JS template literal brace depth stack
    brace_depth = 0
    def mark(a, b, k):
        for j in range(a, b):
            if text[j] not in ' \t\r\n':
                kinds[j] = k
    hash_comment = lang in ('py', 'toml', 'yaml', 'sh', 'docker', 'text', 'gitignore')
    while i < n:
        ch = text[i]
        if lang in ('rs', 'js', 'jsonc'):
            if text.startswith('//', i):
                j = text.find('\n', i); j = n if j < 0 else j
                mark(i, j, ord('m')); i = j; continue
            if text.startswith('/*', i):
                depth = 1; j = i + 2
                while j < n and depth:
                    if lang == 'rs' and text.startswith('/*', j): depth += 1; j += 2; continue
                    if text.startswith('*/', j): depth -= 1; j += 2; continue
                    j += 1
                mark(i, j, ord('m')); i = j; continue
        if lang == 'html' and text.startswith('<!--', i):
            j = text.find('-->', i); j = n if j < 0 else j + 3
            mark(i, j, ord('m')); i = j; continue
        if hash_comment and ch == '#':
            j = text.find('\n', i); j = n if j < 0 else j
            mark(i, j, ord('m')); i = j; continue
        if lang == 'rs':
            m = re.compile(r'b?r(#*)"').match(text, i)
            if m and (i == 0 or not (text[i-1].isalnum() or text[i-1] == '_')):
                close = '"' + m.group(1)
                j = text.find(close, m.end()); j = n if j < 0 else j + len(close)
                mark(i, j, ord('s')); i = j; prev_sig = 'a'; continue
            if ch == '"' or (ch == 'b' and text.startswith('b"', i) and (i == 0 or not (text[i-1].isalnum() or text[i-1]=='_'))):
                j = i + (2 if ch == 'b' else 1)
                while j < n and text[j] != '"':
                    j += 2 if text[j] == '\\' else 1
                j += 1; mark(i, j, ord('s')); i = j; prev_sig = 'a'; continue
            if ch == "'":
                m = re.compile(r"'(\\(x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|.)|[^\\'\n])'").match(text, i)
                if m:
                    mark(i, m.end(), ord('s')); i = m.end(); continue
                kinds[i] = ord('c'); i += 1; continue  # lifetime
        if lang in ('js', 'jsonc'):
            if ch in '"\'':
                j = i + 1
                while j < n and text[j] != ch and text[j] != '\n':
                    j += 2 if text[j] == '\\' else 1
                j += 1; mark(i, j, ord('s')); i = j; prev_sig = 'a'; continue
            if ch == '`' or (ch == '}' and tmpl_stack and tmpl_stack[-1] == brace_depth):
                if ch == '}':
                    tmpl_stack.pop()
                j = i + 1
                while j < n:
                    if text[j] == '\\': j += 2; continue
                    if text[j] == '`': j += 1; break
                    if text.startswith('${', j):
                        tmpl_stack.append(brace_depth); j += 2; break
                    j += 1
                mark(i, j, ord('s')); i = j; prev_sig = 'a'; continue
            if ch == '/':
                # regex literal if previous significant token can't end an expression
                if prev_sig == '' or prev_sig in '(,=:[!&|?{};+-*%<>~^' or prev_word in ('return', 'typeof', 'case', 'in', 'of', 'delete', 'void', 'throw', 'new', 'do', 'else', 'yield', 'await'):
                    j = i + 1; in_class = False
                    while j < n and text[j] != '\n':
                        c = text[j]
                        if c == '\\': j += 2; continue
                        if c == '[': in_class = True
                        elif c == ']': in_class = False
                        elif c == '/' and not in_class: break
                        j += 1
                    if j < n and text[j] == '/':
                        j += 1
                        while j < n and text[j].isalpha(): j += 1
                        mark(i, j, ord('s')); i = j; prev_sig = 'a'; prev_word = ''; continue
            if ch == '{': brace_depth += 1
            elif ch == '}': brace_depth -= 1
        if lang == 'py':
            if text.startswith('"""', i) or text.startswith("'''", i):
                q = text[i:i+3]; j = text.find(q, i + 3); j = n if j < 0 else j + 3
                mark(i, j, ord('s')); i = j; continue
            if ch in '"\'':
                j = i + 1
                while j < n and text[j] != ch and text[j] != '\n':
                    j += 2 if text[j] == '\\' else 1
                j += 1; mark(i, j, ord('s')); i = j; continue
        if ch not in ' \t\r\n':
            kinds[i] = ord('c')
            if ch.isalnum() or ch in '_$':
                m = re.compile(r'[A-Za-z0-9_$]+').match(text, i)
                prev_word = m.group(0); prev_sig = 'a'
                for j in range(i, m.end()): kinds[j] = ord('c')
                i = m.end(); continue
            prev_sig = ch; prev_word = ''
        i += 1
    lines = []
    pos = 0
    for line in text.split('\n'):
        seg = kinds[pos:pos+len(line)]
        has_code = (b'c' in seg) or (b's' in seg)
        has_com = b'm' in seg
        lines.append((has_code, has_com))
        pos += len(line) + 1
    if text.endswith('\n'):
        lines.pop()
    return lines, kinds

TEST_ATTR = re.compile(r'^\s*#\[cfg\((celld_internal_tests|all\(test, celld_internal_tests\)|test)\)\]')

def rust_test_lines(text, kinds):
    """Return set of 1-based line numbers inside test-gated items."""
    lines = text.split('\n')
    starts = []
    off = 0
    for ln in lines:
        starts.append(off); off += len(ln) + 1
    def line_of(p):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= p: lo = mid
            else: hi = mid - 1
        return lo + 1
    out = set()
    for idx, ln in enumerate(lines):
        if not TEST_ATTR.match(ln):
            continue
        p = starts[idx] + ln.index('#')
        # skip the attribute itself
        depth = 0; j = p; n = len(text)
        # Header phase: track (), [], generic <> on code chars; a depth-0 `;`
        # or `,` ends the item; a depth-0 `{` starts the body, which is then
        # brace-matched alone (comparisons inside bodies must not count).
        end = None; body = 0; in_body = False
        # What kind of thing is gated? Skip this and any following attributes.
        q = p
        while True:
            m = re.compile(r'\s*#!?\[').match(text, q)
            if not m: break
            d = 0; q = m.end() - 1
            while q < n:
                if kinds[q] == ord('c'):
                    if text[q] == '[': d += 1
                    elif text[q] == ']':
                        d -= 1
                        if d == 0: q += 1; break
                q += 1
        head = re.compile(r'\s*(?:(?://[^\n]*|/\*.*?\*/)\s*)*').match(text, q).end()
        item_like = re.compile(r'(pub(\([^)]*\))?\s+)?((async|const|unsafe|extern\s+"C")\s+)*(fn|struct|enum|impl|mod|trait|union|macro_rules!|thread_local!)\b').match(text, head) is not None
        def next_sig(k):
            k += 1
            while k < n and kinds[k] != ord('c') and kinds[k] != ord('s'): k += 1
            return k
        while j < n:
            if kinds[j] == ord('c'):
                c = text[j]
                prev = text[j-1] if j else ''
                if in_body:
                    if c == '{': body += 1
                    elif c == '}':
                        body -= 1
                        if body == 0:
                            if item_like: end = j; break
                            k = next_sig(j)
                            nxt = text[k] if k < n else ''
                            word = re.compile(r'[A-Za-z_]+').match(text, k)
                            word = word.group(0) if word else ''
                            if nxt in ';,': end = k; break
                            if word in ('else', 'as') or nxt in '=.?([|&+-*/<>!':
                                in_body = False; j += 1; continue
                            end = j; break
                elif c == '{' and depth == 0:
                    in_body = True; body = 1
                elif c in '([{':
                    depth += 1
                elif c == '<' and (prev.isalnum() or prev in '_:'):
                    depth += 1
                elif c == '>' and prev not in '-=' and depth > 0 and text[j-1] != ' ':
                    depth -= 1
                elif c in ')]}':
                    depth -= 1
                    if depth < 0:
                        end = j - 1; break
                elif c in ';,' and depth == 0:
                    end = j; break
            j += 1
        if end is None: end = n - 1
        a = idx + 1; b = line_of(end)
        # extend backward over attached comments/attributes
        k = idx - 1
        while k >= 0:
            s = lines[k].strip()
            if s.startswith('//') or s.startswith('#[') or (s.startswith('#![') is False and s.endswith(']') and s.startswith('#')):
                k -= 1; continue
            break
        a = k + 2
        out.update(range(a, b + 1))
    return out

JS_TEST_IF = re.compile(r'^\s*if \(typeof __test_\w+ !?===? "function"\)')

def js_test_lines(text, kinds):
    lines = text.split('\n')
    out = set()
    starts = []; off = 0
    for ln in lines:
        starts.append(off); off += len(ln) + 1
    for idx, ln in enumerate(lines):
        if '__test_' not in ln:
            continue
        out.add(idx + 1)
        if JS_TEST_IF.match(ln) and ln.rstrip().endswith('{'):
            depth = 0; j = starts[idx]
            while j < len(text):
                if kinds[j] == ord('c'):
                    if text[j] == '{': depth += 1
                    elif text[j] == '}':
                        depth -= 1
                        if depth == 0: break
                j += 1
            e = text.count('\n', 0, j) + 1
            out.update(range(idx + 1, e + 1))
    return out

def lang_of(path):
    b = os.path.basename(path)
    ext = os.path.splitext(b)[1]
    return {'.rs': 'rs', '.js': 'js', '.mjs': 'js', '.jsonc': 'jsonc', '.json': 'jsonc', '.py': 'py',
            '.toml': 'toml', '.yml': 'yaml', '.yaml': 'yaml', '.html': 'html', '.md': 'md',
            '.svg': 'svg', '.sh': 'sh'}.get(ext) or {'Dockerfile': 'docker', '.gitignore': 'gitignore',
            '_headers': 'text', '_redirects': 'text'}.get(b, 'text')

def analyze(path):
    text = open(os.path.join(ROOT, path), encoding='utf-8', errors='replace').read()
    lang = lang_of(path)
    if lang in ('md', 'svg'):
        lines = [(bool(l.strip()), False) for l in text.split('\n')]
        if text.endswith('\n'): lines.pop()
        return lang, lines, set()
    lines, kinds = lex_lines(text, lang)
    tests = set()
    if lang == 'rs':
        tests = rust_test_lines(text, kinds)
    elif lang == 'js':
        tests = js_test_lines(text, kinds)
    return lang, lines, tests

def load_spec(spec_path):
    rules = []
    for raw in open(spec_path):
        s = raw.split('#', 1)[0].strip() if not raw.lstrip().startswith('#') else ''
        if not s: continue
        parts = s.split()
        path, rng, feat = parts[0], parts[1], parts[2]
        cls = parts[3] if len(parts) > 3 else 'prod'
        rules.append((path, rng, feat, cls))
    return rules

def main():
    spec = sys.argv[1]
    files = subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).split()
    rules = load_spec(spec)
    if '--partial' in sys.argv:
        wanted = {r[0] for r in rules}
        files = [f for f in files if f in wanted]
    by_file = defaultdict(list)
    for r in rules: by_file[r[0]].append(r)
    unknown = [p for p in by_file if p not in files]
    if unknown:
        print('SPEC PATHS NOT IN REPO:', unknown, file=sys.stderr); sys.exit(2)
    rows = []  # (path, line, feature, class, kind)
    problems = []
    for path in files:
        if path not in by_file:
            problems.append(f'UNMAPPED FILE {path}'); continue
        lang, lines, tests = analyze(path)
        nl = len(lines)
        assign = [None] * (nl + 1)
        explicit = [0] * (nl + 1)
        for (_, rng, feat, cls) in by_file[path]:
            if rng == '*': a, b = 1, nl
            else:
                a, b = map(int, rng.split('-'))
                if a < 1 or b > nl or a > b:
                    problems.append(f'BAD RANGE {path} {rng} (file has {nl})'); continue
            for k in range(a, b + 1):
                if rng != '*':
                    if explicit[k]:
                        problems.append(f'OVERLAP {path}:{k} ({assign[k]} vs {feat})')
                    explicit[k] = 1
                assign[k] = (feat, cls)
        for k in range(1, nl + 1):
            if assign[k] is None:
                problems.append(f'GAP {path}:{k}'); continue
            feat, cls = assign[k]
            hc, hm = lines[k - 1]
            kind = 'code' if hc else ('comment' if hm else 'blank')
            if k in tests and cls == 'prod': cls = 'test'
            elif k in tests: cls = 'test'
            rows.append((path, k, feat, cls, kind, lang))
    # compress problems
    if problems:
        gaps = [p for p in problems if p.startswith('GAP')]
        other = [p for p in problems if not p.startswith('GAP')]
        for p in other[:50]: print(p, file=sys.stderr)
        if gaps: print(f'{len(gaps)} GAP lines, first: {gaps[:5]}', file=sys.stderr)
    if sys.argv[2] != '-':
        json.dump(rows, open(sys.argv[2], 'w'))
    if '--partial' in sys.argv:
        agg = defaultdict(lambda: [0, 0, 0, 0])
        for (_, _, feat, cls, kind, _) in rows:
            a = agg[(feat, cls)]
            a[{'code': 0, 'comment': 1, 'blank': 2}[kind]] += 1; a[3] += 1
        for (feat, cls), a in sorted(agg.items()):
            print(f'{feat:16} {cls:6} code={a[0]:6} comment={a[1]:6} blank={a[2]:5} total={a[3]:6}')
    print(f'{len(rows)} lines classified; problems={len(problems)}')

if __name__ == '__main__':
    main()
