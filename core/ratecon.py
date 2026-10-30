"""Rate Confirmation (load tender) to the carrier for one leg, rendered with ReportLab.

What the carrier is paid comes from the Load's AP charges for that carrier (void ones left out).
The customer's name and the AR side never appear. Terms are settings.TMS_RATECON_TERMS (English);
the defaults are a starting point the company should have reviewed.
"""
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, SimpleDocTemplate, Spacer, Table, TableStyle

from .bol import (GREY, LINE, W, P, appointments, box_style, company_info, english, fmt_num, logo_image, party_block,
                  s_bolno, s_cell, s_cell_b, s_cell_r, s_label, s_small, s_sub, s_title, s_value, s_value_b)
from .models import Charge

s_total = ParagraphStyle("rctotal", parent=s_cell_b, fontSize=10.5, leading=13, alignment=TA_RIGHT)


def ratecon_number(leg):
    return f"RC-{leg.shipment.number}-{leg.sequence}"


def carrier_charges(leg):
    """AP charges of this Load owed to the leg's carrier (any not tied to a carrier count when the Load has one carrier)."""
    s = leg.shipment
    qs = s.charges.filter(side=Charge.Side.AP).exclude(state=Charge.State.VOID).order_by("pk")
    carriers = {lg.carrier_id for lg in s.legs.all() if lg.carrier_id}
    if leg.carrier_id:
        if len(carriers) <= 1:
            return list(qs.filter(carrier_id__in=[leg.carrier_id, None]))
        return list(qs.filter(carrier_id=leg.carrier_id))
    return list(qs.filter(carrier__isnull=True))


def ratecon_context(leg, user=None):
    s = leg.shipment
    project = s.project
    charges = carrier_charges(leg)
    pickup, delivery = appointments(leg)
    return {
        "leg": leg, "shipment": s, "project": project, "company": company_info(project), "carrier": leg.carrier,
        "number": ratecon_number(leg), "version": (leg.ratecon_version or 0) + 1,
        "charges": charges, "total": sum((c.amount for c in charges), Decimal("0")),
        "pickup_appt": pickup, "delivery_appt": delivery, "lines": list(s.cargo_lines.all()),
        "instructions": "\n\n".join(x for x in [english(leg.bol_instructions, drop_lines=True),
                                                english(project.bol_instructions if project else "", drop_lines=True)] if x),
        "free_hours": project.detention_free_hours if project else Decimal("2"),
        "dispatcher": ((user.get_full_name() or user.email) if user else "", getattr(user, "email", "") if user else ""),
        "today": timezone.localdate(),
    }


def problems(ctx):
    """Things to fix before sending (Chinese, for the page)."""
    out = []
    c = ctx["carrier"]
    if c is None:
        out.append("这一段还没有指定承运商")
    else:
        if not (c.mc_number or c.dot_number):
            out.append(f"{c.name} 没有 MC# / DOT#")
        out += [f"{c.name}：{p}" for p in getattr(c, "compliance_problems", lambda: [])()]
    if not ctx["charges"]:
        out.append("这个 Load 没有付给该承运商的费用（AP），运价确认单上的金额会是空的")
    if not (ctx["leg"].origin and ctx["leg"].destination):
        out.append("运输段缺起点或终点")
    if not ctx["leg"].scheduled_pickup:
        out.append("运输段没有计划提货日")
    return out


def _money(v):
    return f"${Decimal(v):,.2f}"


