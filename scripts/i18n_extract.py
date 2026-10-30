"""Collect every user-visible Chinese string in the TMS: exact strings and patterns with placeholders."""
import ast, glob, json, re, sys
ROOT = sys.argv[1]
CJK = re.compile(r'[㐀-鿿！-～　-〿]')
HAN = re.compile(r'[㐀-鿿]')
items = {}

def add(zh, kind, src):
    zh = re.sub(r'\s+', ' ', zh).strip()
    if not HAN.search(zh) or len(zh) > 400:
        return
    key = (zh, kind)
    if key not in items:
        items[key] = {"zh": zh, "kind": kind, "src": src}

# ---- templates
TAG = re.compile(r'(\{%.*?%\}|\{#.*?#\}|<script.*?</script>|<style.*?</style>|<[^>]+>)', re.S)
VAR = re.compile(r'\{\{.*?\}\}', re.S)
ATTR = re.compile(r'(?:placeholder|title|aria-label|value|alt|data-confirm|content)="([^"]*)"')
for f in glob.glob(ROOT + '/core/templates/**/*.html', recursive=True) + glob.glob(ROOT + '/templates/**/*.html', recursive=True):
    s = open(f, encoding='utf-8').read()
    name = f.split('templates/')[-1]
    for m in ATTR.finditer(s):
        v = m.group(1)
        if HAN.search(v) and '{%' not in v:
            if '{{' in v:
                add(VAR.sub('{}', v), 'pattern', name)
            else:
                add(v, 'exact', name)
    for part in TAG.split(s):
        if not part or part.startswith(('<', '{%', '{#')):
            continue
        if not HAN.search(part):
            continue
        if '{{' in part:
            add(VAR.sub('{}', part), 'pattern', name)
            for piece in VAR.split(part):   # also the pieces, for fallback splitting
                add(piece, 'exact', name)
        else:
            add(part, 'exact', name)
    # {% translate-free %} strings inside template tags, e.g. default:"未指定"
    for m in re.finditer(r'"([^"]*[㐀-鿿][^"]*)"', s):
        v = m.group(1)
        if '{{' not in v and '{%' not in v and '<' not in v:
            add(v, 'exact', name)

# ---- python
SKIP_ASSIGN = re.compile(r'PROMPT|SCHEMA|_RULES$|^RULES_TEXT|INSTRUCTIONS')
for f in sorted(glob.glob(ROOT + '/core/*.py')) + glob.glob(ROOT + '/core/templatetags/*.py'):
    if f.endswith(('tests.py',)):
        continue
    tree = ast.parse(open(f, encoding='utf-8').read())
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if any(SKIP_ASSIGN.search(n) for n in names):
                for sub in ast.walk(node.value):
                    skip.add(id(sub))
        if isinstance(node, ast.Call) and getattr(node.func, 'attr', '') in ('info', 'warning', 'exception', 'error', 'debug') \
                and getattr(getattr(node.func, 'value', None), 'id', '') in ('log', 'logger'):
            for sub in ast.walk(node):
                skip.add(id(sub))
    inside_f = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                inside_f.add(id(v))
    base = f.split('/')[-1]
    if base == 'ai.py' or base == 'ai_leads.py':
        # prompts and schemas never render; keep only short UI messages
        pass
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    parts.append('{}')
            zh = ''.join(parts)
            if HAN.search(zh):
                add(zh, 'pattern' if '{}' in zh else 'exact', base)
                for piece in zh.split('{}'):
                    add(piece, 'exact', base)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inside_f:
            v = node.value
            if HAN.search(v):
                if '%(' in v or '{' in v and '}' in v and base not in ('ai.py',):
                    add(re.sub(r'\{[^{}]*\}', '{}', v), 'pattern' if '{' in v else 'exact', base)
                for line in v.split('\n'):
                    add(line, 'exact', base)

# ---- JS
for f in glob.glob(ROOT + '/core/static/core/*.js'):
    for m in re.finditer(r'["\'`]([^"\'`\n]*[㐀-鿿][^"\'`\n]*)["\'`]', open(f, encoding='utf-8').read()):
        v = m.group(1)
        add(re.sub(r'\$\{[^}]*\}', '{}', v), 'pattern' if '${' in v else 'exact', f.split('/')[-1])

# ---- state rules (OOG notes shown on the lead page)
regs = json.load(open(ROOT + '/core/data/state_regs.json', encoding='utf-8'))
def walk(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str) and k in ('notes', 'frost_laws', 'permit_validity', 'divisible_rule', 'other_notes'):
                add(v, 'exact', 'state_regs')
            else:
                walk(v)
    elif isinstance(o, list):
        for x in o:
            walk(x)
walk(regs)
for t in json.load(open(ROOT + '/core/data/trailer_specs.json', encoding='utf-8')):
    pass

out = sorted(items.values(), key=lambda x: (x['src'] == 'state_regs', x['kind'], x['zh']))
json.dump(out, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
from collections import Counter
print(len(out), Counter(x['kind'] for x in out), Counter(x['src'] == 'state_regs' for x in out))
print(sum(len(x['zh']) for x in out if x['src']!='state_regs'), sum(len(x['zh']) for x in out if x['src']=='state_regs'))
