"""Inquiries and quotes: scope screening, RFQ e-mail drafts, customer quotes, and
turning a won inquiry into a project.

Nothing here sends anything. E-mails are drafts that a person copies or opens
in Gmail and sends themselves.
"""
from decimal import ROUND_CEILING, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import units
from .models import (CargoLine, Carrier, CarrierQuote, Charge, Customer, CustomerQuote, CustomerQuoteLine, Lead, Leg, Location,
                     Note, Project, Shipment)

# ---------------------------------------------------------------------------
# OOG first screen. Rules of thumb only: legal limits differ by state and
# route, so these are flags for a person to check, never a permit decision.
# ---------------------------------------------------------------------------
LEGAL_WIDTH = Decimal("8.5")      # 8'6" (102 in), federal
LEGAL_HEIGHT = Decimal("13.5")    # 13'6" overall in most states (some allow 14')
LEGAL_LENGTH = Decimal("53")      # 53' trailer
GROSS_LIMIT = 80000               # lbs, federal interstate gross
# Approximate deck heights and legal payloads, for the equipment suggestion.
DECKS = [("Flatbed", Decimal("5"), 48000), ("Step Deck", Decimal("3.5"), 46000), ("RGN / Lowboy", Decimal("2"), 42000)]


def ft_in(v):
    """Decimal feet -> 13'6" (what carriers read)."""
    if v in (None, ""):
        return ""
    inches = int((Decimal(v) * 12).to_integral_value())
    return f"{inches // 12}'{inches % 12}\"" if inches % 12 else f"{inches // 12}'"


def screen_cargo(c):
    """(flags, suggested equipment) for one LeadCargo. Flags are Chinese, for the team."""
    flags = []
    w, h, length, wt = c.width_ft, c.height_ft, c.length_ft, c.weight_lbs
    if w and w > LEGAL_WIDTH:
        flags.append(f"超宽 {ft_in(w)}（> 8'6\"）：需要 oversize permit")
        if w > 12:
            flags.append("宽度 > 12'：多数州需要护送车 escort，部分州还要路线勘察")
    if length and length > LEGAL_LENGTH:
        flags.append(f"超长 {ft_in(length)}（> 53'）：需要加长板 / permit")
    suggestion = ""
    if h or wt:
        for name, deck, payload in DECKS:
            if (not h or h + deck <= LEGAL_HEIGHT) and (not wt or wt <= payload):
                suggestion = name
                break
        if not suggestion:
            suggestion = "多轴 RGN / 特种车"
            if h and h + DECKS[-1][1] > LEGAL_HEIGHT:
                flags.append(f"超高：货高 {ft_in(h)}，放在低平板上总高也超过 13'6\"，需要 overheight permit 和路线勘察")
            if wt and wt > DECKS[-1][2]:
                flags.append(f"超重：单件 {units.plain(wt)} lbs，超出普通低平板合法载重，需要多轴车和 overweight permit（总重 > 80,000 lbs）")
    if c.hazmat:
        flags.append("危险品：需要有 Hazmat 资质的承运商")
    return flags, suggestion


def screen_lead(lead):
    """[(cargo, flags, suggestion)] for the scope."""
    return [(c, *screen_cargo(c)) for c in lead.cargo.all()]


# ---------------------------------------------------------------------------
# E-mail drafts (English)
# ---------------------------------------------------------------------------
def _cargo_line(c):
    dims = " x ".join(ft_in(v) for v in (c.length_ft, c.width_ft, c.height_ft) if v)
    parts = [f"{c.qty} x {c.description}"]
    if dims:
        parts.append(f"{dims} (L x W x H)")
    if c.weight_lbs:
        parts.append(f"{float(c.weight_lbs):,.0f} lbs each" if c.qty > 1 else f"{float(c.weight_lbs):,.0f} lbs")
    if c.hazmat:
        parts.append("HAZMAT")
    return " - ".join(parts)


def _date(d):
    return d.strftime("%m/%d/%Y") if d else "TBD"


def _company_name(lead):
    return (settings.TMS_DOC_COMPANIES.get(lead.company) or settings.TMS_DOC_COMPANIES["ATS"])["name"]


