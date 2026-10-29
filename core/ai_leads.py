"""AI for inquiries: read a customer's inquiry into a Lead, and a carrier's reply into quotes.

Same Gemini setup as core/ai.py. Sizes and weights come back in their original
units and are converted to ft / lbs here (core/units.py). In this module the
model MAY read prices (carrier rates); everything still lands as a draft that a
person reviews, and nothing is ever sent.
"""
import json
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from . import ai, units
from .models import CargoLine, Carrier, CarrierQuote, CarrierQuoteLine, Charge, Company, Customer, Lead, LeadCargo, LeadLane, Shipment

_S = {"type": "STRING"}

_CARGO = {"type": "OBJECT", "properties": {
    "lane": {"type": "STRING", "description": "属于第几段运输（1、2…），不确定留空"},
    "description": {"type": "STRING", "description": "英文品名"},
    "qty": {"type": "STRING", "description": "件数，未写就 1"},
    "handling_type": {"type": "STRING", "description": "之一：" + ", ".join(CargoLine.HandlingType.values)},
    "weight": {"type": "STRING", "description": "重量照原文带单位，如 35,000 kg、20 t、77,161 lbs；没写留空"},
    "weight_is_each": {"type": "STRING", "description": "原文重量是每件的写 true，是合计的写 false"},
    "length": _S, "width": _S, "height": {"type": "STRING", "description": "尺寸照原文带单位，如 40'、13'6\"、12.19 m、480 in"},
    "hazmat": {"type": "STRING", "description": "危险品写 true"},
    "notes": _S},
    "required": ["lane", "description", "qty", "handling_type", "weight", "weight_is_each", "length", "width", "height", "hazmat", "notes"]}

LEAD_SCHEMA = {"type": "OBJECT", "properties": {
    "title": {"type": "STRING", "description": "简短的询价名称，如 “Transformer Houston → Tomball” 或 “#4560 20FR x3 LA → Moreno Valley”"},
    "company": {"type": "STRING", "description": "我方公司 ATS 或 PTT；看不出留空"},
    "customer": {"type": "STRING", "description": "询价的客户公司（绝不能是 ATS 或 PTT）；已有客户请用完全相同的名字"},
    "customer_ref": {"type": "STRING", "description": "客户的项目号 / 参考号"},
    "contact_name": _S, "contact_email": _S, "contact_phone": {"type": "STRING", "description": "电话或微信号"},
    "quote_due": {"type": "STRING", "description": "客户要求的报价截止日 YYYY-MM-DD，没写留空"},
    "summary": {"type": "STRING", "description": "中文，两三句概括客户要做什么"},
    "questions": {"type": "ARRAY", "items": _S, "description": "中文：报价还缺哪些信息需要问客户（如具体地址、尺寸重量、装卸方式、日期、是否需要保险）。一条一个问题"},
    "lanes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "origin": {"type": "STRING", "description": "起点：名称 + 地址，或港口 / 城市, 州"},
        "origin_zip": _S,
        "destination": _S, "destination_zip": _S,
        "service": {"type": "STRING", "description": "之一：" + ", ".join(Shipment.Service.values)},
        "equipment": {"type": "STRING", "description": "车型 / 柜型，如 Flatbed、Step Deck、RGN、53' Van、40HC；没写留空"},
        "trucks": {"type": "STRING", "description": "车次 / 柜数，没写就 1"},
        "ready_date": {"type": "STRING", "description": "YYYY-MM-DD"},
        "deliver_by": {"type": "STRING", "description": "YYYY-MM-DD"},
        "notes": {"type": "STRING", "description": "英文：许可证、护送、吊装、预约、保险等要求"}},
        "required": ["origin", "origin_zip", "destination", "destination_zip", "service", "equipment", "trucks", "ready_date", "deliver_by", "notes"]}},
    "cargo": {"type": "ARRAY", "items": _CARGO}},
    "required": ["title", "company", "customer", "customer_ref", "contact_name", "contact_email", "contact_phone", "quote_due",
                 "summary", "questions", "lanes", "cargo"]}

LEAD_PROMPT = """你是一家美国物流公司（ATS / PTT，做 OOG 大件、重载、BESS 储能柜、项目货、拖柜和转运）的询报价助手。
用户会粘贴客户的询价（邮件、微信聊天记录，可能附 PDF / 图片 / 表格），你把它整理成一条询价记录，输出 JSON。

规则：
1. 只用内容里明确写出的信息，不要编造；没写的留空，并把报价需要但缺少的信息写进 questions。
2. 每一段运输（起点 → 终点）写一条 lanes；同一段要好几车就写 trucks，不要重复写多条。港口提柜送到工地、再从工地转运，就是两段。
3. 货物写进 cargo：重量和尺寸照原文抄数字和单位，不要自己换算，系统会统一换成 ft / lbs。“L×W×H” 拆成 length / width / height，各自带单位。
4. ATS 和 PTT 是我们自己，只能填 company，绝不能填成 customer。
5. 日期 YYYY-MM-DD；只写月/日的按今天推断年份。
6. summary 和 questions 用中文；lanes.notes 和 cargo.description 用英文（会直接用在给承运商的询价邮件里）。
7. 内容是承运商的报价回复而不是客户询价时，照样尽量整理，并在 summary 里说明。"""

