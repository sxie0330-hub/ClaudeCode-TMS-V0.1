"""OOG first screen: what a load looks like on a given trailer, and what each state on the
route requires for it (permits, escorts, police, superload, survey, travel limits).

Sources
  * State rules: docs/state-regs/<state>.md, the company's 48-state notes (oversizeloadassistant.com,
    2026-10), structured into core/data/state_regs.json. Check each state's official rules before moving.
  * Trailers: TrailerType rows (deck height, deck length, tare), seeded from manufacturer spec sheets
    and edited by the team.

What it checks, per truck:
  * loaded height = cargo height + deck height; loaded width = max(cargo, deck width)
  * rear overhang = cargo length - deck length
  * gross weight = cargo on the truck + tractor + trailer tare (permits are about gross and axle weights,
    not cargo weight alone)
  * overweight and oversize permits are only for non-divisible loads: divisible cargo that is over is flagged
It does NOT check axle-group weights (needs axle spacing) or route clearances: those stay with the permit service.
"""
import json
import math
import re
from types import SimpleNamespace
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "state_regs.json"
TRACTOR_LENGTH_FT = Decimal(20)

STATES = {
    "AL": "Alabama", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "AK": "Alaska", "HI": "Hawaii",
}
_NAME_TO_CODE = {v.upper(): k for k, v in STATES.items()}


@lru_cache(maxsize=1)
def regs():
    """{"TX": {...}} from core/data/state_regs.json, plus "US": common limits used when no state is known."""
    out = {"US": GENERIC}
    if DATA.exists():
        out.update({r["state"]: r for r in json.loads(DATA.read_text(encoding="utf-8"))})
    return out


# Used when the route's states are unknown: 8'6" wide, 13'6" high, 80,000 lbs gross.
GENERIC = {"state": "US", "name": "未填州：按常见上限", "legal": {"width_in": 102, "height_in": 162, "gross_lbs": 80000},
           "overweight": {}, "escorts": [], "police": [], "superload": {}, "survey": {}, "travel": {}}


def state_from_text(text):
    """'318 Cooper Circle, Peachtree City, GA 30269' -> 'GA'; 'Houston, Texas' -> 'TX'; '' if unsure."""
    t = (text or "").upper()
    m = re.search(r"\b([A-Z]{2})[\s,]+\d{5}(?:-\d{4})?\b", t) or re.search(r",\s*([A-Z]{2})\s*$", t.strip())
    if m and m.group(1) in STATES:
        return m.group(1)
    for name, code in _NAME_TO_CODE.items():
        if re.search(rf"\b{name}\b", t):
            return code
    return ""


def route_states(lane):
    """Ordered states for a lane: origin, via, destination (deduplicated)."""
    out = []
    via = [s.strip().upper() for s in re.split(r"[,\s/;]+", lane.via_states or "") if s.strip()]
    for code in [lane.origin_state or state_from_text(lane.origin), *via, lane.destination_state or state_from_text(lane.destination)]:
        if code and code in STATES and code not in out:
            out.append(code)
    return out


def ft_in(v):
    if v in (None, ""):
        return "?"
    inches = int((Decimal(v) * 12).to_integral_value())
    return f"{inches // 12}'{inches % 12}\"" if inches % 12 else f"{inches // 12}'"


def in_ft(inches):
    return ft_in(Decimal(inches) / 12) if inches not in (None, "") else "?"


def truck_load(lane, cargo):
    """What one truck carries on this lane: biggest piece dimensions, heaviest truck weight.

    Pieces are spread evenly over the lane's trucks (ceil). Dimensions are the largest piece's,
    assuming pieces are not placed side by side."""
    trucks = max(lane.trucks or 1, 1)
    items = [c for c in cargo if c.qty]
    if not items:
        return None
    weight = Decimal(0)
    unknown_weight = False
    for c in items:
        per_truck = math.ceil(c.qty / trucks)
        if c.weight_lbs:
            weight += Decimal(c.weight_lbs) * per_truck
        else:
            unknown_weight = True
    def biggest(attr):
        vals = [getattr(c, attr) for c in items if getattr(c, attr)]
        return max(vals) if vals else None
    return {
        "length_ft": biggest("length_ft"), "width_ft": biggest("width_ft"), "height_ft": biggest("height_ft"),
        "weight_lbs": weight or None, "unknown_weight": unknown_weight,
        "divisible": any(c.divisible for c in items), "pieces": [c.description for c in items],
    }


