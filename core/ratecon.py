"""Carrier Rate Agreement (rate confirmation) for a project: one document per carrier per project, not per load.

It tells the carrier what we pay (rate per truck plus accessorial schedule) and how to invoice our AP team.
The customer's name and the AR side never appear. Terms: settings.TMS_RATECON_TERMS (English).
"""
from collections import Counter
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, SimpleDocTemplate, Spacer, Table, TableStyle

from .bol import (GREY, LINE, W, P, box_style, company_info, english, fmt_num, logo_image, s_bolno, s_cell, s_cell_b, s_cell_r,
                  s_label, s_small, s_sub, s_title, s_value, s_value_b)
from .models import Charge, RateLine

ACCESSORIAL = {Charge.Type.DETENTION, Charge.Type.LAYOVER, Charge.Type.TONU, Charge.Type.STORAGE, Charge.Type.TARP,
               Charge.Type.CHASSIS, Charge.Type.PREPULL, Charge.Type.PERDIEM}
UNIT_EN = {RateLine.Unit.TRUCK: "per truck", RateLine.Unit.HOUR: "per hour", RateLine.Unit.DAY: "per day",
           RateLine.Unit.EACH: "each", RateLine.Unit.FLAT: "flat"}


# ---------------------------------------------------------------------------
# Defaults for a new agreement
# ---------------------------------------------------------------------------
def default_invoice_instructions(project):
    co = company_info(project)
    email = co.get("ap_email") or "[AP e-mail]"
    bill_to = " · ".join(x for x in [co["name"], co.get("address", "")] if x)
    return "\n".join([
        f"Bill to: {bill_to}",
        f"Send invoices by e-mail to: {email}",
        "One invoice per truck / load. Each invoice must show this Rate Agreement #, our Load #, the container # (if any) and the BOL #.",
        "Attach the signed BOL / POD. Invoices without a clean signed POD are not processed.",
        "Accessorials (detention, layover, TONU, etc.) are paid only with the written approval attached and the times on the BOL / POD.",
        f"Payment terms: {settings.TMS_RATECON_PAYMENT_TERMS}",
    ])


def _city(loc):
    if loc is None:
        return ""
    if loc.city:
        return f"{loc.city}, {loc.state}".strip(", ")
    return english(loc.name or loc.code)


def _commodity(project, leg):
    """English commodity text: the Load's cargo lines (entered in English for the BOL), else the project description."""
    loads = [leg.shipment] if leg else list(project.shipments.all()[:1])
    for s in loads:
        names = list(dict.fromkeys(english(c.description) for c in s.cargo_lines.all() if english(c.description)))
        if names:
            return "; ".join(names)[:255]
    return english(project.cargo_description, drop_lines=True)