REPLY_SCHEMA = {"type": "OBJECT", "properties": {
    "summary": {"type": "STRING", "description": "中文一两句：谁报了什么价"},
    "quotes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "carrier": {"type": "STRING", "description": "承运商公司名；已有承运商请用完全相同的名字"},
        "contact_email": _S,
        "lane": {"type": "STRING", "description": "报的是询价里的第几段（1、2…）；整个项目一口价或不确定留空"},
        "declined": {"type": "STRING", "description": "明确说不接 / 没车写 true"},
        "equipment": _S,
        "per_truck": {"type": "STRING", "description": "价格是每车的写 true；是这一段或整个项目的总价写 false"},
        "valid_until": {"type": "STRING", "description": "报价有效期 YYYY-MM-DD"},
        "transit": {"type": "STRING", "description": "时效，如 2 days"},
        "conditions": {"type": "STRING", "description": "英文：条件、不含项、备注"},
        "lines": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "charge_type": {"type": "STRING", "description": "之一：" + ", ".join(f"{v}={l}" for v, l in Charge.Type.choices)},
            "description": {"type": "STRING", "description": "英文，照原文"},
            "quantity": {"type": "STRING", "description": "数量，没写就 1"},
            "unit_price": {"type": "STRING", "description": "单价，纯数字（美元），如 4850.00"},
            "is_estimate": {"type": "STRING", "description": "视情况才发生的费用（detention / layover / TONU 的单价、按小时的等待费）写 true"}},
            "required": ["charge_type", "description", "quantity", "unit_price", "is_estimate"]}}},
        "required": ["carrier", "contact_email", "lane", "declined", "equipment", "per_truck", "valid_until", "transit", "conditions", "lines"]}}},
    "required": ["summary", "quotes"]}

REPLY_PROMPT = """你是物流公司的询报价助手。用户会粘贴一家或几家承运商对我们询价的回复（邮件、微信、PDF 报价单、截图），
以及这条询价的需求。把每家承运商的报价整理成 JSON。

规则：
1. 这里**可以**读取金额：把运费、燃油、许可证、护送、吊装等逐项写进 lines，unit_price 只写数字（美元）。原文是别的货币时照数字写，并在 conditions 注明币种。
2. all-in 价格写一行 LINEHAUL，description 注明 all-in。燃油单独列出的写 FUEL。
3. Detention、Layover、TONU、按小时 / 按天的等待或仓储费：只是单价、不一定发生的，is_estimate 写 true。
4. 只用原文明确写的数字，不要计算、不要猜。看不清的不要写。
5. 每家承运商、每一段一条 quotes。说不接或没车的，declined 写 true，lines 留空。"""


class Draft(dict):
    """Cleaned AI result (dict) used to fill forms."""


def _date(v):
    try:
        return ai.parse_value("date", v) if v else None
    except ValueError:
        return None


def _int(v, default=1):
    try:
        return max(int(float(str(v).replace(",", ""))), 1)
    except (TypeError, ValueError):
        return default


def _money(v):
    try:
        d = Decimal(str(v).replace(",", "").replace("$", "").strip())
        return d.quantize(Decimal("0.01")) if d >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _context():
    return "已有资料：\n" + json.dumps({
        "customers": list(Customer.objects.values_list("name", flat=True)[:300]),
        "carriers": list(Carrier.objects.values_list("name", flat=True)[:300]),
    }, ensure_ascii=False)


def _run(text, docs, prompt, schema, context, caller):
    text = (text or "").strip()
    kept, skipped = ai.limit_docs([d for d in (docs or []) if d.get("data")])
    if not text and not kept:
        raise ai.AiError("请先粘贴内容或上传文件")
    if len(text) > ai.MAX_EMAIL_CHARS:
        raise ai.AiError(f"内容太长（{len(text)} 字），请只保留相关部分，{ai.MAX_EMAIL_CHARS} 字以内")
    body = (text or "（只有附件）") + ("\n\n（附上的文件：" + "、".join(d["name"] for d in kept) + "）" if kept else "")
    kwargs = {"prompt": prompt, "schema": schema}
    if kept:
        kwargs["attachments"] = kept
    result, _usage = (caller or ai.call_gemini)(body, context, timezone.localdate(), **kwargs)
    return result, [d["name"] for d in kept], skipped


