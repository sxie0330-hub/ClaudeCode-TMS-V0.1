"""One universal straight Bill of Lading, rendered with ReportLab.

The same layout covers a drayage HQ, a flat rack, a BESS unit on an RGN or a
53' van full of pallets: everything variable lives in the commodity table,
which grows to as many cargo lines as the shipment has.
"""
import re
import unicodedata
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

STATIC = Path(__file__).parent / "static" / "core"

GREY = colors.HexColor("#D9D9D9")
LINE = colors.black
W = LETTER[0] - 0.8 * inch  # usable width

s_base = ParagraphStyle("base", fontName="Helvetica", fontSize=8, leading=10)
s_small = ParagraphStyle("small", parent=s_base, fontSize=6.5, leading=8)
s_label = ParagraphStyle("label", parent=s_base, fontName="Helvetica-Bold", fontSize=7, leading=9)
s_value = ParagraphStyle("value", parent=s_base, fontSize=9.5, leading=12)
s_value_b = ParagraphStyle("valueb", parent=s_value, fontName="Helvetica-Bold")
s_title = ParagraphStyle("title", parent=s_base, fontName="Helvetica-Bold", fontSize=18, leading=21)
s_sub = ParagraphStyle("sub", parent=s_base, fontSize=7, leading=9)
s_bolno = ParagraphStyle("bolno", parent=s_base, fontName="Helvetica-Bold", fontSize=13, leading=16, alignment=TA_RIGHT)
s_head = ParagraphStyle("head", parent=s_label, alignment=TA_CENTER)
s_cell = ParagraphStyle("cell", parent=s_base, fontSize=8.5, leading=10.5)
s_cell_c = ParagraphStyle("cellc", parent=s_cell, alignment=TA_CENTER)
s_cell_r = ParagraphStyle("cellr", parent=s_cell, alignment=TA_RIGHT)

LIABILITY_NOTE = (
    "NOTE: Liability limitation for loss or damage in this shipment may be applicable. "
    "See 49 U.S.C. § 14706(c)(1)(A) and (B)."
)
RECEIVED_CLAUSE = (
    "RECEIVED, subject to individually determined rates or contracts that have been agreed upon in writing "
    "between the carrier and shipper, if applicable, otherwise to the rates, classifications and rules that "
    "have been established by the carrier and are available to the shipper, on request, and to all applicable "
    "state and federal regulations."
)
SHIPPER_CERT = (
    "This is to certify that the above named materials are properly classified, packaged, marked and labeled, "
    "and are in proper condition for transportation according to the applicable regulations of the DOT."
)
CARRIER_ACK = (
    "Carrier acknowledges receipt of packages and required placards. Carrier certifies emergency response "
    "information was made available and/or carrier has the DOT emergency response guidebook or equivalent "
    "documentation in the vehicle. Property described above is received in good order, except as noted."
)
CONSIGNEE_ACK = "Received the above described property in apparent good order, except as noted."
DECLARED_VALUE = (
    "Where the rate is dependent on value, shippers are required to state specifically in writing the agreed or "
    "declared value of the property as follows: \"The agreed or declared value of the property is specifically "
    "stated by the shipper to be not exceeding $__________ per __________.\""
)


# BOLs are English-only. The built-in PDF fonts cover Windows-1252 (Latin)
# characters; anything else (Chinese, emoji, arrows) would print as boxes.
_SWAP = {"→": "->", "←": "<-", "⇒": "->", "～": "~", "、": ", ", "。": ". ", "【": "[", "】": "]",
         "「": '"', "」": '"', "『": '"', "』": '"', "《": '"', "》": '"', "✓": "X", "✔": "X"}
_CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]")


