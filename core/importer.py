"""Import projects/loads from a JSON file (e.g. exported from Notion).

Shape (every key optional except where noted):

{
  "customers": [{"name": "Hithium", "code": "HTH"}],
  "carriers":  [{"name": "Harris Brokerage", "code": "Harris", "kind": "BROKER", ...}],
  "locations": [{"code": "KHE Carson", "name": "...", "kind": "YARD", "address": "...", "city": "...", ...}],
  "projects": [{
      "name": "PTT#4475 ...",            # required
      "customer": "Hithium",             # required, by name
      "company": "PTT", "monday_item_id": "P-04475", "start_date": "2026-09-28", "end_date": "...",
      "manual_status": "DONE" | "CANCELLED" | "", "owners": ["someone@company.com"],
      "default_shipper": "<location code>", "default_consignee": "<location code>",
      "cargo_description": "...", "un_number": "", "hazard_class": "", "emergency_contact": "",
      "detention_free_hours": 2, "detention_rate": 150, "terms": "...", "notes": "...", "source": "...",
      "milestones": [{"name": "Test Run", "planned_date": "...", "done_date": "..."}],
      "timeline": [{"date": "2026-10-11", "text": "..."}],
      "loads": [{
          "container_number": "SHPU2204302", "equipment": "20SOC", "service": "OTR", "customer_ref": "...",
          "mbl": "SHZ8660350", "mbl_eta": "2026-11-08",
          "shipper": "<code>", "consignee": "<code>", "pod_received": "2026-10-16", "pod_check": "OK", "notes": "...",
          "cargo":   [{"handling_qty": 1, "handling_type": "UNIT", "description": "...", "weight_lbs": 88185}],
          "units":   [{"serial_number": "...", "product": "...", "config": "A"}],
          "legs":    [{"sequence": 1, "kind": "OTR", "origin": "<code>", "destination": "<code>", "carrier": "<name>",
                       "carrier_load_number": "BLN30784", "driver_name": "...", "truck_trailer": "...",
                       "scheduled_pickup": "...", "actual_pickup": "...", "scheduled_delivery": "...", "actual_delivery": "...",
                       "pickup_departed_at": "2026-10-09T16:40:00-04:00", "delivery_arrived_at": "...", "delivery_departed_at": "...",
                       "notes": "..."}],
          "charges": [{"side": "AP", "charge_type": "LINEHAUL", "unit_price": 24000, "carrier": "<name>",
                       "invoice_number": "", "state": "OPEN", "description": "..."}],
          "timeline": [{"date": "2026-10-13", "text": "..."}]
      }]
  }]
}

Re-importing is safe: a project that already exists (same customer + name) is skipped.
"""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import (
    CargoLine,
    Carrier,
    Charge,
    Customer,
    Leg,
    Location,
    MasterBL,
    Milestone,
    Note,
    Project,
    Sequence,
    Shipment,
    Unit,
    YardStay,
)


class ImportError_(Exception):
    pass


def _d(v):
    if not v:
        return None
    if isinstance(v, date):
        return v
    d = parse_date(str(v)[:10])
    if d is None:
        raise ImportError_(f"日期格式不对：{v}（应为 YYYY-MM-DD）")
    return d


def _dt(v):
    if not v:
        return None
    dt = parse_datetime(str(v))
    if dt is None:
        raise ImportError_(f"时间格式不对：{v}（应为 ISO 格式，如 2026-10-13T09:20:00-04:00）")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _money(v):
    return None if v in (None, "") else Decimal(str(v))


def _pick(d, keys):
    return {k: d[k] for k in keys if k in d and d[k] is not None}


def wipe_operational_data():
    """Delete every customer/project/load and their history. Users and settings stay."""
    models = [Note, Charge, YardStay, Leg, Unit, CargoLine, Shipment, MasterBL, Milestone, Project, Customer, Carrier, Location]
    n = 0
    for m in models:
        mgr = getattr(m, "all_objects", m.objects)
        n += mgr.all().count()
        mgr.all().delete()
        if hasattr(m, "history"):
            m.history.all().delete()
    Sequence.objects.filter(key__startswith="SHP").delete()
    Sequence.objects.filter(key="PRJ").delete()
    return f"已清空旧数据：{n} 条记录"