def prefill(project, carrier):
    """Field values and rate lines for a new agreement: from the RFQ the project came from (the carrier's
    quote), else from what the project already pays this carrier per load (most common amount per charge type)."""
    legs = [lg for s in project.shipments.select_related().prefetch_related("legs__origin", "legs__destination", "cargo_lines")
            for lg in s.legs.all() if lg.carrier_id == carrier.pk]
    first = legs[0] if legs else None
    data = {
        "origin": _city(first.origin) if first else _city(project.default_shipper),
        "destination": _city(first.destination) if first else _city(project.default_consignee),
        "commodity": _commodity(project, first),
        "equipment": english(first.equipment or (first.shipment.get_equipment_display() if first.shipment.equipment else ""))
        if first else "",
        "free_hours": project.detention_free_hours,
        "valid_from": project.start_date,
        "invoice_instructions": default_invoice_instructions(project),
    }
    lines = []
    lead = project.leads.order_by("-pk").first() if hasattr(project, "leads") else None
    if lead:
        quote = next((q for q in lead.carrier_quotes.prefetch_related("lines") if q.carrier_id == carrier.pk
                      and q.status in ("SELECTED", "QUOTED")), None)
        if quote:
            data["equipment"] = data["equipment"] or english(quote.equipment)
            for ln in quote.lines.all():
                lines.append({"charge_type": ln.charge_type, "description": english(ln.description), "unit_price": ln.unit_price,
                              "unit": RateLine.Unit.TRUCK if quote.per_truck and not ln.is_estimate else (
                                  RateLine.Unit.HOUR if ln.charge_type == Charge.Type.DETENTION else RateLine.Unit.EACH),
                              "optional": ln.is_estimate})
    if not lines:
        per_type = {}
        for c in Charge.objects.filter(shipment__project=project, side=Charge.Side.AP, carrier=carrier).exclude(state=Charge.State.VOID):
            per_type.setdefault(c.charge_type, Counter())[c.unit_price] += 1
        for ctype, counts in per_type.items():
            price = counts.most_common(1)[0][0]
            accessorial = ctype in ACCESSORIAL
            lines.append({"charge_type": ctype, "description": "", "unit_price": price, "optional": accessorial,
                          "unit": RateLine.Unit.HOUR if ctype == Charge.Type.DETENTION else (
                              RateLine.Unit.EACH if accessorial else RateLine.Unit.TRUCK)})
    if not any(ln["charge_type"] == Charge.Type.DETENTION for ln in lines):
        lines.append({"charge_type": Charge.Type.DETENTION, "description": "After free time, with real-time notice and written approval",
                      "unit_price": Decimal("0"), "unit": RateLine.Unit.HOUR, "optional": True})
    return data, lines


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------
def context(agreement, user=None):
    project = agreement.project
    lines = list(agreement.lines.all())
    loads = [s for s in project.shipments.prefetch_related("legs") if any(lg.carrier_id == agreement.carrier_id for lg in s.legs.all())]
    return {
        "a": agreement, "project": project, "company": company_info(project), "carrier": agreement.carrier,
        "lines": lines, "base": [ln for ln in lines if not ln.optional], "extras": [ln for ln in lines if ln.optional],
        "per_truck": agreement.per_truck, "version": agreement.version + 1, "loads": loads,
        "dispatcher": ((user.get_full_name() or user.email) if user else "", getattr(user, "email", "") if user else ""),
        "today": timezone.localdate(),
    }


def problems(ctx):
    """What to fix before sending (Chinese, for the page)."""
    out, a, c = [], ctx["a"], ctx["carrier"]
    if not (c.mc_number or c.dot_number):
        out.append(f"{c.name} 没有 MC# / DOT#")
    out += [f"{c.name}：{p}" for p in c.compliance_problems()]
    if not ctx["base"] or not any(ln.unit_price for ln in ctx["base"]):
        out.append("运价表里没有基本运费（每车价格）")
    if "[AP e-mail]" in a.invoice_instructions:
        out.append("开票说明里还没有 AP 邮箱：在设置里填 TMS_ATS_AP_EMAIL / TMS_PTT_AP_EMAIL，或直接改开票说明")
    if any(ln.optional and not ln.unit_price for ln in ctx["extras"]):
        out.append("有附加费没填单价（如 Detention 每小时多少），不填会印成 “as approved in writing”")
    return out


def _money(v):
    return f"${Decimal(v):,.2f}"


def _rate_rows(lines):
    rows = []
    for ln in lines:
        rate = _money(ln.unit_price) if ln.unit_price else "As approved in writing"
        rows.append([P(ln.get_charge_type_display(), s_cell), P(english(ln.description), s_cell), P(rate, s_cell_r),
                     P(UNIT_EN.get(ln.unit, ln.unit), s_cell)])
    return rows