def _signature(user, lead):
    name = (user.get_full_name() or user.email) if user else ""
    return "\n".join(x for x in ["Thank you,", name, _company_name(lead)] if x)


def scope_lines(lead, lanes=None):
    """The scope in plain English, lane by lane, without the customer's name."""
    lanes = list(lanes if lanes is not None else lead.lanes.all())
    all_cargo = list(lead.cargo.all())
    out = []
    for i, lane in enumerate(lanes, 1):
        head = f"Lane {i}: {lane.origin}{' ' + lane.origin_zip if lane.origin_zip else ''} -> {lane.destination}{' ' + lane.destination_zip if lane.destination_zip else ''}"
        out.append(head)
        eq = lane.equipment or lane.get_service_display()
        out.append(f"  - Equipment: {eq}; trucks / units: {lane.trucks}")
        out.append(f"  - Ready: {_date(lane.ready_date)}; deliver by: {_date(lane.deliver_by)}")
        cargo = [c for c in all_cargo if c.lane_id == lane.pk] or ([c for c in all_cargo if not c.lane_id] if len(lanes) == 1 else [])
        for c in cargo:
            out.append(f"  - Commodity: {_cargo_line(c)}")
        if lane.notes.strip():
            out.append(f"  - Notes: {lane.notes.strip()}")
        out.append("")
    loose = [c for c in all_cargo if not c.lane_id] if len(lanes) != 1 else []
    if loose:
        out.append("Commodities:")
        out += [f"  - {_cargo_line(c)}" for c in loose]
        out.append("")
    return "\n".join(out).rstrip()


def rfq_email(lead, user=None, lanes=None, contact_name=""):
    """(subject, body) asking carriers for a rate. The customer's name is never included."""
    lanes = list(lanes if lanes is not None else lead.lanes.all())
    first = lanes[0] if lanes else None
    eq = (first.equipment or first.get_service_display()) if first else ""
    route = f"{first.origin} -> {first.destination}" if first else lead.title
    subject = f"RFQ {lead.number}: {route}" + (f" | {eq}" if eq else "") + (f" (+{len(lanes) - 1} more)" if len(lanes) > 1 else "")
    body = "\n".join([
        f"Hi {contact_name or 'team'},",
        "",
        "Please quote the following shipment:",
        "",
        scope_lines(lead, lanes) or "(scope to follow)",
        "",
        "Please include in your quote:",
        "  - All-in rate per truck (linehaul + fuel), and any permit / escort / tarp costs",
        "  - Accessorials: detention, layover, TONU",
        "  - Equipment type, transit time and quote validity",
        "",
        f"Reference: {lead.number}",
        "",
        _signature(user, lead),
    ])
    return subject[:200], body


def money(v):
    return f"${Decimal(v):,.2f}"


def customer_email(quote, user=None):
    """(subject, body) for the customer, from a customer quote."""
    lead = quote.lead
    subject = f"Quotation {quote.number} - {lead.title}"
    lines = [f"Dear {lead.contact_name or 'Sir / Madam'},", ""]
    lines.append(quote.intro.strip() or f"Thank you for your inquiry. Please find our quotation for {lead.title} below.")
    lines.append("")
    section = None
    for ln in quote.lines.all():
        if ln.section != section:
            section = ln.section
            if section:
                lines += ["", section]
        qty = f"{units.plain(ln.quantity)} x " if ln.quantity != 1 else ""
        tail = " (if applicable)" if ln.is_optional else ""
        lines.append(f"  - {ln.description}: {qty}{money(ln.unit_price)} {ln.get_unit_display()}{tail}")
    if quote.show_pdf_totals:
        lines += ["", f"Total: {money(quote.total)} (excluding items marked 'if applicable')"]
    if quote.valid_until:
        lines += ["", f"This quotation is valid until {_date(quote.valid_until)}."]
    if quote.assumptions.strip():
        lines += ["", "Assumptions / exclusions:"] + [f"  - {x.strip()}" for x in quote.assumptions.splitlines() if x.strip()]
    if quote.terms.strip():
        lines += ["", "Terms:"] + [f"  - {x.strip()}" for x in quote.terms.splitlines() if x.strip()]
    lines += ["", "Please let us know if you have any questions.", "", _signature(user, lead)]
    return subject[:200], "\n".join(lines)


