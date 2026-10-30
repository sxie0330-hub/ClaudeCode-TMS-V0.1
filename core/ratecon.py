"""Carrier Rate Agreement (rate confirmation) for a project: one text agreement per carrier per project, not per load.

Party A is our company (ATS or PTT, from the project), Party B the carrier. It states what Party A requires, the rate
Party B quoted, the proof needed for detention and other accessorials, and how to invoice Party A's AP team (from the
company's "Carrier Invoice Submission Guidelines"). It never commits Party A to any volume. The customer's name and
the AR side never appear. Terms: settings.TMS_RATECON_TERMS (English).
"""
from collections import Counter
from decimal import Decimal
from io import BytesIO

from django.conf import settings
from django.utils import timezone
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .bol import company_info, english, esc, fmt_num, logo_image
from .models import Charge, RateLine

ACCESSORIAL = {Charge.Type.DETENTION, Charge.Type.LAYOVER, Charge.Type.TONU, Charge.Type.STORAGE, Charge.Type.TARP,
               Charge.Type.CHASSIS, Charge.Type.PREPULL, Charge.Type.PERDIEM}
UNIT_EN = {RateLine.Unit.TRUCK: "per truck", RateLine.Unit.HOUR: "per hour", RateLine.Unit.DAY: "per day",
           RateLine.Unit.EACH: "each", RateLine.Unit.FLAT: "flat"}

DEFAULT_REQUIREMENTS = "\n".join([
    "Provide the equipment stated above, in good working condition, with straps / chains / tarps as the load requires.",
    "Arrive on time for every pickup and delivery appointment; tell Party A at once of any delay, breakdown or exception.",
    "Load, secure and transport the cargo safely and legally; oversize / overweight loads follow every permit and escort condition.",
    "Send tracking updates at pickup, at least daily in transit, and on arrival / departure at each stop.",
    "Have the shipper and consignee sign the BOL / POD with printed name, date and check-in / check-out times.",
    "Send the signed POD to Party A operations within 24 hours of delivery.",
])