def build(ctx):
    a, co, carrier = ctx["a"], ctx["company"], ctx["carrier"]
    story = []
    logo = logo_image(co["logo_path"]) if co["logo_path"] else P(co["name"], s_title)
    company = " · ".join(x for x in [co["name"], co["address"], co["phone"]] if x)
    head = Table([[
        logo,
        [P("RATE CONFIRMATION", s_title), P("Carrier Rate Agreement — covers every load of this lane", s_sub), P(company, s_sub)],
        [P("Agreement #", ParagraphStyle("rcl", parent=s_label, alignment=TA_RIGHT)), P(a.number, s_bolno),
         P(f"Date: {ctx['today'].strftime('%m/%d/%Y')}" + (f" · Rev {ctx['version']}" if ctx["version"] > 1 else ""),
           ParagraphStyle("rcd", parent=s_cell, alignment=TA_RIGHT))],
    ]], colWidths=[1.9 * inch, W - 1.9 * inch - 2.1 * inch, 2.1 * inch])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 6)]

    disp_name, disp_email = ctx["dispatcher"]
    broker = [P("BROKER", s_label), P(co["name"], s_value_b)]
    if co["address"]:
        broker.append(P(co["address"], s_cell))
    if co["phone"]:
        broker.append(P(f"Phone: {co['phone']}", s_cell))
    if disp_name:
        broker.append(P(f"Contact: {disp_name}" + (f" · {disp_email}" if disp_email and disp_email != disp_name else ""), s_cell))
    ids = " · ".join(x for x in [f"MC# {carrier.mc_number}" if carrier.mc_number else "", f"USDOT# {carrier.dot_number}" if carrier.dot_number else "",
                                 f"SCAC {carrier.scac}" if carrier.scac else ""] if x)
    carr = [P("CARRIER", s_label), P(carrier.name, s_value_b)]
    if ids:
        carr.append(P(ids, s_cell))
    contact = " · ".join(x for x in [english(carrier.contact_name), carrier.contact_phone, carrier.contact_email] if x)
    if contact:
        carr.append(P(f"Contact: {contact}", s_cell))
    t = Table([[broker, carr]], colWidths=[W / 2, W / 2])
    t.setStyle(box_style())
    story += [t, Spacer(1, 5)]

    valid = " – ".join(x for x in [a.valid_from.strftime("%m/%d/%Y") if a.valid_from else "",
                                   a.valid_until.strftime("%m/%d/%Y") if a.valid_until else "until the project is complete"] if x)
    lane = [
        [P("Lane", s_label), P(f"{english(a.origin)}  →  {english(a.destination)}", s_value_b), P("Project ref", s_label), P(ctx["project"].number, s_value_b)],
        [P("Commodity", s_label), P(english(a.commodity) or "—", s_value), P("Equipment", s_label), P(english(a.equipment) or "—", s_value_b)],
        [P("Capacity", s_label), P(english(a.capacity) or "As tendered by the broker", s_value), P("Valid", s_label), P(valid, s_value)],
    ]
    t = Table(lane, colWidths=[0.95 * inch, W / 2 - 0.95 * inch + 0.6 * inch, 0.95 * inch, W / 2 - 0.95 * inch - 0.6 * inch])
    t.setStyle(box_style())
    story += [t, Spacer(1, 5)]

    head_row = [[P(h, s_label) for h in ("Charge", "Description", "Rate (USD)", "Unit")]]
    widths = [1.5 * inch, W - 1.5 * inch - 1.4 * inch - 1.1 * inch, 1.4 * inch, 1.1 * inch]
    rows = head_row + (_rate_rows(ctx["base"]) or [[P("—", s_cell), P("Rate to be confirmed in writing", s_cell), P("", s_cell), P("", s_cell)]])
    if ctx["per_truck"]:
        rows.append([P("", s_cell), P("ALL-IN RATE PER TRUCK", s_cell_b), P(_money(ctx["per_truck"]), ParagraphStyle("pt", parent=s_cell_b, alignment=TA_RIGHT, fontSize=10.5, leading=13)),
                     P("per truck", s_cell_b)])
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY)]))
    story += [KeepTogether([P("RATE", s_label), t]), Spacer(1, 4)]
    extra = [P(f"Free time: {fmt_num(a.free_hours)} hours at each pickup and delivery. The accessorials below are paid only when reported "
               "while they happen and approved in writing by the broker.", s_cell)]
    if ctx["extras"]:
        t = Table(head_row + _rate_rows(ctx["extras"]), colWidths=widths)
        t.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY)]))
        extra.append(t)
    story += [KeepTogether([P("ACCESSORIALS", s_label)] + extra), Spacer(1, 5)]

    def boxed(title, text):
        lines = [x for x in english(text, drop_lines=True).split("\n")]
        tb = Table([[P(title, s_label)]] + [[P(x or " ", s_cell)] for x in lines], colWidths=[W])
        tb.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, LINE), ("BACKGROUND", (0, 0), (-1, 0), GREY),
                                ("LEFTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
        return tb

    story += [boxed("HOW TO INVOICE US", a.invoice_instructions or default_invoice_instructions(ctx["project"])), Spacer(1, 5)]
    if a.notes.strip():
        story += [boxed("OTHER CONDITIONS", a.notes), Spacer(1, 5)]
    ops = ("Each load is tendered by e-mail with its Load #, pickup / delivery appointments and BOL. Send tracking updates at pickup, "
           "daily in transit, and on arrival / departure at each stop. Oversize / overweight loads follow every permit condition.")
    story += [boxed("DISPATCH", ops), Spacer(1, 5)]

    terms = [P("TERMS AND CONDITIONS", s_label)] + [P(f"{i}. {x}", s_small) for i, x in enumerate(settings.TMS_RATECON_TERMS, 1)]
    t = Table([[x] for x in terms], colWidths=[W])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 4),
                           ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    story += [t, Spacer(1, 6)]

    sig = Table([
        [P("ACCEPTED BY CARRIER", s_label), P("ISSUED BY BROKER", s_label)],
        [P("Signature: ______________________________", s_value), P(co["name"], s_value_b)],
        [P("Printed name: ___________________________", s_value), P(f"By: {disp_name or '______________________'}", s_value)],
        [P("Date: _____________   MC#: ______________", s_value), P(f"Date: {ctx['today'].strftime('%m/%d/%Y')}", s_value)],
    ], colWidths=[W / 2, W / 2])
    sig.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY), ("INNERGRID", (0, 1), (-1, -1), 0, LINE)]))
    story.append(KeepTogether(sig))
    story.append(P("This agreement applies to every load the broker tenders to the carrier on this lane while it is valid. "
                   "A revised agreement replaces this one from its date. Please sign and return before the first pickup.", s_small))
    return story