def gmail_compose_url(to, subject, body):
    """Opens a pre-filled Gmail draft window. A person still presses Send."""
    from urllib.parse import urlencode

    return "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": to or "", "su": subject, "body": body[:6000]})


# ---------------------------------------------------------------------------
# Customer quote from carrier costs
# ---------------------------------------------------------------------------
DEFAULT_ASSUMPTIONS = (
    "Rates are based on the dimensions, weights and dates provided; changes may affect pricing.\n"
    "Permits and escorts are quoted for the route as currently planned; state requirements may change.\n"
    "Accessorial charges (detention, layover, storage) apply only if incurred.\n"
    "Subject to equipment availability at time of booking."
)


def _round_up(v, step=5):
    return (Decimal(v) / step).to_integral_value(rounding=ROUND_CEILING) * step


def chosen_quotes(lead):
    """The carrier quote used for each lane: the one marked 已选用, else the cheapest that has a price."""
    out = {}
    quotes = [q for q in lead.carrier_quotes.prefetch_related("lines").all() if q.status in (CarrierQuote.Status.SELECTED, CarrierQuote.Status.QUOTED)]
    for q in quotes:
        key = q.lane_id
        cur = out.get(key)
        rank = (q.status != CarrierQuote.Status.SELECTED, _per_unit_total(q))
        if cur is None or rank < (cur.status != CarrierQuote.Status.SELECTED, _per_unit_total(cur)):
            out[key] = q
    return out


def _per_unit_total(q):
    t = q.total
    return t if t > 0 else Decimal("Infinity")


@transaction.atomic
def build_customer_quote(lead, markup_pct=Decimal("15")):
    """A new draft customer quote, priced at chosen carrier cost + markup (rounded up to $5)."""
    m = Decimal(1) + Decimal(markup_pct) / 100
    version = (lead.customer_quotes.order_by("-version").values_list("version", flat=True).first() or 0) + 1
    lead.customer_quotes.filter(status__in=[CustomerQuote.Status.DRAFT, CustomerQuote.Status.SENT]).update(
        status=CustomerQuote.Status.SUPERSEDED)
    q = CustomerQuote.objects.create(
        lead=lead, version=version, valid_until=timezone.localdate() + timezone.timedelta(days=14),
        assumptions=DEFAULT_ASSUMPTIONS, style=CustomerQuote.Style.FORMAL if lead.lanes.count() > 1 else CustomerQuote.Style.SIMPLE)
    lanes = list(lead.lanes.all())
    chosen = chosen_quotes(lead)
    pos = 0
    for i, lane in enumerate(lanes, 1):
        section = f"Lane {i}: {lane.origin} -> {lane.destination}" if len(lanes) > 1 else ""
        cq = chosen.get(lane.pk) or (chosen.get(None) if len(lanes) == 1 else None)
        if cq is None:
            pos += 1
            CustomerQuoteLine.objects.create(
                quote=q, section=section, lane=lane, charge_type=Charge.Type.LINEHAUL, position=pos,
                description=f"Transportation - {lane.equipment or lane.get_service_display()}",
                quantity=lane.trucks, unit=CustomerQuoteLine.Unit.TRUCK)
            continue
        for ln in cq.lines.all():
            pos += 1
            per_truck = cq.per_truck and not ln.is_estimate
            CustomerQuoteLine.objects.create(
                quote=q, section=section, lane=lane, charge_type=ln.charge_type, position=pos,
                description=ln.description or ln.get_charge_type_display(),
                quantity=(ln.quantity or 1) * (lane.trucks if per_truck else 1),
                unit=CustomerQuoteLine.Unit.TRUCK if per_truck else (CustomerQuoteLine.Unit.EACH if ln.is_estimate else CustomerQuoteLine.Unit.LUMP),
                unit_cost=ln.unit_price, unit_price=_round_up(ln.unit_price * m), is_optional=ln.is_estimate)
    # Whole-project carrier quotes (no lane) when there are several lanes.
    if len(lanes) != 1 and chosen.get(None):
        for ln in chosen[None].lines.all():
            pos += 1
            CustomerQuoteLine.objects.create(
                quote=q, section="Project", charge_type=ln.charge_type, position=pos,
                description=ln.description or ln.get_charge_type_display(), quantity=ln.quantity or 1,
                unit=CustomerQuoteLine.Unit.LUMP, unit_cost=ln.unit_price, unit_price=_round_up(ln.unit_price * m),
                is_optional=ln.is_estimate)
    return q