def build(ctx):
    leg, s, co, carrier = ctx["leg"], ctx["shipment"], ctx["company"], ctx["carrier"]
    story = []
    logo = logo_image(co["logo_path"]) if co["logo_path"] else P(co["name"], s_title)
    company = " · ".join(x for x in [co["name"], co["address"], co["phone"]] if x)
    head = Table([[
        logo,
        [P("RATE CONFIRMATION", s_title), P("Load Tender — Carrier Rate Agreement", s_sub), P(company, s_sub)],
        [P("Rate Con #", ParagraphStyle("rcl", parent=s_label, alignment=TA_RIGHT)), P(ctx["number"], s_bolno),
         P(f"Date: {ctx['today'].strftime('%m/%d/%Y')}" + (f" · Rev {ctx['version']}" if ctx["version"] > 1 else ""),
           ParagraphStyle("rcd", parent=s_cell, alignment=TA_RIGHT))],
    ]], colWidths=[1.9 * inch, W - 1.9 * inch - 2.0 * inch, 2.0 * inch])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 6)]

    # Broker / carrier
    disp_name, disp_email = ctx["dispatcher"]
    broker = [P("BROKER / SHIPPER OF RECORD", s_label), P(co["name"], s_value_b)]
    if co["address"]:
        broker.append(P(co["address"], s_cell))
    if co["phone"]:
        broker.append(P(f"Phone: {co['phone']}", s_cell))
    if disp_name:
        broker.append(P(f"Dispatcher: {disp_name}" + (f" · {disp_email}" if disp_email and disp_email != disp_name else ""), s_cell))
    if carrier:
        ids = " · ".join(x for x in [f"MC# {carrier.mc_number}" if carrier.mc_number else "",
                                     f"USDOT# {carrier.dot_number}" if carrier.dot_number else "",
                                     f"SCAC {carrier.scac}" if carrier.scac else ""] if x)
        carr = [P("CARRIER", s_label), P(carrier.name, s_value_b)]
        if ids:
            carr.append(P(ids, s_cell))
        contact = " · ".join(x for x in [english(carrier.contact_name), carrier.contact_phone, carrier.contact_email] if x)
        if contact:
            carr.append(P(f"Contact: {contact}", s_cell))
    else:
        carr = [P("CARRIER", s_label), P("—")]
    driver = " · ".join(x for x in [english(leg.driver_name), leg.driver_phone, english(leg.truck_trailer)] if x)
    if driver:
        carr.append(P(f"Driver / Truck: {driver}", s_cell))
    t = Table([[broker, carr]], colWidths=[W / 2, W / 2])
    t.setStyle(box_style())
    story += [t, Spacer(1, 5)]

    # References and equipment
    refs = [
        [P("Our Load #", s_label), P(s.number, s_value_b), P("Carrier Load / PRO #", s_label), P(leg.carrier_load_number or "—", s_value_b)],
        [P("Container #", s_label), P(s.container_number or "—", s_value_b), P("BOL #", s_label), P(leg.effective_bol_number, s_value_b)],
        [P("Equipment", s_label), P(english(leg.equipment or (s.get_equipment_display() if s.equipment else "")) or "—", s_value_b),
         P("Permits / Escorts", s_label), P(("Required" + (" — confirmed" if leg.permits_confirmed else " — carrier to arrange per rate below"))
                                            if leg.permits_required else "Not required", s_value_b)],
    ]
    t = Table(refs, colWidths=[1.1 * inch, W / 2 - 1.1 * inch, 1.3 * inch, W / 2 - 1.3 * inch])
    t.setStyle(box_style())
    story += [t, Spacer(1, 5)]

    # Stops
    t = Table([[party_block("PICKUP", leg.origin, ctx["pickup_appt"]), party_block("DELIVERY", leg.destination, ctx["delivery_appt"])]],
              colWidths=[W / 2, W / 2])
    t.setStyle(box_style())
    story += [t, Spacer(1, 5)]

    # Commodity
    rows = [[P(h, s_label) for h in ("Qty", "Commodity", "L × W × H (ft)", "Weight (lbs)", "HM")]]
    for ln in ctx["lines"]:
        dims = " × ".join(fmt_num(v) or "?" for v in (ln.length_ft, ln.width_ft, ln.height_ft)) if any(
            (ln.length_ft, ln.width_ft, ln.height_ft)) else ""
        hm = " ".join(x for x in [ln.un_number, f"Class {ln.hazard_class}" if ln.hazard_class else ""] if x) if ln.hazmat else ""
        rows.append([P(f"{ln.handling_qty} {ln.get_handling_type_display()}", s_cell), P(english(ln.description), s_cell),
                     P(dims, s_cell), P(fmt_num(ln.weight_lbs), s_cell_r), P(hm or ("X" if ln.hazmat else ""), s_cell)])
    if len(rows) == 1:
        rows.append([P("", s_cell), P("See BOL", s_cell), P("", s_cell), P("", s_cell), P("", s_cell)])
    t = Table(rows, colWidths=[0.9 * inch, W - 0.9 * inch - 1.4 * inch - 1.0 * inch - 1.0 * inch, 1.4 * inch, 1.0 * inch, 1.0 * inch],
              repeatRows=1)
    t.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY)]))
    story += [t, Spacer(1, 5)]

    # Rate
    rows = [[P(h, s_label) for h in ("Charge", "Description", "Qty", "Rate", "Amount")]]
    for c in ctx["charges"]:
        rows.append([P(c.get_charge_type_display(), s_cell), P(english(c.description), s_cell), P(fmt_num(c.quantity), s_cell_r),
                     P(_money(c.unit_price), s_cell_r), P(_money(c.amount), s_cell_r)])
    if not ctx["charges"]:
        rows.append([P("—", s_cell), P("Rate to be confirmed in writing", s_cell), P("", s_cell), P("", s_cell), P("", s_cell)])
    rows.append([P("", s_cell), P("", s_cell), P("", s_cell), P("TOTAL (USD)", s_cell_b), P(_money(ctx["total"]), s_total)])
    t = Table(rows, colWidths=[1.3 * inch, W - 1.3 * inch - 0.6 * inch - 1.1 * inch - 1.2 * inch, 0.6 * inch, 1.1 * inch, 1.2 * inch])
    t.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY), ("SPAN", (0, -1), (2, -1))]))
    story += [KeepTogether(t), Spacer(1, 5)]

    # Instructions
    lines = [
        f"Detention: {fmt_num(ctx['free_hours'])} hours free at each stop. Detention, layover, TONU and any other accessorial are payable "
        "only if reported while it happens and approved in writing by the dispatcher above.",
        "Send tracking updates at pickup, in transit at least daily, and on arrival / departure at each stop.",
    ]
    if ctx["instructions"]:
        lines = ctx["instructions"].split("\n") + [""] + lines
    t = Table([[P("SPECIAL INSTRUCTIONS", s_label)]] + [[P(x or " ", s_cell)] for x in lines], colWidths=[W])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, LINE), ("BACKGROUND", (0, 0), (-1, 0), GREY),
                           ("LEFTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    story += [t, Spacer(1, 5)]

    # Terms
    terms = [P("TERMS AND CONDITIONS", s_label), P(f"Payment: {settings.TMS_RATECON_PAYMENT_TERMS}", s_small)]
    terms += [P(f"{i}. {x}", s_small) for i, x in enumerate(settings.TMS_RATECON_TERMS, 1)]
    t = Table([[x] for x in terms], colWidths=[W])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 4),
                           ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    story += [t, Spacer(1, 6)]

    # Signatures
    sig = Table([
        [P("ACCEPTED BY CARRIER", s_label), P("ISSUED BY BROKER", s_label)],
        [P("Signature: ______________________________", s_value), P(f"{co['name']}", s_value_b)],
        [P("Printed name: ___________________________", s_value), P(f"By: {disp_name or '______________________'}", s_value)],
        [P("Date: _____________   MC#: ______________", s_value), P(f"Date: {ctx['today'].strftime('%m/%d/%Y')}", s_value)],
    ], colWidths=[W / 2, W / 2])
    sig.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY), ("INNERGRID", (0, 1), (-1, -1), 0, LINE)]))
    story.append(KeepTogether(sig))
    story.append(P("Please sign and return this confirmation before dispatch. Dispatching the load confirms acceptance of this rate and these terms.",
                   s_small))
    return story