class _Doc(SimpleDocTemplate):
    number = ""

    def afterPage(self):
        c = self.canv
        c.saveState()
        c.setFont("Helvetica", 7)
        c.drawRightString(self.pagesize[0] - self.rightMargin, self.bottomMargin * 0.45, f"Rate Agreement {self.number}  ·  Page {c.getPageNumber()}")
        c.restoreState()


def render(ctx):
    buf = BytesIO()
    doc = _Doc(buf, pagesize=LETTER, leftMargin=0.4 * inch, rightMargin=0.4 * inch, topMargin=0.4 * inch, bottomMargin=0.45 * inch,
               title=f"Rate Confirmation {ctx['a'].number}", author=ctx["company"]["name"])
    doc.number = ctx["a"].number
    doc.build(build(ctx))
    return buf.getvalue()


def email_draft(ctx):
    """(to, subject, body) for a Gmail draft. A person attaches the PDF and presses Send."""
    a, c = ctx["a"], ctx["carrier"]
    subject = f"Rate Confirmation {a.number} - {english(a.origin)} to {english(a.destination)}"
    rate = f"{_money(ctx['per_truck'])} per truck all-in" if ctx["per_truck"] else "see attached"
    body = "\n".join([
        f"Hi {english(c.contact_name) if c.contact_name else 'team'},",
        "",
        f"Attached is our Rate Confirmation {a.number} for {english(a.origin)} to {english(a.destination)}"
        + (f" ({english(a.equipment)})" if a.equipment else "") + f": {rate}.",
        "It covers every load we tender to you on this lane, and explains how to invoice our AP team.",
        "",
        "Please sign and return it before the first pickup.",
        "",
        "Thank you,",
        ctx["dispatcher"][0] or ctx["company"]["name"],
        ctx["company"]["name"],
    ])
    return c.contact_email or "", subject, body
