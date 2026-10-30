"""English interface: the pages are written in Chinese; when a user picks English (cookie tms_lang=en),
LanguageMiddleware translates the HTML (and JSON) on the way out, from core/data/i18n_en.json.

The dictionary is built from every Chinese string the code can show (templates, model labels and choices,
messages, to-do rules, OOG texts, the 48-state notes): "exact" strings, and "patterns" with {} where a value is
filled in at runtime ("{0}" style when English needs another order). Values captured by a pattern are translated
too. Text the dictionary does not cover (mostly data people typed in Chinese) is left as it is.
"""
import html
import json
import re
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "i18n_en.json"
COOKIE = "tms_lang"
HAN = re.compile(r"[㐀-鿿]")
FULL = str.maketrans({"，": ", ", "：": ": ", "（": " (", "）": ") ", "；": "; ", "、": ", ", "。": ". ", "“": '"', "”": '"',
                      "！": "! ", "？": "? ", "【": "[", "】": "]", "　": " "})
SEP = re.compile(r"(\s*(?:[·•|｜/：:，,、；;（）()\[\]【】「」“”!?！？。→←*\n]|\s-\s)\s*)")


@lru_cache(maxsize=1)
def _data():
    if not DATA.exists():
        return {}, []
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    exact = {_norm(k): v for k, v in raw.get("exact", {}).items()}
    pats = []
    for zh, en in raw.get("patterns", []):
        statics = [p for p in zh.split("{}") if p]
        han = max(statics, key=lambda p: len(HAN.findall(p)), default="")
        if not HAN.search("".join(statics)) or "{}{}" in zh.replace(" ", ""):
            continue  # nothing to anchor on, or two values side by side (ambiguous split)
        rx = "^" + r"(.+?)".join(re.escape(_norm(p)) for p in zh.split("{}")) + "$"
        pats.append((len("".join(statics)), han.strip(), re.compile(rx, re.S), en, len(HAN.findall("".join(statics)))))
    pats.sort(key=lambda x: -x[0])
    return exact, pats


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _fill(en, groups):
    if re.search(r"\{\d+\}", en):
        try:
            return en.format(*groups)
        except (IndexError, KeyError, ValueError):
            pass
    out = en
    for g in groups:
        out = out.replace("{}", g, 1)
    return out


@lru_cache(maxsize=50000)
def translate(text):
    """English for one piece of interface text (whitespace kept at both ends)."""
    if not text or not HAN.search(text):
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    core = _norm(text)
    out = _translate_core(core, 0)
    return lead + out + trail


def _translate_core(core, depth):
    exact, pats = _data()
    if core in exact:
        return exact[core]
    if depth < 3:
        han_total = len(HAN.findall(core))
        for _, key, rx, en, han_static in pats:
            if key and key.strip() not in core:
                continue
            if han_static < 4 and han_static * 2 < han_total:
                continue  # a short pattern explaining less than half of the Chinese: too loose
            m = rx.match(core)
            if m:
                return _fill(en, [_translate_core(_norm(g), depth + 1) if HAN.search(g) else g for g in m.groups()])
    if depth > 5:
        return core.translate(FULL)
    # split on punctuation / separators and translate the pieces
    parts = SEP.split(core)
    if len(parts) > 1:
        done = []
        for p in parts:
            if not p:
                continue
            if SEP.fullmatch(p):
                done.append(p.translate(FULL))
            elif HAN.search(p):
                done.append(_translate_core(_norm(p), depth + 1))
            else:
                done.append(p)
        return re.sub(r" {2,}", " ", "".join(done)).strip()
    return _words(core)


def _words(core):
    """Last resort: the longest runs of words (then runs of Chinese characters) that the dictionary knows."""
    exact, _ = _data()
    words = core.split(" ")
    out, i = [], 0
    while i < len(words):
        for j in range(len(words), i, -1):
            chunk = " ".join(words[i:j])
            if HAN.search(chunk) and chunk in exact:
                out.append(exact[chunk])
                i = j
                break
        else:
            w = words[i]
            if HAN.search(w):
                pieces = re.split(r"([\u3400-\u9fff]+)", w)
                w = "".join(_han_run(p) if HAN.search(p) else p for p in pieces)
            out.append(w)
            i += 1
    return " ".join(x for x in out if x).translate(FULL).strip()


def _han_run(run):
    """A run of Chinese characters: translated only when the dictionary covers all of it (whole run, or known
    words of two or more characters back to back). Otherwise it is left exactly as typed: people's own Chinese
    (names, notes) must not come out as single characters."""
    exact, _ = _data()
    if run in exact:
        return " " + exact[run] + " "
    out, i = [], 0
    while i < len(run):
        for j in range(len(run), i + 1, -1):
            if run[i:j] in exact:
                out.append(exact[run[i:j]])
                i = j
                break
        else:
            return run
    return " " + " ".join(out) + " "


# --------------------------------------------------------------------------
# HTML / JSON
# --------------------------------------------------------------------------
TOKEN = re.compile(r"(<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>|<textarea\b.*?</textarea>|<pre\b.*?</pre>|<[^>]+>)", re.S | re.I)
ATTRS = re.compile(r'\b(placeholder|title|aria-label|alt|data-confirm|label)="([^"]*)"')
BUTTON_VALUE = re.compile(r'(<input\b[^>]*type="(?:submit|button)"[^>]*\bvalue=")([^"]*)(")', re.I)


def _tag(tag):
    if not HAN.search(tag):
        return tag
    tag = ATTRS.sub(lambda m: f'{m.group(1)}="{html.escape(translate(html.unescape(m.group(2))))}"', tag)
    return BUTTON_VALUE.sub(lambda m: m.group(1) + html.escape(translate(html.unescape(m.group(2)))) + m.group(3), tag)


def translate_html(page):
    out = []
    for part in TOKEN.split(page):
        if not part:
            continue
        if part.startswith("<"):
            low = part[:10].lower()
            if low.startswith("<textarea"):  # translate the placeholder, never the text people typed
                end = part.index(">") + 1
                out.append(_tag(part[:end]) + part[end:])
            elif low.startswith(("<script", "<style", "<pre", "<!--")):
                out.append(part)
            else:
                out.append(_tag(part))
        elif HAN.search(part):
            out.append(html.escape(translate(html.unescape(part)), quote=False))
        else:
            out.append(part)
    return "".join(out).replace('<html lang="zh-CN">', '<html lang="en">', 1)


def translate_json(obj):
    if isinstance(obj, str):
        return translate(obj)
    if isinstance(obj, list):
        return [translate_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: translate_json(v) for k, v in obj.items()}
    return obj


def lang_of(request):
    return "en" if request.COOKIES.get(COOKIE) == "en" else "zh"


class LanguageMiddleware:
    """Activate English for Django's own texts and translate the response when the user chose English."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.utils import translation

        en = lang_of(request) == "en"
        if en:
            translation.activate("en")
            request.LANGUAGE_CODE = "en"
        response = self.get_response(request)
        if not en or getattr(response, "streaming", False):
            return response
        ctype = response.get("Content-Type", "")
        try:
            if ctype.startswith("text/html"):
                response.content = translate_html(response.content.decode(response.charset or "utf-8")).encode("utf-8")
            elif ctype.startswith("application/json"):
                response.content = json.dumps(translate_json(json.loads(response.content)), ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return response
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response