def loaded(trailer, load):
    """Dimensions and gross of this load on this trailer (None where the data is missing)."""
    h, w, length = load["height_ft"], load["width_ft"], load["length_ft"]
    deck_h = trailer.deck_height_in
    deck_len = trailer.max_extended_length_ft or trailer.deck_length_ft
    tare = trailer.combo_tare_lbs
    overhang = max(Decimal(length) - Decimal(deck_len), Decimal(0)) if (length and deck_len) else None
    overall = None
    if trailer.trailer_length_ft:
        # Overall = trailer + about 20 ft of tractor, plus any rear overhang.
        overall = Decimal(trailer.trailer_length_ft) + TRACTOR_LENGTH_FT + (overhang or 0)
    return {
        "height_in": (Decimal(h) * 12 + Decimal(deck_h)) if (h and deck_h is not None) else None,
        "width_in": max(Decimal(w) * 12, Decimal(trailer.deck_width_in)) if w else Decimal(trailer.deck_width_in),
        "rear_overhang_ft": overhang,
        "fits_deck": (Decimal(length) <= Decimal(deck_len)) if (length and deck_len) else None,
        "overall_ft": overall,
        "gross_lbs": (Decimal(load["weight_lbs"]) + tare) if (load["weight_lbs"] and tare is not None) else None,
        "cargo_lbs": load["weight_lbs"], "tare_lbs": tare,
    }


def _value(dim, L, trailer):
    return {"width": L["width_in"], "height": L["height_in"], "weight": L["gross_lbs"],
            "rear_overhang": L["rear_overhang_ft"], "length": L["overall_ft"]}.get(dim)