# Proof Party B must provide before an accessorial is paid (Party A's invoice guidelines plus detention evidence).
ACCESSORIAL_PROOF = [
    "Detention: (1) Party B told Party A operations in writing, while it was happening, that free time had run out; "
    "(2) the BOL / POD shows check-in and check-out times signed by both the driver and the shipper / consignee; "
    "(3) a GPS or ELD record shows the truck's arrival and departure at the facility; and (4) Party A operations approved it in writing. "
    "Time caused by Party B (late arrival, missing equipment, driver hours) is not paid.",
    "Port detention: the GPS record of the time at the terminal.",
    "Layover: written approval from Party A operations before it starts.",
    "Truck ordered not used (TONU): Party A's written cancellation after the truck was dispatched.",
    "Chassis: in and out Equipment Interchange Receipts (EIRs); chassis split fees: proof the chassis was returned to a different location.",
    "Weight charges: the scale / weight tickets.",
    "Any other charge: the communication showing Party A operations approved it.",
]


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
        "requirements": DEFAULT_REQUIREMENTS,
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
        lines.append({"charge_type": Charge.Type.DETENTION, "description": "After free time, with the proof listed in this agreement",
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
    out, c = [], ctx["carrier"]
    if not (c.mc_number or c.dot_number):
        out.append(f"{c.name} 没有 MC# / DOT#")
    out += [f"{c.name}：{p}" for p in c.compliance_problems()]
    if not ctx["base"] or not any(ln.unit_price for ln in ctx["base"]):
        out.append("运价里没有基本运费（乙方每车报价）")
    if not ctx["company"].get("ap_email"):
        out.append("这家公司还没有设置收发票邮箱")
    if any(ln.optional and not ln.unit_price for ln in ctx["extras"]):
        out.append("有附加费没填单价（如 Detention 每小时多少），不填会写成 “as approved by Party A in writing”")
    return out


def _money(v):
    return f"USD {Decimal(v):,.2f}"


def _rate_text(ln):
    rate = f"{_money(ln.unit_price)} {UNIT_EN.get(ln.unit, ln.unit)}" if ln.unit_price else "as approved by Party A in writing"
    desc = english(ln.description)
    return f"{ln.get_charge_type_display()}: {rate}" + (f" ({desc})" if desc else "")


# Styles for a plain text agreement
_body = ParagraphStyle("ra_body", fontName="Helvetica", fontSize=9, leading=12)
_small = ParagraphStyle("ra_small", parent=_body, fontSize=8, leading=10.5, bulletFontSize=8, leftIndent=14, bulletIndent=2)
_head = ParagraphStyle("ra_head", parent=_body, fontName="Helvetica-Bold", fontSize=9.5, leading=13, spaceBefore=7, spaceAfter=2)
_title = ParagraphStyle("ra_title", parent=_body, fontName="Helvetica-Bold", fontSize=15, leading=19, alignment=TA_CENTER)
_sub = ParagraphStyle("ra_sub", parent=_body, alignment=TA_CENTER)
_item = ParagraphStyle("ra_item", parent=_body, leftIndent=14, bulletIndent=4)


def _p(text, style=_body):
    return Paragraph(esc(text), style)


def _items(lines, style=_item):
    return [Paragraph(esc(x), style, bulletText="•") for x in lines if x and x.strip()]


def invoicing_lines(ctx):
    co, a = ctx["company"], ctx["a"]
    out = [
        f"Send every invoice to: {co.get('ap_email') or '—'}. The e-mail subject must include the invoice number. All documents in PDF.",
        "One invoice per load. The invoice must quote this agreement number, Party A's Load # and the container / batch number "
        "(if any), and must match the supporting documents.",
        "Attach the Bill of Lading / Proof of Delivery, clear and legible, with the delivery signature, printed name, and delivery "
        "date and time, plus the supporting documents for any accessorial (section 5). Invoice and documents may be one combined "
        "PDF or clearly named separate files.",
        "For high-volume or storage billing, also attach a summary Excel sheet listing invoice number, reference number "
        "(container or batch) and invoice amount.",
        "All documents must be clearly scanned and fully legible, and all reference numbers must be the same on the invoice, POD "
        "and supporting documents. Incomplete or non-compliant invoices are returned unpaid and may delay payment.",
        "Payment terms: " + settings.TMS_RATECON_PAYMENT_TERMS.format(days=a.payment_days or 30),
    ]
    if co.get("statements_to"):
        out.append("Statements: " + ", ".join(co["statements_to"]) + ".")
    if co.get("ap_contact"):
        out.append(f"Questions: {co['ap_contact']}.")
    extra = [x for x in english(a.invoice_instructions, drop_lines=True).split("\n") if x.strip()]
    return out + extra


def build(ctx):
    a, co, carrier, project = ctx["a"], ctx["company"], ctx["carrier"], ctx["project"]
    story = []
    if co["logo_path"]:
        story += [logo_image(co["logo_path"], max_w=1.9 * inch, max_h=0.7 * inch), Spacer(1, 4)]
    story += [_p("CARRIER RATE CONFIRMATION AND AGREEMENT", _title),
              _p(f"Agreement No. {a.number}" + (f"  ·  Revision {ctx['version']}" if ctx["version"] > 1 else "")
                 + f"  ·  Date {ctx['today'].strftime('%m/%d/%Y')}", _sub), Spacer(1, 6)]

    party_a = ", ".join(x for x in [co["name"], co["address"], co["phone"]] if x)
    disp_name, disp_email = ctx["dispatcher"]
    ids = ", ".join(x for x in [f"MC# {carrier.mc_number}" if carrier.mc_number else "",
                                f"USDOT# {carrier.dot_number}" if carrier.dot_number else ""] if x)
    contact = ", ".join(x for x in [english(carrier.contact_name), carrier.contact_phone, carrier.contact_email] if x)
    story += [_p("1. Parties", _head),
              _p(f"Party A (broker): {party_a}" + (f". Operations contact: {disp_name}" + (f", {disp_email}" if disp_email and disp_email != disp_name else "") if disp_name else "") + "."),
              _p(f"Party B (carrier): {carrier.name}" + (f", {ids}" if ids else "") + (f". Contact: {contact}" if contact else "") + ".")]

    valid = (f"from {a.valid_from.strftime('%m/%d/%Y')} " if a.valid_from else "") + (
        f"until {a.valid_until.strftime('%m/%d/%Y')}" if a.valid_until else "until the project is complete or this agreement is ended under section 7")
    scope = [f"Project: {project.number}", f"Lane: {english(a.origin)} to {english(a.destination)}"]
    if a.commodity:
        scope.append(f"Commodity: {english(a.commodity)}")
    if a.equipment:
        scope.append(f"Equipment: {english(a.equipment)}")
    scope.append(f"Term: {valid}")
    story += [_p("2. Project and lane", _head)] + _items(scope)

    reqs = [x for x in english(a.requirements, drop_lines=True).split("\n") if x.strip()]
    story += [_p("3. What Party A requires of Party B", _head)] + _items(reqs or DEFAULT_REQUIREMENTS.split("\n"))

    vol = ("Party A does not commit to any number of loads. Party A tenders loads one at a time, at its sole discretion, by written "
           "load tender; each tender states the Load #, pickup / delivery appointments and BOL. Party A may assign any load to another "
           "carrier at any time.")
    if a.capacity.strip():
        vol += f" Party B states its capacity as: {english(a.capacity)}. This is Party B's statement only and does not oblige Party A to tender that volume."
    story += [_p("4. Volume", _head), _p(vol)]

    rate_lines = [_rate_text(ln) for ln in ctx["base"]] or ["Rate to be confirmed by Party A in writing"]
    if ctx["per_truck"] and len(ctx["base"]) > 1:
        rate_lines.append(f"Total per truck: {_money(ctx['per_truck'])}")
    acc = [f"Free time: {fmt_num(a.free_hours)} hours at each pickup and each delivery."] + [_rate_text(ln) for ln in ctx["extras"]]
    story += [_p("5. Rate quoted by Party B", _head), _p("Party B quoted, and Party A accepts, the following rate for each load. "
                                                            "It is all-in unless an item is listed below."),
              *_items(rate_lines),
              _p("Accessorials are paid only with the proof below and only when the charge is not caused by Party B:", _body),
              *_items(acc + ACCESSORIAL_PROOF)]

    story += [_p("6. How to invoice Party A", _head)] + _items(invoicing_lines(ctx))
    if a.notes.strip():
        story += [_p("Other conditions", _head)] + _items([x for x in english(a.notes, drop_lines=True).split("\n") if x.strip()])
    story += [_p("7. Terms", _head)] + [Paragraph(esc(x), _small, bulletText=f"{i}.") for i, x in enumerate(settings.TMS_RATECON_TERMS, 1)]
    story += [Spacer(1, 4), _p("This agreement covers every load Party A tenders to Party B on this lane while it is valid. A revised "
                               "agreement replaces this one from its date. Party B signs and returns it before the first pickup; "
                               "dispatching a tendered load also confirms acceptance.", _small)]

    sig = Table([
        [_p(f"Party A: {co['name']}"), _p(f"Party B: {carrier.name}")],
        [_p("Signature: ______________________________"), _p("Signature: ______________________________")],
        [_p(f"Name / title: {disp_name or '__________________________'}"), _p("Name / title: __________________________")],
        [_p(f"Date: {ctx['today'].strftime('%m/%d/%Y')}"), _p("Date: ___________________")],
    ], colWidths=[3.6 * inch, 3.6 * inch])
    sig.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [Spacer(1, 10), KeepTogether(sig)]
    return story


class _Doc(SimpleDocTemplate):
    number = ""

    def afterPage(self):
        c = self.canv
        c.saveState()
        c.setFont("Helvetica", 7)
        c.drawRightString(self.pagesize[0] - self.rightMargin, self.bottomMargin * 0.45, f"Agreement {self.number}  ·  Page {c.getPageNumber()}")
        c.restoreState()


def render(ctx):
    buf = BytesIO()
    doc = _Doc(buf, pagesize=LETTER, leftMargin=0.65 * inch, rightMargin=0.65 * inch, topMargin=0.5 * inch, bottomMargin=0.55 * inch,
               title=f"Rate Confirmation {ctx['a'].number}", author=ctx["company"]["name"])
    doc.number = ctx["a"].number
    doc.build(build(ctx))
    return buf.getvalue()


def email_draft(ctx):
    """(to, subject, body) for a Gmail draft. A person attaches the PDF and presses Send."""
    a, c = ctx["a"], ctx["carrier"]
    subject = f"Rate Confirmation {a.number} - {english(a.origin)} to {english(a.destination)}"
    rate = f"{_money(ctx['per_truck'])} per truck all-in" if ctx["per_truck"] else "the rate you quoted"
    body = "\n".join([
        f"Hi {english(c.contact_name) if c.contact_name else 'team'},",
        "",
        f"Attached is our Rate Confirmation {a.number} for {english(a.origin)} to {english(a.destination)}"
        + (f" ({english(a.equipment)})" if a.equipment else "") + f", at {rate}.",
        "It covers the loads we tender to you on this lane; each load is confirmed separately by our load tender.",
        "Please read the detention proof and invoicing requirements, then sign and return it before the first pickup.",
        "",
        "Thank you,",
        ctx["dispatcher"][0] or ctx["company"]["name"],
        ctx["company"]["name"],
    ])
    return c.contact_email or "", subject, body