@transaction.atomic
def create_lead(text, user, docs=None, caller=None, source=Lead.Source.EMAIL):
    """Read an inquiry and create the Lead with its lanes and cargo. Returns (lead, problems)."""
    if not ai.enabled():
        raise ai.AiError("AI 识别没有开启")
    r, read, skipped = _run(text, docs, LEAD_PROMPT, LEAD_SCHEMA, _context(), caller)
    problems = [f"没有读取（太大或太多）：{'、'.join(skipped)}"] if skipped else []
    customer_name = ai._s(r.get("customer"), 120)
    company = r.get("company") if r.get("company") in Company.values else Company.ATS
    own = ai.own_company(customer_name)
    if own:
        company, customer_name = own, ""
    customer = Customer.objects.filter(name__iexact=customer_name).first() if customer_name else None
    lead = Lead.objects.create(
        title=ai._s(r.get("title"), 200) or "新询价", company=company, customer=customer,
        prospect_name="" if customer else customer_name, customer_ref=ai._s(r.get("customer_ref"), 80),
        contact_name=ai._s(r.get("contact_name"), 120), contact_email=ai._s(r.get("contact_email"), 200),
        contact_phone=ai._s(r.get("contact_phone"), 80), quote_due=_date(r.get("quote_due")), source=source,
        summary=ai._s(r.get("summary"), 2000), questions="\n".join(ai._s(q, 300) for q in r.get("questions") or [] if ai._s(q)),
        source_text=(text or "") + ("\n\n（附件：" + "、".join(read) + "）" if read else ""),
        owner=user, created_by=user, status=Lead.Status.SCOPING)
    lanes = []
    for i, ln in enumerate(r.get("lanes") or [], 1):
        if not ai._s(ln.get("origin")) and not ai._s(ln.get("destination")):
            continue
        service = ln.get("service") if ln.get("service") in Shipment.Service.values else Shipment.Service.OTR
        lanes.append(LeadLane.objects.create(
            lead=lead, sequence=i, origin=ai._s(ln.get("origin"), 255) or "TBD", origin_zip=ai._s(ln.get("origin_zip"), 12),
            destination=ai._s(ln.get("destination"), 255) or "TBD", destination_zip=ai._s(ln.get("destination_zip"), 12),
            service=service, equipment=ai._s(ln.get("equipment"), 80), trucks=_int(ln.get("trucks")),
            ready_date=_date(ln.get("ready_date")), deliver_by=_date(ln.get("deliver_by")), notes=ai._s(ln.get("notes"), 2000)))
    for c in r.get("cargo") or []:
        made = cargo_from_ai(lead, c, lanes, problems)
        if made is None:
            continue
    return lead, problems


def cargo_from_ai(lead, c, lanes, problems):
    desc = ai._s(c.get("description"), 255)
    if not desc:
        return None
    qty = _int(c.get("qty"))
    vals = {}
    try:
        w = units.parse_weight_lbs(c.get("weight"))
        if w is not None and str(c.get("weight_is_each")).lower() != "true" and qty > 1:
            w = (w / qty).quantize(units.LBS)  # stored per piece
        vals["weight_lbs"] = w
    except ValueError as e:
        problems.append(f"{desc} 重量：{e}")
    for d in ("length", "width", "height"):
        try:
            vals[f"{d}_ft"] = units.parse_length_ft(c.get(d))
        except ValueError as e:
            problems.append(f"{desc} {d}：{e}")
    lane = None
    idx = _int(c.get("lane"), 0) if ai._s(c.get("lane")) else 0
    if 1 <= idx <= len(lanes):
        lane = lanes[idx - 1]
    elif len(lanes) == 1:
        lane = lanes[0]
    ht = str(c.get("handling_type") or "").upper()
    return LeadCargo.objects.create(
        lead=lead, lane=lane, description=desc, qty=qty,
        handling_type=ht if ht in CargoLine.HandlingType.values else CargoLine.HandlingType.UNIT,
        hazmat=str(c.get("hazmat")).lower() == "true", notes=ai._s(c.get("notes"), 255), **vals)