# ---------------------------------------------------------------------------
# Won: inquiry -> project + loads
# ---------------------------------------------------------------------------
def _location(text, zip_code=""):
    text = (text or "").strip()
    if not text:
        return None
    code = text[:60]
    loc = Location.all_objects.filter(code=code).first()
    if loc is None:
        loc = Location.objects.create(code=code, name=text[:160], postal_code=zip_code or "")
    return loc


def _carrier(q):
    if q is None:
        return None
    if q.carrier_id:
        return q.carrier
    name = (q.carrier_name or "").strip()
    if not name:
        return None
    return Carrier.objects.filter(name=name).first() or Carrier.objects.create(name=name, contact_email=q.contact_email[:254] if "@" in q.contact_email else "")


def _split(total_qty, trucks):
    """How many pieces go on each truck."""
    if trucks <= 1:
        return [total_qty]
    base, extra = divmod(total_qty, trucks)
    return [base + (1 if i < extra else 0) for i in range(trucks)]


@transaction.atomic
def convert(lead, user, owner=None, project_name="", carrier_quotes=None, customer_quote=None):
    """Create the project, its Loads (one per truck), legs with the chosen carrier, AP from the carrier quote
    and AR from the customer quote. Returns the Project."""
    if lead.project_id:
        raise ValueError("这个询价已经转成项目了")
    customer = lead.customer
    if customer is None:
        name = (lead.prospect_name or "").strip()
        if not name:
            raise ValueError("请先填写客户")
        customer = Customer.objects.filter(name=name).first() or Customer.objects.create(
            name=name, contact_name=lead.contact_name, contact_email=lead.contact_email if "@" in lead.contact_email else "",
            contact_phone=lead.contact_phone[:60])
        lead.customer = customer
    lanes = list(lead.lanes.all())
    if not lanes:
        raise ValueError("请先在需求里加至少一段运输")
    carrier_quotes = carrier_quotes if carrier_quotes is not None else chosen_quotes(lead)
    cargo = list(lead.cargo.all())
    first_cargo = cargo[0].description if cargo else ""
    project = Project.objects.create(
        company=lead.company, customer=customer, name=(project_name or lead.title)[:160],
        start_date=min((ln.ready_date for ln in lanes if ln.ready_date), default=None),
        cargo_description=first_cargo[:255], notes=lead.summary, terms=(customer_quote.terms if customer_quote else ""),
        default_shipper=_location(lanes[0].origin, lanes[0].origin_zip),
        default_consignee=_location(lanes[-1].destination, lanes[-1].destination_zip),
    )
    project.source_ref = f"询价 {lead.number}"
    project.save(update_fields=["source_ref"])
    if owner:
        project.owners.add(owner)
    loads_by_lane = {}
    for lane in lanes:
        origin, dest = _location(lane.origin, lane.origin_zip), _location(lane.destination, lane.destination_zip)
        lane_cargo = [c for c in cargo if c.lane_id == lane.pk] or ([c for c in cargo if not c.lane_id] if len(lanes) == 1 else [])
        cq = carrier_quotes.get(lane.pk) or (carrier_quotes.get(None) if len(lanes) == 1 else None)
        carrier = _carrier(cq)
        flags = any(screen_cargo(c)[0] for c in lane_cargo)
        splits = {c.pk: _split(c.qty, lane.trucks) for c in lane_cargo}
        loads = []
        for i in range(max(lane.trucks, 1)):
            eq = lane.equipment.upper().replace("'", "").replace(" ", "")
            s = Shipment.objects.create(
                customer=customer, project=project, service=lane.service, shipper=origin, consignee=dest,
                equipment=eq if eq in Shipment.Equipment.values else "", customer_ref=lead.customer_ref,
                notes=(lane.notes or "")[:2000], source_ref=f"询价 {lead.number}")
            for c in lane_cargo:
                n = splits[c.pk][i] if i < len(splits[c.pk]) else 0
                if n:
                    CargoLine.objects.create(
                        shipment=s, description=c.description, handling_qty=n, handling_type=c.handling_type,
                        weight_lbs=(c.weight_lbs * n) if c.weight_lbs else None, length_ft=c.length_ft, width_ft=c.width_ft,
                        height_ft=c.height_ft, hazmat=c.hazmat)
            if carrier or lane.ready_date or lane.deliver_by:
                Leg.objects.create(
                    shipment=s, sequence=1, kind=Leg.Kind.DRAYAGE if lane.service.startswith("DRAY") else Leg.Kind.OTR,
                    origin=origin, destination=dest, carrier=carrier, equipment=(cq.equipment if cq and cq.equipment else lane.equipment)[:60],
                    scheduled_pickup=lane.ready_date, scheduled_delivery=lane.deliver_by, permits_required=flags)
            loads.append(s)
        loads_by_lane[lane.pk] = loads
        if cq is not None:  # AP: per truck on every load, or lump sum on the first one
            for ln in cq.lines.all():
                if ln.is_estimate:
                    continue
                targets = loads if cq.per_truck else loads[:1]
                for s in targets:
                    Charge.objects.create(shipment=s, side=Charge.Side.AP, charge_type=ln.charge_type, carrier=carrier,
                                          description=(ln.description or "")[:255], quantity=ln.quantity or 1, unit_price=ln.unit_price,
                                          notes=f"询价 {lead.number} 承运商报价")
            cq.status = CarrierQuote.Status.SELECTED
            cq.save(update_fields=["status"])
    if customer_quote is not None:  # AR: per-truck lines spread over the lane's loads, the rest on the first load
        first_load = next((ls[0] for ls in loads_by_lane.values() if ls), None)
        per_unit = (CustomerQuoteLine.Unit.TRUCK, CustomerQuoteLine.Unit.LOAD, CustomerQuoteLine.Unit.CNTR)
        for ln in customer_quote.lines.all():
            if ln.is_optional:
                continue
            loads = loads_by_lane.get(ln.lane_id) or []
            if ln.unit in per_unit and loads and ln.quantity == len(loads):
                for s in loads:
                    Charge.objects.create(shipment=s, side=Charge.Side.AR, charge_type=ln.charge_type, description=ln.description[:255],
                                          quantity=1, unit_price=ln.unit_price, notes=f"报价 {customer_quote.number}")
            elif loads or first_load:
                Charge.objects.create(shipment=(loads or [first_load])[0], side=Charge.Side.AR, charge_type=ln.charge_type,
                                      description=ln.description[:255], quantity=ln.quantity, unit_price=ln.unit_price,
                                      notes=f"报价 {customer_quote.number}")
        customer_quote.status = CustomerQuote.Status.ACCEPTED
        customer_quote.save(update_fields=["status"])
    for loads in loads_by_lane.values():
        for s in loads:
            s.refresh_status()
    project.refresh_status()
    lead.status, lead.project = Lead.Status.WON, project
    lead.save()
    n = sum(len(v) for v in loads_by_lane.values())
    Note.objects.create(project=project, created_by=user, kind=Note.Kind.SYSTEM,
                        text=f"由询价 {lead.number} 成单转入：{n} 个 Load" + (f"，负责人 {owner.get_full_name() or owner.email}" if owner else ""))
    return project


def margin(quote):
    """(sell, cost, margin, margin %) of a customer quote, excluding optional lines."""
    sell, cost = quote.total, quote.cost_total
    pct = (sell - cost) / sell * 100 if sell else Decimal(0)
    return sell, cost, sell - cost, pct.quantize(Decimal("0.1"))

