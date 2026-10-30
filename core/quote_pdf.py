"""Customer quotation PDF, English only, in the same style as the BOL.

Two styles:
  SIMPLE  one price table, assumptions, terms
  FORMAL  adds Scope of Work, priced sections with subtotals, optional items
          and an acceptance block for the customer to sign
"""
from decimal import Decimal
from io import BytesIO

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import quotes, units
from .bol import GREY, W, company_info, english, logo_image, esc, s_base, s_cell, s_label, s_small, s_title, s_value, s_value_b

s_h = ParagraphStyle("qh", parent=s_base, fontName="Helvetica-Bold", fontSize=10.5, leading=13, spaceBefore=8, spaceAfter=3)
s_r = ParagraphStyle("qr", parent=s_cell, alignment=TA_RIGHT)
s_rb = ParagraphStyle("qrb", parent=s_r, fontName="Helvetica-Bold")
s_body = ParagraphStyle("qbody", parent=s_base, fontSize=9, leading=12)


def P(text, style=s_value):
    return Paragraph(esc(text), style)


def _money(v):
    return f"${Decimal(v):,.2f}"


def _bullets(text):
    return [P(f"- {english(x)}", s_body) for x in (text or "").splitlines() if english(x)]


class _Doc(SimpleDocTemplate):
    number = ""

    def afterPage(self):
        c = self.canv
        c.saveState()
        c.setFont("Helvetica", 7)
        c.drawRightString(self.pagesize[0] - self.rightMargin, self.bottomMargin * 0.45, f"Quotation {self.number}  ·  Page {c.getPageNumber()}")
        c.restoreState()


def render(quote):
    lead = quote.lead
    co = company_info(type("P", (), {"company": lead.company})())
    buf = BytesIO()
    doc = _Doc(buf, pagesize=LETTER, leftMargin=0.5 * inch, rightMargin=0.5 * inch, topMargin=0.45 * inch, bottomMargin=0.5 * inch,
               title=f"Quotation {quote.number}", author=co["name"])
    doc.number = quote.number
    width = doc.width
    story = []

    logo = logo_image(co["logo_path"]) if co["logo_path"] else P(co["name"], s_title)
    contact = " · ".join(x for x in [co["address"], co["phone"]] if x)
    head = Table([[
        [logo, P(co["name"], s_label), P(contact, s_small)] if contact else [logo, P(co["name"], s_label)],
        [P("QUOTATION", ParagraphStyle("qt", parent=s_title, alignment=TA_RIGHT)),
         P(f"No. {quote.number}", s_rb),
         P(f"Date: {timezone.localdate().strftime('%m/%d/%Y')}", s_r),
         P(f"Valid until: {quote.valid_until.strftime('%m/%d/%Y')}" if quote.valid_until else "", s_r)],
    ]], colWidths=[width * 0.55, width * 0.45])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 10)]

    to = [P("PREPARED FOR", s_label), P(english(lead.customer_label) or "-", s_value_b)]
    if english(lead.contact_name):
        to.append(P(f"Attn: {english(lead.contact_name)}", s_cell))
    if english(lead.contact_email):
        to.append(P(english(lead.contact_email), s_cell))
    re_ = [P("RE", s_label), P(english(lead.title) or lead.number, s_value_b)]
    if english(lead.customer_ref):
        re_.append(P(f"Your ref: {english(lead.customer_ref)}", s_cell))
    box = Table([[to, re_]], colWidths=[width / 2, width / 2])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black), ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.black),
                             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 4),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story += [box, Spacer(1, 8)]

    intro = english(quote.intro) or f"Thank you for your inquiry. We are pleased to quote {english(lead.title) or 'the shipment'} as follows."
    story.append(P(intro, s_body))

    formal = quote.style == quote.Style.FORMAL
    if formal:
        story.append(P("SCOPE OF WORK", s_h))
        scope = english(quote.scope_text) or english(quotes.scope_lines(lead))
        story += [P(x.strip() and x or " ", s_body) for x in scope.splitlines()] or [P("-", s_body)]

    story.append(P("PRICING", s_h))
    lines = [ln for ln in quote.lines.all()]
    main = [ln for ln in lines if not ln.is_optional]
    optional = [ln for ln in lines if ln.is_optional]
    cols = [width * 0.50, width * 0.12, width * 0.14, width * 0.12, width * 0.12]
    rows = [[P("Description", s_label), P("Qty", s_label), P("Unit", s_label), P("Rate", ParagraphStyle("l", parent=s_label, alignment=TA_RIGHT)),
             P("Amount", ParagraphStyle("l2", parent=s_label, alignment=TA_RIGHT))]]
    styles = [("BACKGROUND", (0, 0), (-1, 0), GREY)]
    section, sub = None, Decimal(0)

    def close_section():
        if section and formal and quote.show_pdf_totals:
            rows.append([P(f"Subtotal - {english(section)}", s_rb), "", "", "", P(_money(sub), s_rb)])

    for ln in main:
        if ln.section != section:
            close_section()
            section, sub = ln.section, Decimal(0)
            if section:
                rows.append([P(english(section), s_value_b), "", "", "", ""])
                styles.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), colors.HexColor("#F2F2F2")))
                styles.append(("SPAN", (0, len(rows) - 1), (-1, len(rows) - 1)))
        sub += ln.amount
        rows.append([P(english(ln.description), s_cell), P(units.plain(ln.quantity), s_cell), P(ln.get_unit_display(), s_cell),
                     P(_money(ln.unit_price), s_r), P(_money(ln.amount), s_r)])
    close_section()
    if quote.show_pdf_totals:
        rows.append([P("TOTAL (USD)", s_rb), "", "", "", P(_money(quote.total), s_rb)])
        styles.append(("LINEABOVE", (0, len(rows) - 1), (-1, len(rows) - 1), 1, colors.black))
    t = Table(rows, colWidths=cols, repeatRows=1)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black), ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#BBBBBB")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
                          + styles))
    story.append(t)

    if optional:
        story.append(P("ADDITIONAL CHARGES (IF APPLICABLE)", s_h))
        orow = [[P(english(ln.description), s_cell), P(f"{_money(ln.unit_price)} {ln.get_unit_display()}", s_r)] for ln in optional]
        ot = Table(orow, colWidths=[width * 0.7, width * 0.3])
        ot.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")), ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
        story.append(ot)

    if english(quote.assumptions):
        story += [P("ASSUMPTIONS / EXCLUSIONS", s_h)] + _bullets(quote.assumptions)
    if english(quote.terms):
        story += [P("TERMS", s_h)] + _bullets(quote.terms)

    if formal:
        sign = Table([
            [P("ACCEPTED BY CUSTOMER", s_label), P(f"FOR {english(co['name']).upper()}", s_label)],
            [P("\n\nSignature: ____________________________\nName / Title: _________________________\nDate: _______________", s_cell),
             P("\n\nSignature: ____________________________\nName / Title: _________________________\nDate: _______________", s_cell)],
        ], colWidths=[width / 2, width / 2])
        sign.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black), ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.black),
                                  ("BACKGROUND", (0, 0), (-1, 0), GREY), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [Spacer(1, 12), KeepTogether([P("Please sign and return to confirm this quotation.", s_body), Spacer(1, 4), sign])]
    doc.build(story)
    return buf.getvalue()