def _latin(ch):
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def english(text, drop_lines=False):
    """Text safe for the BOL. drop_lines: free-text notes lose any line written
    in Chinese (a half-stripped sentence reads worse than none); names and
    addresses just lose the non-Latin characters."""
    text = str(text or "")
    if drop_lines:
        text = "\n".join(line for line in text.splitlines() if not _CJK.search(line))
    text = "".join(_SWAP.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKC", text)  # full-width ，：（） → ASCII
    text = "".join(ch for ch in text if ch == "\n" or _latin(ch))
    lines = [re.sub(r"[ \t]{2,}", " ", line).strip(" ·-/,;:|") for line in text.split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def company_info(project):
    """Name, address, phone and logo for the BOL header: ATS or PTT, from the project."""
    key = getattr(project, "company", None) or "ATS"
    info = dict(settings.TMS_DOC_COMPANIES.get(key) or settings.TMS_DOC_COMPANIES["ATS"])
    logo = info.get("logo") or ""
    candidates = [STATIC / logo] if Path(logo).suffix else [STATIC / f"{logo}.png", STATIC / f"{logo}.jpg"]
    info["logo_path"] = next((c for c in candidates if logo and c.exists()), None)
    return info


def esc(text):
    return english(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")


def P(text, style=s_value):
    return Paragraph(esc(text), style)


def box_style(extra=None):
    cmds = [
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    return TableStyle(cmds + (extra or []))


def party_block(title, loc):
    if loc is None:
        return [P(title, s_label), P("—")]
    lines = [P(title, s_label)]
    name = english(loc.name)
    if name:
        lines.append(P(name, s_value_b))
    lines.append(P(english(loc.full_address) or loc.code, s_value if name else s_value_b))
    contact = " · ".join(x for x in [english(loc.contact_name), english(loc.contact_phone)] if x)
    if contact:
        lines.append(P(f"Contact: {contact}", s_cell))
    hours = english(loc.hours, drop_lines=True)
    if hours:
        lines.append(P(f"Hours: {hours}", s_cell))
    return lines


def kv(label, value):
    return [P(label, s_label), P(value or "—", s_value_b)]


def fmt_num(d):
    if d is None:
        return ""
    d = Decimal(d)
    return f"{d:,.0f}" if d == d.to_integral() else f"{d:,.1f}"


def _appt(day, at, confirmed):
    text = " ".join(x for x in [day.strftime("%m/%d/%Y") if day else "", at.strftime("%H:%M") if at else ""] if x)
    if text and confirmed:
        text += " (confirmed)"
    return text


def delivery_notes(leg, shipment):
    """Lines for the pickup & delivery box, from what the TMS already knows."""
    out = []
    if leg:
        p = _appt(leg.scheduled_pickup, leg.pickup_appt, leg.pickup_appt_confirmed)
        if p:
            out.append(f"Pickup appt: {p}")
        d = _appt(leg.scheduled_delivery, leg.delivery_appt, leg.delivery_appt_confirmed)
        if d:
            out.append(f"Delivery appt: {d}")
    dest = (leg.destination if leg and leg.destination else None) or shipment.consignee
    if dest:
        contact = " ".join(x for x in [english(dest.contact_name), english(dest.contact_phone)] if x)
        if contact:
            out.append(f"Call consignee before arrival: {contact}")
        hours = english(dest.hours, drop_lines=True)
        if hours:
            out.append(f"Receiving hours: {hours}")
    if shipment.container_number and shipment.empty_return_location:
        ret = english(shipment.empty_return_location.name) or shipment.empty_return_location.code
        due = f" by {shipment.empty_return_due.strftime('%m/%d/%Y')}" if shipment.empty_return_due else ""
        out.append(f"Return empty to: {ret}{due}")
    return out


def bol_context(shipment, leg=None):
    """Resolve who/what/where for one BOL. A BOL belongs to one leg (one truck move)."""
    project = shipment.project
    return {
        "shipment": shipment,
        "project": project,
        "leg": leg,
        "bol_number": leg.effective_bol_number if leg else shipment.number,
        "ship_from": (leg.origin if leg and leg.origin else shipment.shipper),
        "ship_to": (leg.destination if leg and leg.destination else shipment.consignee),
        "carrier": leg.carrier if leg else None,
        "pickup_date": (leg.actual_pickup or leg.scheduled_pickup) if leg else None,
        "lines": list(shipment.cargo_lines.all()),
        "instructions": "\n\n".join(
            x for x in [
                english(leg.bol_instructions if leg else "", drop_lines=True),
                english(project.bol_instructions if project else "", drop_lines=True),
            ] if x
        ),
        "emergency": (project.emergency_contact if project else ""),
        "company": company_info(project),
        "delivery_notes": delivery_notes(leg, shipment),
    }


def build_page(ctx, blank_rows=3):
    sh = ctx["shipment"]
    leg = ctx["leg"]
    carrier = ctx["carrier"]
    story = []

    # --- Header ------------------------------------------------------------
    co = ctx["company"]
    logo = Image(str(co["logo_path"]), width=1.7 * inch, height=0.43 * inch) if co["logo_path"] else P(co["name"], s_title)
    company = " · ".join(x for x in [co["name"], co["address"], co["phone"]] if x)
    head = Table(
        [[
            logo,
            [P("BILL OF LADING", s_title), P("Straight Bill of Lading — Short Form — Not Negotiable", s_sub), P(company, s_sub)],
            [P("BOL Number", ParagraphStyle("r", parent=s_label, alignment=TA_RIGHT)), P(ctx["bol_number"], s_bolno),
             P(f"Date: {(ctx['pickup_date'] or timezone.localdate()).strftime('%m/%d/%Y')}", ParagraphStyle("rd", parent=s_cell, alignment=TA_RIGHT))],
        ]],
        colWidths=[1.9 * inch, W - 1.9 * inch - 2.0 * inch, 2.0 * inch],
    )
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 6)]

    # --- Parties + references -------------------------------------------
    left_w = W * 0.56
    right_w = W - left_w
    carrier_name = (carrier.name if carrier else "")
    refs = []
    refs.append(kv("Carrier", carrier_name))
    sub = []
    if carrier and carrier.scac:
        sub.append(f"SCAC {carrier.scac}")
    if leg and leg.carrier_load_number:
        sub.append(f"Load/PRO# {leg.carrier_load_number}")
    if sub:
        refs.append([P("", s_label), P(" · ".join(sub), s_cell)])
    equipment = " / ".join(x for x in [sh.get_equipment_display() if sh.equipment else "", leg.equipment if leg else ""] if x)
    refs += [
        kv("Driver / Phone", " · ".join(x for x in [english(leg.driver_name), english(leg.driver_phone)] if x) if leg else ""),
        kv("Truck / Trailer #", leg.truck_trailer if leg else ""),
        kv("Container #", sh.container_number),
        kv("Seal #", sh.seal_number),
        kv("Equipment", equipment),
        kv("Ocean B/L (MBL)", sh.master_bl.number if sh.master_bl else ""),
    ]
    refs_tbl = Table(refs, colWidths=[1.25 * inch, right_w - 1.25 * inch])
    refs_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))

    project = ctx["project"]
    ref2 = [
        kv("Shipment ID", sh.number),
        kv("Customer Ref / PO", sh.customer_ref),
        kv("Project", " · ".join(x for x in [project.number, english(project.name)] if x) if project else ""),
    ]
    if sh.units.exists():
        serials = ", ".join(u.serial_number for u in sh.units.all())
        ref2.append(kv("Serial No.", serials))
    ref2_tbl = Table(ref2, colWidths=[1.25 * inch, right_w - 1.25 * inch])
    ref2_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))

    ship_to = party_block("SHIP TO (CONSIGNEE)", ctx["ship_to"])

    bill_to = [P("THIRD PARTY FREIGHT CHARGES BILL TO", s_label), P(co["name"], s_value_b)]
    if co["address"]:
        bill_to.append(P(co["address"], s_cell))
    bill_to.append(P("Freight charge terms:  [ ] Prepaid   [ ] Collect   [X] 3rd Party", s_cell))

    parties = Table(
        [
            [party_block("SHIP FROM", ctx["ship_from"]), refs_tbl],
            [ship_to, ref2_tbl],
            [bill_to, [P("Emergency Response Phone (24 hr)", s_label), P(ctx["emergency"] or "—", s_value_b)]],
        ],
        colWidths=[left_w, right_w],
    )
    parties.setStyle(box_style())
    story += [parties, Spacer(1, 5)]

    # --- Driver / delivery instructions (always printed; blank lines to write on)
    blank = "_" * 52
    driver = [P(x, s_cell) for x in (ctx["instructions"] or "").split("\n") if x.strip()]
    delivery = [P(x, s_cell) for x in ctx["delivery_notes"]]
    rows_n = max(3, len(driver), len(delivery))
    driver += [P(blank, s_cell)] * (rows_n - len(driver))
    delivery += [P(blank, s_cell)] * (rows_n - len(delivery))
    t = Table([[P("DRIVER / SPECIAL INSTRUCTIONS", s_label), P("PICKUP & DELIVERY INSTRUCTIONS", s_label)], [driver, delivery]],
              colWidths=[W / 2, W / 2])
    t.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY)]))
    story += [t, Spacer(1, 5)]

    # --- Commodity table ----------------------------------------------------
    cols = [0.55, 0.7, 0.42, 0.33, 2.62, 1.2, 0.85, 0.55, 0.45]
    scale = W / (sum(cols) * inch)
    widths = [c * inch * scale for c in cols]
    rows = [[
        P("Handling Unit\nQty", s_head), P("Type", s_head), P("Pieces", s_head),
        P("HM\n(X)", s_head), P("Description of Articles\n(UN#, proper shipping name, hazard class, PG)", s_head),
        P("Dims L×W×H (in)", s_head), P("Weight (lbs)", s_head), P("NMFC", s_head), P("Class", s_head),
    ]]
    total_qty = 0
    total_pcs = 0
    total_wt = Decimal(0)
    any_hazmat = False
    for line in ctx["lines"]:
        any_hazmat = any_hazmat or line.hazmat
        total_qty += line.handling_qty or 0
        total_pcs += line.pieces or 0
        total_wt += line.weight_lbs or 0
        rows.append([
            P(f"{line.handling_qty}", s_cell_c),
            P(line.get_handling_type_display(), s_cell_c),
            P(line.pieces or "", s_cell_c),
            P("X" if line.hazmat else "", s_cell_c),
            P(line.bol_description, ParagraphStyle("d", parent=s_cell, fontName="Helvetica-Bold" if line.hazmat else "Helvetica")),
            P(line.dims, s_cell_c),
            P(fmt_num(line.weight_lbs), s_cell_r),
            P(line.nmfc, s_cell_c),
            P(line.freight_class, s_cell_c),
        ])
    for _ in range(blank_rows):
        rows.append([""] * 9)
    rows.append([
        P(str(total_qty), s_cell_c), P("", s_cell), P(str(total_pcs or ""), s_cell_c), P("", s_cell),
        P("GRAND TOTAL", ParagraphStyle("gt", parent=s_label, alignment=TA_RIGHT)), P("", s_cell),
        P(fmt_num(total_wt) if total_wt else "", ParagraphStyle("gtw", parent=s_cell_r, fontName="Helvetica-Bold")), "", "",
    ])
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(box_style([
        ("BACKGROUND", (0, 0), (-1, 0), GREY),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F2F2F2")),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white]),
    ]))
    story += [t, Spacer(1, 5)]

    # --- Loading & securement checkboxes -------------------------------------
    permits = "[X] Required" if leg and leg.permits_required else "[ ] Required"
    checks = Table([
        [P("TRAILER LOADED", s_label), P("FREIGHT COUNTED", s_label), P("SECUREMENT", s_label), P("PERMITS / ESCORTS", s_label)],
        [P("[ ] By shipper\n[ ] By driver", s_cell),
         P("[ ] By shipper\n[ ] By driver / pallets said to contain\n[ ] By driver / pieces", s_cell),
         P("[ ] Chains  [ ] Straps  [ ] Tarped\nTie-down points: ______", s_cell),
         P(f"{permits}\nPermit #: ____________", s_cell)],
    ], colWidths=[W * 0.2, W * 0.32, W * 0.26, W * 0.22])
    checks.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY)]))
    story += [checks, Spacer(1, 5)]

    # --- Driver time log (detention record) ----------------------------------
    def tm(dt):
        return timezone.localtime(dt).strftime("%m/%d/%Y %H:%M") if dt else "____/____  ____:____"

    log = Table([
        [P("DRIVER TIME LOG", s_label), P("Arrival (date / time)", s_head), P("Departure (date / time)", s_head),
         P("Waiting time", s_head), P("Site signature", s_head)],
        [P("At shipper / pickup", s_cell), P(tm(leg.pickup_arrived_at if leg else None), s_cell_c),
         P(tm(leg.pickup_departed_at if leg else None), s_cell_c), P("______ hrs", s_cell_c), P("", s_cell)],
        [P("At consignee / delivery", s_cell), P(tm(leg.delivery_arrived_at if leg else None), s_cell_c),
         P(tm(leg.delivery_departed_at if leg else None), s_cell_c), P("______ hrs", s_cell_c), P("", s_cell)],
    ], colWidths=[W * 0.22, W * 0.21, W * 0.21, W * 0.13, W * 0.23])
    log.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY), ("TOPPADDING", (0, 1), (-1, -1), 5), ("BOTTOMPADDING", (0, 1), (-1, -1), 5)]))
    story += [log, Spacer(1, 5)]

    # --- Legal / hazmat ---------------------------------------------------
    legal = [[P(LIABILITY_NOTE, ParagraphStyle("ln", parent=s_label, alignment=TA_CENTER))], [P(RECEIVED_CLAUSE, s_small)],
             [P(DECLARED_VALUE, s_small)]]
    if any_hazmat:
        legal.append([P(
            "HAZARDOUS MATERIALS: Shipment contains hazardous materials marked \"X\" above. "
            f"Emergency Response Phone: {ctx['emergency'] or '__________________'}", ParagraphStyle("hz", parent=s_label, fontSize=7.5)
        )])
    lt = Table(legal, colWidths=[W])
    lt.setStyle(box_style())
    story += [lt, Spacer(1, 5)]

    # --- Signatures --------------------------------------------------------
    sig_line = "\n\n\n______________________________"
    third = W / 3
    sigs = Table(
        [
            [P("SHIPPER SIGNATURE / DATE", s_label), P("CARRIER SIGNATURE / PICKUP DATE", s_label), P("CONSIGNEE SIGNATURE / DELIVERY DATE", s_label)],
            [
                [P(sig_line, s_cell), P("Print name: ____________________", s_cell), Spacer(1, 3), P(SHIPPER_CERT, s_small)],
                [P(sig_line, s_cell), P("Driver: ____________  Truck/Trailer: ________", s_cell),
                 P("Time in: ________  Time out: ________", s_cell), Spacer(1, 3), P(CARRIER_ACK, s_small)],
                [P(sig_line, s_cell), P("Print name: ____________________", s_cell),
                 P("Time in: ________  Time out: ________", s_cell), Spacer(1, 3), P(CONSIGNEE_ACK, s_small),
                 P("Exceptions / damage noted: ______________________", s_cell)],
            ],
        ],
        colWidths=[third, third, third],
    )
    sigs.setStyle(box_style([("BACKGROUND", (0, 0), (-1, 0), GREY)]))
    story.append(sigs)
    return story


def _height(flowables, width, height):
    return sum(f.wrap(width, height)[1] + f.getSpaceBefore() + f.getSpaceAfter() for f in flowables)


def fit_page(ctx, width, height):
    """One BOL that fills a full page: pad the commodity table with blank rows
    until the next row would no longer fit. Long cargo lists still flow on."""
    avail = height - 14  # frame padding
    page = build_page(ctx, 1)
    if _height(page, width, avail) > avail:
        return page
    for rows in range(2, 40):
        trial = build_page(ctx, rows)
        if _height(trial, width, avail) > avail:
            break
        page = trial
    return page


def render_bols(contexts):
    """contexts: list of bol_context() dicts → PDF bytes, one page (or more) per BOL."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER, leftMargin=0.4 * inch, rightMargin=0.4 * inch, topMargin=0.4 * inch, bottomMargin=0.4 * inch,
        title="Bill of Lading", author=(contexts[0]["company"]["name"] if contexts else settings.TMS_COMPANY_NAME),
    )
    story = []
    for i, ctx in enumerate(contexts):
        if i:
            story.append(PageBreak())
        story += fit_page(ctx, doc.width, doc.height)
    doc.build(story)
    return buf.getvalue()