def check_state(code, L, trailer, divisible=False):
    """Requirements in one state for this loaded truck. Returns a dict with Chinese lines."""
    r = regs().get(code)
    out = {"state": code, "name": STATES.get(code, GENERIC["name"] if code == "US" else code), "has_rules": bool(r), "permits": [], "escorts": [], "police": [],
           "superload": [], "survey": [], "travel": [], "notes": []}
    if not r:
        out["notes"].append("没有这个州的规定资料")
        return out
    out.update(agency=r.get("agency") or "", phone=r.get("phone") or "", validity=r.get("permit_validity") or "",
               frost=r.get("frost_laws") or "", collected=r.get("collected") or "")
    legal, ow = r.get("legal") or {}, r.get("overweight") or {}
    # 8'6" is legal on the National Network in every state (23 CFR 658); some state files list 8'0" for local roads.
    legal_w = max(legal.get("width_in") or 102, 102)
    if L["width_in"] and L["width_in"] > legal_w:
        out["permits"].append(f"超宽：{in_ft(L['width_in'])} > 法定 {in_ft(legal_w)}")
    if legal.get("height_in") and L["height_in"] and L["height_in"] > legal["height_in"]:
        out["permits"].append(f"超高：装车后 {in_ft(L['height_in'])} > 法定 {in_ft(legal['height_in'])}")
    if legal.get("rear_overhang_ft") is not None and L["rear_overhang_ft"] and L["rear_overhang_ft"] > Decimal(str(legal["rear_overhang_ft"])):
        out["permits"].append(f"超后悬：{ft_in(L['rear_overhang_ft'])} > 法定 {ft_in(legal['rear_overhang_ft'])}")
    if legal.get("gross_lbs") and L["gross_lbs"] and L["gross_lbs"] > legal["gross_lbs"]:
        out["permits"].append(f"超重：总重 {L['gross_lbs']:,.0f} lbs > 法定 {legal['gross_lbs']:,} lbs（还要按轴组核定）")
        if ow.get("gross_max_lbs") and L["gross_lbs"] > ow["gross_max_lbs"]:
            out["permits"].append(f"超出该州超重许可上限 {ow['gross_max_lbs']:,} lbs")
    elif legal.get("gross_lbs") and L["gross_lbs"] is None and L["cargo_lbs"] and L["cargo_lbs"] > legal["gross_lbs"]:
        out["permits"].append(f"超重：光货重 {L['cargo_lbs']:,.0f} lbs 已超过法定总重 {legal['gross_lbs']:,} lbs（车型缺自重，总重没算）")
    a = avg_axle(L, trailer)
    if a and L["gross_lbs"] and legal.get("gross_lbs") and L["gross_lbs"] > legal["gross_lbs"] and ow.get("tandem_axle_lbs"):
        limit = Decimal(ow["tandem_axle_lbs"]) / 2
        if a > limit:
            out["permits"].append(f"轴数可能不够：{trailer.combo_axles} 轴，除转向轴外平均每轴约 {a:,.0f} lbs > 该州超重许可串联轴每轴 {limit:,.0f}（估算，要按实际轴距核定）")
    if divisible and out["permits"]:
        out["permits"].append("货物可拆分：超尺寸 / 超重许可只发给不可拆分货物，要拆分或多车运输")
    for key, label in (("escorts", "护送车"), ("police", "警车")):
        best = {}
        for rule in r.get(key) or []:
            v = _value(rule.get("dimension"), L, trailer)
            over = rule.get("over")
            if v is None or over is None or Decimal(str(v)) <= Decimal(str(over)):
                continue
            dim = rule["dimension"]
            n = rule.get("escorts")
            if dim not in best or (n or 0) > (best[dim].get("escorts") or 0):
                best[dim] = rule
        for dim, rule in best.items():
            n = rule.get("escorts")
            where = {"front": "前", "rear": "后", "front+rear": "前后"}.get(rule.get("where") or "", "")
            what = {"width": "宽", "height": "高", "length": "长", "weight": "重", "rear_overhang": "后悬", "front_overhang": "前悬"}.get(dim, dim)
            unit = rule.get("unit") or ""
            thr = in_ft(rule["over"]) if unit == "in" else (f"{rule['over']:,} lbs" if unit == "lbs" else f"{rule['over']} ft")
            text = f"{what} > {thr}：{label} {n if n is not None else '?'} 辆{('（' + where + '）') if where else ''}"
            if rule.get("notes"):
                text += f"（{rule['notes']}）"
            out[key].append(text)
    for key, label in (("superload", "Superload"), ("survey", "路线勘察")):
        s = r.get(key) or {}
        hits = []
        if s.get("width_in") and L["width_in"] and L["width_in"] > s["width_in"]:
            hits.append(f"宽 > {in_ft(s['width_in'])}")
        if s.get("height_in") and L["height_in"] and L["height_in"] > s["height_in"]:
            hits.append(f"高 > {in_ft(s['height_in'])}")
        if s.get("length_ft") and L["overall_ft"] and L["overall_ft"] > Decimal(str(s["length_ft"])):
            hits.append(f"整车长 > {s['length_ft']} ft")
        if s.get("gross_lbs") and L["gross_lbs"] and L["gross_lbs"] > s["gross_lbs"]:
            hits.append(f"总重 > {s['gross_lbs']:,} lbs")
        if hits:
            out[key].append(f"{label}：" + "、".join(hits))
    t = r.get("travel") or {}
    if t.get("night_max_width_in") and L["width_in"] and L["width_in"] > t["night_max_width_in"]:
        out["travel"].append(f"超过夜间通行宽度 {in_ft(t['night_max_width_in'])}：只能白天走")
    if t.get("weekend_allowed") is False and out["permits"]:
        out["travel"].append("周末不能走")
    if t.get("notes"):
        out["travel"].append(t["notes"])
    return out


def permitted_per_axle(states):
    """Lowest permitted per-axle weight on the route, from each state's overweight tandem allowance (lbs)."""
    vals = [(regs().get(s, {}).get("overweight") or {}).get("tandem_axle_lbs") for s in states]
    vals = [v / 2 for v in vals if v]
    return Decimal(str(min(vals))) if vals else Decimal(20000)


STEER_AXLE_LBS = Decimal(12000)


def avg_axle(L, trailer):
    """Rough average on the non-steer axles: (gross - 12,000 steer) / (axles - 1). Not an axle-spacing calculation."""
    if not L["gross_lbs"] or not trailer.combo_axles or trailer.combo_axles < 2:
        return None
    return ((L["gross_lbs"] - STEER_AXLE_LBS) / (trailer.combo_axles - 1)).quantize(Decimal(1))