def read_reply(lead, text, docs=None, caller=None):
    """Carrier reply -> Draft list of quotes for a person to check. Nothing is saved here."""
    if not ai.enabled():
        raise ai.AiError("AI 识别没有开启")
    from .quotes import scope_lines

    context = _context() + f"\n\n这条询价（{lead.number}）的需求：\n" + (scope_lines(lead) or lead.summary)
    r, read, skipped = _run(text, docs, REPLY_PROMPT, REPLY_SCHEMA, context, caller)
    lanes = list(lead.lanes.all())
    quotes = []
    for q in r.get("quotes") or []:
        name = ai._s(q.get("carrier"), 120)
        carrier = Carrier.objects.filter(name__iexact=name).first() if name else None
        idx = _int(q.get("lane"), 0) if ai._s(q.get("lane")) else 0
        lane = lanes[idx - 1] if 1 <= idx <= len(lanes) else (lanes[0] if len(lanes) == 1 else None)
        lines = []
        for ln in q.get("lines") or []:
            price = _money(ln.get("unit_price"))
            if price is None:
                continue
            ct = str(ln.get("charge_type") or "").upper()
            lines.append({"charge_type": ct if ct in Charge.Type.values else Charge.Type.OTHER,
                          "description": ai._s(ln.get("description"), 255), "quantity": str(_money(ln.get("quantity")) or 1),
                          "unit_price": str(price), "is_estimate": str(ln.get("is_estimate")).lower() == "true"})
        declined = str(q.get("declined")).lower() == "true"
        vu = _date(q.get("valid_until"))
        quotes.append(Draft(
            carrier_id=carrier.pk if carrier else "", carrier_name="" if carrier else name,
            contact_email=ai._s(q.get("contact_email"), 200), lane_id=lane.pk if lane else "",
            status=CarrierQuote.Status.DECLINED if declined else CarrierQuote.Status.QUOTED,
            equipment=ai._s(q.get("equipment"), 80), per_truck=str(q.get("per_truck")).lower() != "false",
            valid_until=vu.isoformat() if vu else "", transit=ai._s(q.get("transit"), 80),
            conditions=ai._s(q.get("conditions"), 2000), lines=lines,
            extra=[len(lines), len(lines) + 1], line_count=len(lines) + 2))  # two blank rows on the form
    return {"summary": ai._s(r.get("summary"), 500), "quotes": quotes, "read": read, "skipped": skipped,
            "text": (text or "") + ("\n\n（附件：" + "、".join(read) + "）" if read else "")}


@transaction.atomic
def save_reply_quotes(lead, form, user):
    """Save the quotes a person checked on the reply review form. Returns the saved CarrierQuotes."""
    saved = []
    n = min(_int(form.get("count"), 0), 30)
    text = form.get("reply_text", "")[:20000]
    for i in range(n):
        p = f"q{i}_"
        if not form.get(p + "include"):
            continue
        carrier = Carrier.objects.filter(pk=form.get(p + "carrier_id") or 0).first()
        name = ai._s(form.get(p + "carrier_name"), 120)
        if carrier is None and not name:
            raise ai.AiError(f"第 {i + 1} 家：请填写承运商")
        lane = lead.lanes.filter(pk=form.get(p + "lane_id") or 0).first()
        status = form.get(p + "status") if form.get(p + "status") in CarrierQuote.Status.values else CarrierQuote.Status.QUOTED
        # Reuse the 已询价 row for this carrier and lane, if any.
        q = (lead.carrier_quotes.filter(carrier=carrier, lane=lane, status=CarrierQuote.Status.REQUESTED).first()
             if carrier else lead.carrier_quotes.filter(carrier_name=name, lane=lane, status=CarrierQuote.Status.REQUESTED).first())
        q = q or CarrierQuote(lead=lead)
        q.carrier, q.carrier_name, q.lane, q.status = carrier, "" if carrier else name, lane, status
        q.contact_email = ai._s(form.get(p + "contact_email"), 200)
        q.equipment, q.transit = ai._s(form.get(p + "equipment"), 80), ai._s(form.get(p + "transit"), 80)
        q.per_truck = bool(form.get(p + "per_truck"))
        q.valid_until = _date(form.get(p + "valid_until"))
        q.conditions = ai._s(form.get(p + "conditions"), 2000)
        q.reply_text = text
        q.save()
        q.lines.all().delete()
        for j in range(min(_int(form.get(p + "lines"), 0), 30)):
            lp = f"{p}l{j}_"
            price = _money(form.get(lp + "unit_price"))
            if price is None or form.get(lp + "delete"):
                continue
            ct = form.get(lp + "charge_type")
            CarrierQuoteLine.objects.create(
                quote=q, charge_type=ct if ct in Charge.Type.values else Charge.Type.OTHER,
                description=ai._s(form.get(lp + "description"), 255), quantity=_money(form.get(lp + "quantity")) or 1,
                unit_price=price, is_estimate=bool(form.get(lp + "is_estimate")))
        saved.append(q)
    if saved and lead.status in (Lead.Status.NEW, Lead.Status.SCOPING):
        lead.status = Lead.Status.SOURCING
        lead.save(update_fields=["status", "updated_at"])
    return saved