class _Doc(SimpleDocTemplate):
    number = ""

    def afterPage(self):
        c = self.canv
        c.saveState()
        c.setFont("Helvetica", 7)
        c.drawRightString(self.pagesize[0] - self.rightMargin, self.bottomMargin * 0.45,
                          f"Rate Con {self.number}  ·  Page {c.getPageNumber()}")
        c.restoreState()


def render(ctx):
    buf = BytesIO()
    doc = _Doc(buf, pagesize=LETTER, leftMargin=0.4 * inch, rightMargin=0.4 * inch, topMargin=0.4 * inch, bottomMargin=0.45 * inch,
               title=f"Rate Confirmation {ctx['number']}", author=ctx["company"]["name"])
    doc.number = ctx["number"]
    doc.build(build(ctx))
    return buf.getvalue()


def email_draft(ctx):
    """(to, subject, body) for a Gmail draft. A person attaches the PDF and presses Send."""
    leg, s = ctx["leg"], ctx["shipment"]
    c = ctx["carrier"]
    pickup = f"{leg.origin.city}, {leg.origin.state}" if leg.origin and leg.origin.city else (english(str(leg.origin)) if leg.origin else "")
    drop = f"{leg.destination.city}, {leg.destination.state}" if leg.destination and leg.destination.city else (
        english(str(leg.destination)) if leg.destination else "")
    subject = f"Rate Confirmation {ctx['number']} - {s.container_number or s.number} {pickup} to {drop}".strip()
    body = "\n".join([
        f"Hi {english(c.contact_name) if c and c.contact_name else 'team'},",
        "",
        f"Please find attached Rate Confirmation {ctx['number']} for {s.container_number or s.number}"
        + (f", pickup {leg.scheduled_pickup.strftime('%m/%d/%Y')}" if leg.scheduled_pickup else "") + ".",
        f"Total: {_money(ctx['total'])} USD.",
        "",
        "Please sign and return it before dispatch, and send driver name, phone and truck / trailer number.",
        "",
        "Thank you,",
        ctx["dispatcher"][0] or ctx["company"]["name"],
        ctx["company"]["name"],
    ])
    return (c.contact_email if c else ""), subject, body