def suggest_trailer(load, states, trailers):
    """Pick the most ordinary trailer that works. Returns (trailer, reason).

    1. fully legal (height, gross, length, capacity)
    2. legal height, overweight permit, axles look enough (average per axle <= the route's permitted tandem / 2)
    3. axles look enough, lowest loaded height (overheight permit)
    4. nothing in the library carries it: the highest-rated unit, flagged for specialised equipment"""
    min_h = min((regs().get(s, {}).get("legal", {}).get("height_in") or 162 for s in states), default=162)
    min_gross = min((regs().get(s, {}).get("legal", {}).get("gross_lbs") or 80000 for s in states), default=80000)
    per_axle = permitted_per_axle(states)
    trailers = [t for t in trailers if t.active and not t.enclosed and t.complete]
    if not trailers or not load:
        return None, ""
    rows = []
    for t in trailers:
        L = loaded(t, load)
        a = avg_axle(L, t)
        rows.append(SimpleNamespace(
            t=t, L=L,
            h_ok=L["height_in"] is None or L["height_in"] <= min_h,
            gross_ok=L["gross_lbs"] is None or L["gross_lbs"] <= min_gross,
            cap_ok=not t.rated_capacity_lbs or not load["weight_lbs"] or load["weight_lbs"] <= t.rated_capacity_lbs,
            len_ok=L["fits_deck"] in (None, True),
            axle_ok=L["gross_lbs"] is None or (a is not None and a <= per_axle)))
    def first(cands, key=lambda r: r.t.sort):
        cands = sorted(cands, key=key)
        return cands[0].t if cands else None
    t = first([r for r in rows if r.h_ok and r.gross_ok and r.cap_ok and r.len_ok])
    if t:
        return t, "装车后高度和总重都合法的最普通车型"
    t = first([r for r in rows if r.h_ok and r.cap_ok and r.len_ok and r.axle_ok])
    if t:
        return t, "高度合法；总重超法定要办超重许可，按平均每轴估算轴数够"
    t = first([r for r in rows if r.cap_ok and r.len_ok and r.axle_ok], key=lambda r: (r.L["height_in"] or 0, r.t.sort))
    if t:
        return t, "放不进合法高度，选装车后最低、轴数够的车型（要办超高许可）"
    t = first([r for r in rows if r.cap_ok and r.axle_ok], key=lambda r: (r.L["rear_overhang_ft"] or 0, r.L["height_in"] or 0, r.t.sort))
    if t:
        return t, "货比车板长，选后悬最小、轴数够的车型"
    best = max(trailers, key=lambda t: (t.rated_capacity_lbs or 0, t.combo_axles or 0))
    return best, "车型库里没有能装下的车：要液压轴线车 / 桥式 / 专门设计的组合，这里按载重最大的车型估算"


# ---------------------------------------------------------------------------
# Company rule (2026-10): a 20' BESS of 90,000 lbs or more per truck
#   * route through California -> the team's 9-axle RGN (3+2+2+2)
#   * otherwise -> 8-axle 4+4, if its average axle weight passes every state on the route; else the 9-axle
# Both combinations need a permit even empty. Trailers are looked up by key in the library (active or not).
# ---------------------------------------------------------------------------
BESS_WORDS = re.compile(r"\bBESS\b|\bESS\b|battery|energy storage|megapack|储能|电池", re.I)
BESS_HEAVY_LBS = Decimal(90000)
BESS_MAX_LENGTH_FT = Decimal(21)  # 20' units (19'10" outside) with a little slack
NINE_AXLE, EIGHT_AXLE = "rgn_9axle_team", "axle8_4x4"


def is_heavy_bess(load):
    return bool(load and load["weight_lbs"] and load["weight_lbs"] >= BESS_HEAVY_LBS and load["length_ft"]
                and Decimal(load["length_ft"]) <= BESS_MAX_LENGTH_FT and any(BESS_WORDS.search(p or "") for p in load["pieces"]))