def import_data(data, user=None, dry_run=False):
    if not isinstance(data, dict):
        raise ImportError_("文件内容应是一个 JSON 对象")
    log = []
    with transaction.atomic():
        log += _import(data, user)
        if dry_run:
            transaction.set_rollback(True)
            log.append("（只预览：以上内容没有写入）")
    return log


def _import(data, user):
    log = []
    User = get_user_model()
    for c in data.get("customers", []):
        Customer.objects.get_or_create(name=c["name"], defaults=_pick(c, ["code", "contact_name", "contact_phone", "contact_email", "notes"]))
    for c in data.get("carriers", []):
        Carrier.objects.get_or_create(name=c["name"], defaults=_pick(c, ["code", "kind", "scac", "mc_number", "dot_number", "contact_name",
                                                                         "contact_phone", "contact_email", "dispatch_notes"]))
    for loc in data.get("locations", []):
        Location.objects.get_or_create(code=loc["code"], defaults=_pick(loc, ["name", "kind", "address", "city", "state", "postal_code", "gps",
                                                                              "contact_name", "contact_phone", "hours", "notes"]))

    def location(code):
        if not code:
            return None
        loc = Location.objects.filter(code=code).first()
        if not loc:
            raise ImportError_(f"地址库里找不到代号「{code}」，请先在 locations 里定义")
        return loc

    def carrier(name):
        if not name:
            return None
        return Carrier.objects.filter(name=name).first() or Carrier.objects.get_or_create(name=name)[0]

    for pd in data.get("projects", []):
        if not pd.get("name") or not pd.get("customer"):
            raise ImportError_("每个项目都必须有 name 和 customer")
        customer = Customer.objects.get_or_create(name=pd["customer"])[0]
        if Project.objects.filter(customer=customer, name=pd["name"]).exists():
            log.append(f"跳过已存在的项目：{pd['name']}")
            continue
        p = Project.objects.create(
            customer=customer, name=pd["name"], company=pd.get("company", "ATS"), monday_item_id=pd.get("monday_item_id", ""),
            start_date=_d(pd.get("start_date")), end_date=_d(pd.get("end_date")), manual_status=pd.get("manual_status", ""),
            default_shipper=location(pd.get("default_shipper")), default_consignee=location(pd.get("default_consignee")),
            cargo_description=pd.get("cargo_description", ""), un_number=pd.get("un_number", ""), hazard_class=pd.get("hazard_class", ""),
            emergency_contact=pd.get("emergency_contact", ""), bol_instructions=pd.get("bol_instructions", ""),
            detention_free_hours=_money(pd.get("detention_free_hours")) or Decimal("2"), detention_rate=_money(pd.get("detention_rate")),
            terms=pd.get("terms", ""), notes=pd.get("notes", ""), source_ref=pd.get("source", "")[:255],
        )
        for email in pd.get("owners", []):
            u = User.objects.filter(email__iexact=email).first()
            if u:
                p.owners.add(u)
            else:
                log.append(f"  负责人 {email} 还没登录过系统，先跳过（登录后在项目里指定）")
        for m in pd.get("milestones", []):
            Milestone.objects.create(project=p, name=m["name"], planned_date=_d(m.get("planned_date")), done_date=_d(m.get("done_date")))
        for t in pd.get("timeline", []):
            n = Note.objects.create(project=p, kind=Note.Kind.NOTE, text=t["text"], created_by=user)
            if t.get("date"):
                Note.objects.filter(pk=n.pk).update(created_at=timezone.make_aware(datetime.combine(_d(t["date"]), datetime.min.time())))
        n_loads = 0
        for ld in pd.get("loads", []):
            s = Shipment(
                customer=customer, project=p, container_number=ld.get("container_number", ""), equipment=ld.get("equipment", ""),
                service=ld.get("service", Shipment.Service.OTR), customer_ref=ld.get("customer_ref", ""), seal_number=ld.get("seal_number", ""),
                monday_item_id=ld.get("monday_item_id", p.monday_item_id), shipper=location(ld.get("shipper")) or p.default_shipper,
                consignee=location(ld.get("consignee")) or p.default_consignee, pod_received=_d(ld.get("pod_received")),
                pod_check=ld.get("pod_check", ""), pod_link=ld.get("pod_link", ""), notes=ld.get("notes", ""),
                lfd=_d(ld.get("lfd")), empty_return_due=_d(ld.get("empty_return_due")), source_ref=ld.get("source", pd.get("source", ""))[:255],
            )
            if ld.get("mbl"):
                s.master_bl = MasterBL.objects.get_or_create(number=ld["mbl"].upper())[0]
                if ld.get("mbl_eta") and not s.master_bl.eta:
                    s.master_bl.eta = _d(ld["mbl_eta"])
                    s.master_bl.save()
            s.save()
            for c in ld.get("cargo", []):
                CargoLine.objects.create(shipment=s, **_pick(c, ["handling_qty", "handling_type", "pieces", "description", "weight_lbs",
                                                                 "length_in", "width_in", "height_in", "hazmat", "un_number",
                                                                 "hazard_class", "packing_group", "nmfc", "freight_class"]))
            for u in ld.get("units", []):
                Unit.objects.create(shipment=s, **_pick(u, ["serial_number", "product", "config", "tier", "new_serial_number", "notes"]))
            for i, lg in enumerate(ld.get("legs", []), start=1):
                Leg.objects.create(
                    shipment=s, sequence=lg.get("sequence", i), kind=lg.get("kind", Leg.Kind.OTR),
                    origin=location(lg.get("origin")) or s.shipper, destination=location(lg.get("destination")) or s.consignee,
                    carrier=carrier(lg.get("carrier")), carrier_load_number=lg.get("carrier_load_number", ""), equipment=lg.get("equipment", ""),
                    driver_name=lg.get("driver_name", ""), driver_phone=lg.get("driver_phone", ""), truck_trailer=lg.get("truck_trailer", ""),
                    scheduled_pickup=_d(lg.get("scheduled_pickup")), actual_pickup=_d(lg.get("actual_pickup")),
                    scheduled_delivery=_d(lg.get("scheduled_delivery")), actual_delivery=_d(lg.get("actual_delivery")),
                    pickup_arrived_at=_dt(lg.get("pickup_arrived_at")), pickup_departed_at=_dt(lg.get("pickup_departed_at")),
                    delivery_arrived_at=_dt(lg.get("delivery_arrived_at")), delivery_departed_at=_dt(lg.get("delivery_departed_at")),
                    pickup_appt_confirmed=bool(lg.get("pickup_appt_confirmed", lg.get("actual_pickup"))),
                    delivery_appt_confirmed=bool(lg.get("delivery_appt_confirmed", lg.get("actual_delivery"))),
                    notes=lg.get("notes", ""),
                )
            for ch in ld.get("charges", []):
                Charge.objects.create(
                    shipment=s, side=ch["side"], charge_type=ch.get("charge_type", "LINEHAUL"), description=ch.get("description", ""),
                    quantity=_money(ch.get("quantity")) or Decimal("1"), unit_price=_money(ch.get("unit_price")) or Decimal("0"),
                    carrier=carrier(ch.get("carrier")) if ch["side"] == "AP" else None, invoice_number=ch.get("invoice_number", ""),
                    invoice_date=_d(ch.get("invoice_date")), state=ch.get("state", Charge.State.OPEN), notes=ch.get("notes", ""),
                )
            for t in ld.get("timeline", []):
                n = Note.objects.create(shipment=s, kind=Note.Kind.NOTE, text=t["text"], created_by=user)
                if t.get("date"):
                    Note.objects.filter(pk=n.pk).update(created_at=timezone.make_aware(datetime.combine(_d(t["date"]), datetime.min.time())))
            s.refresh_status()
            n_loads += 1
        p.refresh_status()
        p.refresh_from_db()
        log.append(f"导入项目 {p.number} {p.name}：{n_loads} 个 Load，状态「{p.get_status_display()}」")
    return log
