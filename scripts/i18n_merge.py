import json, glob, re, sys
D = sys.argv[1]
exact, patterns, seen_p = {}, [], set()
HAN = re.compile(r'[㐀-鿿]')
for f in sorted(glob.glob(D + '/*.en.json')):
    src = json.load(open(f.replace('.en.json', '.json')))
    out = json.load(open(f))
    assert len(src) == len(out), f
    for s, o in zip(src, out):
        zh, en = s['zh'], o['en']
        assert zh == o['zh'], (f, zh)
        if HAN.search(en):
            continue
        if s['kind'] == 'pattern' and '{}' in zh:
            if zh not in seen_p:
                seen_p.add(zh); patterns.append([zh, en])
        else:
            exact[re.sub(r'\s+', ' ', zh).strip()] = en
extra = json.load(open(D + '/extra.json')) if glob.glob(D + '/extra.json') else {"exact": {}, "patterns": []}
exact.update(extra["exact"])
for zh, en in extra["patterns"]:
    if zh not in seen_p:
        seen_p.add(zh); patterns.append([zh, en])
json.dump({"exact": exact, "patterns": patterns}, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
print(len(exact), len(patterns))