def heavy_bess_rule(load, states):
    """(trailer, reason, warnings) when the heavy-BESS rule applies, else (None, "", [])."""
    if not is_heavy_bess(load):
        return None, "", []
    from .models import TrailerType

    lib = {t.key: t for t in TrailerType.objects.filter(key__in=[NINE_AXLE, EIGHT_AXLE])}
    nine, eight = lib.get(NINE_AXLE), lib.get(EIGHT_AXLE)
    always = "这套组合空车也要 permit"
    if "CA" in states:
        if nine:
            return nine, "公司规则：20' BESS ≥ 90,000 lbs 且路线经过 CA，用 9 轴（3+2+2+2）", [always]
        return None, "", ["公司规则要用 9 轴（3+2+2+2），但车型库里没有这个车型"]
    if eight:
        L = loaded(eight, load)
        a = avg_axle(L, eight)
        if a is None:
            return eight, "公司规则：20' BESS ≥ 90,000 lbs、路线不经过 CA，用 4+4 共 8 轴", [
                always, f"{eight.name} 缺自重，每轴重量没法核对 DOT 规定：请在车型库补上车头和挂车自重"]
        limit = permitted_per_axle(states)
        if a <= limit:
            return eight, f"公司规则：20' BESS ≥ 90,000 lbs、路线不经过 CA，用 4+4 共 8 轴（平均每轴约 {a:,.0f} ≤ {limit:,.0f} lbs）", [always]
        if nine:
            return nine, f"公司规则：4+4 共 8 轴平均每轴约 {a:,.0f} lbs，超过路线上的许可每轴 {limit:,.0f}，改用 9 轴（3+2+2+2）", [always]
    if nine:
        return nine, "公司规则：车型库里没有 4+4 共 8 轴，用 9 轴（3+2+2+2）", [always]
    return None, "", []


def lane_cargo(lane, lanes, cargo):
    """Cargo on this lane; cargo with no lane belongs to the only lane of a one-lane lead."""
    return [c for c in cargo if c.lane_id == lane.pk] or ([c for c in cargo if not c.lane_id] if len(lanes) == 1 else [])


def needs_permit(res):
    return any(r["permits"] for r in res["rows"])


def screen_lead(lead, trailers=None):
    from .models import TrailerType
    trailers = list(TrailerType.objects.filter(active=True)) if trailers is None else trailers
    lanes = list(lead.lanes.all())
    cargo = list(lead.cargo.all())
    return [screen_lane(lane, lane_cargo(lane, lanes, cargo), trailers) for lane in lanes]


def screen_lane(lane, cargo, trailers):
    """Everything the lead page shows for one lane."""
    states = route_states(lane)
    load = truck_load(lane, cargo)
    res = {"lane": lane, "states": states, "load": load, "trailer": None, "reason": "", "loaded": None, "rows": [], "warnings": [],
           "permit": False, "hazmat": False}
    if not load:
        res["warnings"].append("这一段还没有货物")
        return res
    if not states:
        res["warnings"].append("看不出起点 / 终点在哪个州：先按常见上限（8'6\" 宽、13'6\" 高、80,000 lbs）判断，请在运输段里填写州代码")
        states = ["US"]
    for missing, label in (("height_ft", "高"), ("width_ft", "宽"), ("length_ft", "长")):
        if not load[missing]:
            res["warnings"].append(f"缺货物{label}，相关检查跳过")
    if load["unknown_weight"]:
        res["warnings"].append("有货物缺重量，总重偏低")
    trailer = lane.trailer if lane.trailer_id else None
    reason = "指定车型"
    if trailer is None:
        trailer, reason, notes = heavy_bess_rule(load, states)
        res["warnings"] += notes
    if trailer is None:
        trailer, reason = suggest_trailer(load, states, trailers)
    if trailer is None:
        res["warnings"].append("车型库里没有可用车型")
        return res
    if not trailer.complete:
        missing = [label for label, v in (("板高", trailer.deck_height_in), ("可装长度", trailer.deck_length_ft),
                                          ("自重", trailer.combo_tare_lbs)) if v is None]
        res["warnings"].append(f"{trailer.name} 缺{'、'.join(missing)}，请在车型库补齐")
    L = loaded(trailer, load)
    res.update(trailer=trailer, reason=reason, loaded=L, rows=[check_state(s, L, trailer, load["divisible"]) for s in states])
    res["permit"] = needs_permit(res)
    res["hazmat"] = any(c.hazmat for c in cargo)
    if L["overall_ft"] is None and load["length_ft"]:
        res["warnings"].append(f"{trailer.name} 没有挂车总长，按长度的护送规定没算")
    if L["fits_deck"] is False:
        res["warnings"].append(f"货长 {ft_in(load['length_ft'])} 超出 {trailer.name} 可装长度，后悬 {ft_in(L['rear_overhang_ft'])}")
    return res
