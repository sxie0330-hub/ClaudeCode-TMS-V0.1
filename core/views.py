import csv
import json
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import audit
from .bol import bol_context, render_bols
from .forms import (
    BulkShipmentForm,
    CargoLineForm,
    CarrierForm,
    ChargeForm,
    CustomerForm,
    LegForm,
    LegPermitForm,
    LocationForm,
    TrailerTypeForm,
    MasterBLForm,
    NoteForm,
    ProjectForm,
    ShipmentDocsForm,
    ShipmentForm,
    UnitForm,
    YardStayForm,
)
from .models import (
    CargoLine,
    Carrier,
    Charge,
    Customer,
    Leg,
    LegPermit,
    RateAgreement,
    RateLine,
    Location,
    TrailerType,
    MasterBL,
    Note,
    PodCheck,
    Profile,
    Project,
    Role,
    Shipment,
    Status,
    Unit,
    YardStay,
)
from .permissions import (
    can_edit_ops,
    can_edit_shipment_ops,
    can_see_margin,
    can_manage_rates,
    can_see_side,
    is_management,
    visible_loads,
    visible_projects,
    visible_sides,
)

PAGE_SIZE = 50
OPEN_PROJECT = [Project.Status.PENDING, Project.Status.ACTIVE, Project.Status.DELIVERED]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _need(ok):
    if not ok:
        raise PermissionDenied


def _note(shipment, user, text, kind=Note.Kind.SYSTEM, project=None):
    Note.objects.create(shipment=shipment, project=project, created_by=user, text=text, kind=kind)


def _form_page(request, form, title, cancel_url, *, intro=None, danger=None):
    return render(request, "core/form.html", {"form": form, "title": title, "cancel_url": cancel_url, "intro": intro, "danger": danger})


def _edit(request, form_class, instance, title, success_url, cancel_url=None, form_kwargs=None, before_save=None, intro=None):
    form_kwargs = form_kwargs or {}
    if request.method == "POST":
        form = form_class(request.POST, instance=instance, **form_kwargs)
        if form.is_valid():
            obj = form.save(commit=False)
            if before_save:
                before_save(obj)
            obj.save()
            form.save_m2m()
            messages.success(request, "已保存")
            return redirect(success_url(obj) if callable(success_url) else success_url)
    else:
        form = form_class(instance=instance, **form_kwargs)
    return _form_page(request, form, title, cancel_url or (success_url(instance) if callable(success_url) and instance.pk else "/"), intro=intro)


def _confirm_delete(request, obj, title, success_url, cancel_url, text=None):
    if request.method == "POST":
        obj.soft_delete()
        messages.success(request, "已删除（可在时间线中由管理层恢复）")
        return redirect(success_url)
    return render(request, "core/confirm.html", {"title": title, "text": text or f"确定删除「{obj}」？", "cancel_url": cancel_url, "danger": True})


SPECIAL_FILTERS = [
    ("mine", "1", "我负责的项目"), ("lfd", "soon", "LFD 临近、还没提"), ("pod", "open", "已送达、POD 未上传"),
    ("appt", "soon", "今天 / 明天有提货或送货"), ("ap", "noinvoice", "已送达、承运商发票号未填"),
    ("ar", "open", "已送达、未开票"), ("extra", "open", "额外费用待审 / 争议中"),
]


def _shipment_filters(request, qs):
    g = request.GET
    q = g.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(number__icontains=q) | Q(container_number__icontains=q) | Q(master_bl__number__icontains=q)
            | Q(customer_ref__icontains=q) | Q(monday_item_id__icontains=q) | Q(seal_number__icontains=q)
            | Q(units__serial_number__icontains=q) | Q(units__new_serial_number__icontains=q)
            | Q(legs__carrier_load_number__icontains=q) | Q(project__name__icontains=q)
            | Q(customer__name__icontains=q) | Q(legs__bol_number__icontains=q)
        ).distinct()
    if g.get("status"):
        qs = qs.filter(status=g["status"])
    elif g.get("view") == "open" or not g:
        qs = qs.exclude(status__in=[Status.CLOSED, Status.CANCELLED])
    if g.get("customer"):
        qs = qs.filter(customer_id=g["customer"])
    if g.get("project"):
        qs = qs.filter(project_id=g["project"])
    if g.get("service"):
        qs = qs.filter(service=g["service"])
    if g.get("lfd") == "soon":
        horizon = timezone.localdate() + timezone.timedelta(days=settings.TMS_LFD_WARNING_DAYS)
        qs = qs.filter(lfd__lte=horizon, status__in=[Status.PLANNING, Status.AWAITING, Status.ARRIVED])
    # Filters behind the home-page number tiles (see SPECIAL_FILTERS for their labels)
    user = getattr(request, "user", None)
    if g.get("mine") and getattr(user, "pk", None):
        qs = qs.filter(project__owners=user).distinct()
    if g.get("pod") == "open":
        qs = qs.filter(status=Status.DELIVERED, pod_uploaded_at__isnull=True, pod_waived_reason="")
    if g.get("appt") == "soon":
        days = [timezone.localdate(), timezone.localdate() + timezone.timedelta(days=1)]
        qs = qs.filter(Q(legs__scheduled_pickup__in=days, legs__actual_pickup__isnull=True)
                       | Q(legs__scheduled_delivery__in=days, legs__actual_delivery__isnull=True)).distinct()
    if g.get("ap") == "noinvoice":
        qs = qs.filter(status=Status.DELIVERED, charges__side="AP", charges__state=Charge.State.OPEN, charges__invoice_number="",
                       charges__is_deleted=False).distinct()
    if g.get("ar") == "open":
        qs = qs.filter(status=Status.DELIVERED, charges__side="AR", charges__state=Charge.State.OPEN, charges__is_deleted=False).distinct()
    if g.get("extra") == "open":
        from .alerts import ACCESSORIAL

        qs = qs.filter(charges__side="AP", charges__charge_type__in=ACCESSORIAL, charges__is_deleted=False,
                       charges__state__in=[Charge.State.OPEN, Charge.State.DISPUTED]).distinct()
    sort = g.get("sort", "-created_at")
    allowed = {"number", "-number", "lfd", "-lfd", "master_bl__eta", "-master_bl__eta", "-created_at", "status", "container_number"}
    return qs.order_by(sort if sort in allowed else "-created_at")


def _with_money(qs, user):
    zero = Value(Decimal("0"), output_field=DecimalField(max_digits=12, decimal_places=2))
    live = Q(charges__is_deleted=False) & ~Q(charges__state=Charge.State.VOID)
    if can_see_side(user, Charge.Side.AR):
        qs = qs.annotate(ar_total=Coalesce(Sum("charges__amount", filter=live & Q(charges__side="AR")), zero))
    if can_see_side(user, Charge.Side.AP):
        qs = qs.annotate(ap_total=Coalesce(Sum("charges__amount", filter=live & Q(charges__side="AP")), zero))
    return qs


# ---------------------------------------------------------------------------
# shipments
# ---------------------------------------------------------------------------
def shipment_list(request):
    qs = visible_loads(request.user, Shipment.objects.select_related("customer", "project", "master_bl", "consignee"))
    # Prefetches run only for the 50 rows of the page actually rendered.
    qs = _shipment_filters(request, qs).prefetch_related("legs__carrier", "legs__origin", "legs__destination")
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "core/shipment_list.html", {
        "page": page,
        "rows": rows,
        "statuses": Status.choices,
        "customers": Customer.objects.all(),
        "projects": visible_projects(request.user, Project.objects.filter(status__in=OPEN_PROJECT).select_related("customer")),
        "services": Shipment.Service.choices,
        "querystring": params.urlencode(),
        "g": request.GET,
        "today": timezone.localdate(),
        "special": [label for key, val, label in SPECIAL_FILTERS if request.GET.get(key) == val],
        "keep": [(key, val) for key, val, _ in SPECIAL_FILTERS if key != "lfd" and request.GET.get(key) == val],
    })


def shipment_export(request):
    qs = _shipment_filters(request, visible_loads(request.user, Shipment.objects.select_related("customer", "project", "master_bl", "consignee", "shipper")))
    qs = _with_money(qs, request.user).prefetch_related("legs__carrier", "units")
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="shipments-{timezone.localdate()}.csv"'
    resp.write("﻿")  # Excel/Sheets open UTF-8 correctly with a BOM
    w = csv.writer(resp)
    see_ar = can_see_side(request.user, "AR")
    see_ap = can_see_side(request.user, "AP")
    head = ["Load#", "状态", "客户", "项目", "Monday ID", "客户参考号", "服务", "MBL", "柜号", "柜型", "序列号", "ETA", "LFD",
            "起运地", "最终收货地", "承运商", "实际提货", "实际送达", "POD"]
    if see_ar:
        head.append("应收合计")
    if see_ap:
        head.append("应付合计")
    if see_ar and see_ap:
        head.append("毛利")
    w.writerow(head)
    for s in qs:
        legs = list(s.legs.all())
        row = [
            s.number, s.get_status_display(), s.customer.name, s.project.name if s.project else "", s.monday_item_id, s.customer_ref,
            s.get_service_display(), s.master_bl.number if s.master_bl else "", s.container_number, s.equipment,
            " / ".join(u.serial_number for u in s.units.all()),
            s.master_bl.eta if s.master_bl and s.master_bl.eta else "", s.lfd or "",
            s.shipper.code if s.shipper else "", s.consignee.code if s.consignee else "",
            " / ".join(str(leg.carrier) for leg in legs if leg.carrier),
            legs[0].actual_pickup if legs and legs[0].actual_pickup else "",
            legs[-1].actual_delivery if legs and legs[-1].actual_delivery else "",
            s.pod_link,
        ]
        if see_ar:
            row.append(s.ar_total)
        if see_ap:
            row.append(s.ap_total)
        if see_ar and see_ap:
            row.append(s.ar_total - s.ap_total)
        w.writerow(row)
    return resp


def shipment_detail(request, pk):
    s = get_object_or_404(Shipment.objects.select_related("customer", "project", "master_bl__terminal", "shipper", "consignee", "empty_return_location"), pk=pk)
    user = request.user
    sides = visible_sides(user)
    charges = [c for c in s.charges.select_related("carrier") if c.side in sides]
    ar = [c for c in charges if c.side == Charge.Side.AR]
    ap = [c for c in charges if c.side == Charge.Side.AP]

    def total(rows):
        return sum((c.amount for c in rows if c.state != Charge.State.VOID), Decimal("0"))

    yard_stays = list(s.yard_stays.select_related("yard"))
    legs = list(s.legs.select_related("origin", "destination", "carrier").prefetch_related("permits"))
    agreements = {a.carrier_id: a for a in RateAgreement.objects.filter(project_id=s.project_id)} if s.project_id else {}
    for leg in legs:
        leg.rate_agreement = agreements.get(leg.carrier_id)
    free = s.project.detention_free_hours if s.project else Decimal("2")
    rate = s.project.detention_rate if s.project else None
    waits = []
    for leg in legs:
        for where, w in (("提货点", leg.pickup_wait(free)), ("收货点", leg.delivery_wait(free))):
            if w:
                waits.append({"leg": leg, "where": where, **w, "amount": (w["billable"] * rate).quantize(Decimal("1")) if rate else None})
    first, last = (legs[0], legs[-1]) if legs else (None, None)

    def fmt(v):
        if not v:
            return ""
        if isinstance(v, str):
            return v
        if hasattr(v, "hour"):
            return timezone.localtime(v).strftime("%m-%d %H:%M %Z")
        return v.strftime("%m-%d")

    nodes = [
        {"name": "已派车", "done": bool(first and first.carrier_id), "when": (first.carrier.name if first and first.carrier else "")},
        {"name": "到达提货点", "done": bool(first and first.pickup_arrived_at), "when": first.pickup_arrived_at if first else None},
        {"name": "提货离场", "done": bool(first and (first.pickup_departed_at or first.actual_pickup)), "when": (first.pickup_departed_at or first.actual_pickup) if first else None},
        {"name": "在途", "done": bool(first and first.actual_pickup), "when": ""},
        {"name": "到达收货点", "done": bool(last and last.delivery_arrived_at), "when": last.delivery_arrived_at if last else None},
        {"name": "卸货完成", "done": s.status in (Status.DELIVERED, Status.CLOSED), "when": (last.delivery_departed_at or last.actual_delivery) if last else None},
        {"name": "POD 收到", "done": bool(s.pod_received), "when": s.pod_received},
    ]
    for n in nodes:
        n["when"] = fmt(n["when"])
    return render(request, "core/shipment_detail.html", {
        "s": s,
        "legs": legs,
        "nodes": nodes,
        "waits": waits,
        "free_hours": free,
        "rate": rate,
        "cargo": s.cargo_lines.all(),
        "units": s.units.all(),
        "yard_stays": yard_stays,
        "ar": ar, "ap": ap,
        "ar_total": total(ar), "ap_total": total(ap),
        "margin": total(ar) - total(ap) if can_see_margin(user) else None,
        "timeline": audit.shipment_timeline(s, user, can_revert=is_management(user)),
        "note_form": NoteForm(),
        "can_edit": can_edit_shipment_ops(user, s),
        "can_edit_docs": can_edit_ops(user),
        "can_edit_ar": can_see_side(user, "AR"),
        "can_rates": can_manage_rates(user),
        "can_edit_ap": can_see_side(user, "AP"),
    })


def _new_shipment_defaults(project):
    if not project:
        return {}
    return {"customer": project.customer, "project": project, "shipper": project.default_shipper,
            "consignee": project.default_consignee, "monday_item_id": project.monday_item_id}


def _add_default_cargo(shipment):
    p = shipment.project
    if p and p.cargo_description and not shipment.cargo_lines.exists():
        CargoLine.objects.create(
            shipment=shipment, handling_qty=1,
            handling_type=CargoLine.HandlingType.CONTAINER if shipment.container_number else CargoLine.HandlingType.UNIT,
            description=p.cargo_description, hazmat=bool(p.un_number), un_number=p.un_number,
            hazard_class=p.hazard_class, nmfc=p.nmfc, freight_class=p.freight_class,
        )


def shipment_create(request):
    _need(can_edit_ops(request.user))
    project = visible_projects(request.user).filter(pk=request.GET.get("project")).first()
    if request.method == "POST":
        form = ShipmentForm(request.POST)
        if form.is_valid():
            s = form.save()
            _add_default_cargo(s)
            messages.success(request, f"已创建 Load {s.number}")
            return redirect(s)
    else:
        form = ShipmentForm(initial=_new_shipment_defaults(project))
    return _form_page(request, form, "新建 Load", project.get_absolute_url() if project else reverse("shipment_list"))


def shipment_edit(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(can_edit_shipment_ops(request.user, s))
    return _edit(request, ShipmentForm, s, f"编辑 Load {s.number}", lambda o: o.get_absolute_url())


def shipment_docs(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(can_edit_ops(request.user))
    return _edit(request, ShipmentDocsForm, s, f"{s.number} 文件与还空", lambda o: o.get_absolute_url(),
                 intro="送达后仍可编辑。文件请存在 Google Drive，这里贴共享链接。")


def shipment_delete(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(is_management(request.user))
    return _confirm_delete(request, s, f"删除 Load {s.number}", reverse("shipment_list"), s.get_absolute_url())


@require_POST
def shipment_unlock(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(is_management(request.user))
    Shipment.objects.filter(pk=pk).update(is_locked=False)
    _note(s, request.user, "管理层解锁了 Load（已送达/已结案的数据可再次编辑）")
    messages.warning(request, "已解锁。改完后请点“重新锁定”。")
    return redirect(s)


@require_POST
def shipment_lock(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(is_management(request.user))
    Shipment.objects.filter(pk=pk).update(is_locked=True)
    _note(s, request.user, "管理层重新锁定了 Load")
    return redirect(s)


@require_POST
def shipment_close(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(is_management(request.user))
    if s.closed_at:
        s.closed_at = None
        _note(s, request.user, "管理层取消结案")
    else:
        if s.status != Status.DELIVERED:
            messages.error(request, "只有已送达的 Load 可以结案")
            return redirect(s)
        if not s.pod_done:
            messages.error(request, "POD 还没确认上传，不能结案。请 OP 核对并上传 POD 后点“POD 已上传”，或由管理层设为无需 POD")
            return redirect(s)
        s.closed_at = timezone.now()
        _note(s, request.user, "管理层结案")
    s.save()
    return redirect(s)


# ---------------------------------------------------------------------------
# POD: a person confirms it was checked and filed (TMS keeps no file)
# ---------------------------------------------------------------------------
def _mark_pod_uploaded(s, user, link=""):
    if s.pod_uploaded_at:
        return False
    s.pod_uploaded_at, s.pod_uploaded_by = timezone.now(), user
    if not s.pod_received:
        s.pod_received = timezone.localdate()
    if not s.pod_check:
        s.pod_check = PodCheck.OK
    if link:
        s.pod_link = link
    s.save()
    _note(s, user, "POD 已核对并上传" + (f"：{link}" if link else ""))
    return True


@require_POST
def shipment_pod(request, pk):
    """action = uploaded / undo / waive / unwaive."""
    s = get_object_or_404(Shipment, pk=pk)
    user, action = request.user, request.POST.get("action")
    _need(can_edit_ops(user))
    if action == "uploaded":
        link = request.POST.get("pod_link", "").strip()
        if link and not link.lower().startswith(("http://", "https://")):
            messages.error(request, "POD 存放位置请填完整链接（https://…），或者留空")
            return redirect(s)
        _mark_pod_uploaded(s, user, link)
    elif action == "undo" and s.pod_uploaded_at:
        s.pod_uploaded_at, s.pod_uploaded_by = None, None
        s.save()
        _note(s, user, "撤销 POD 已上传")
    elif action in ("waive", "unwaive"):
        _need(is_management(user))
        if action == "waive":
            reason = request.POST.get("reason", "").strip()
            if not reason:
                messages.error(request, "请写明为什么这个 Load 不需要 POD")
                return redirect(s)
            s.pod_waived_reason = reason[:255]
            _note(s, user, f"管理层设为无需 POD：{reason}")
        else:
            s.pod_waived_reason = ""
            _note(s, user, "管理层取消“无需 POD”")
        s.save()
    if s.project_id:
        s.project.refresh_status()
    return redirect(request.POST.get("next") or s.get_absolute_url())


@require_POST
def bulk_pod_uploaded(request):
    _need(can_edit_ops(request.user))
    shipments = list(visible_loads(request.user).filter(pk__in=request.POST.getlist("ids")).order_by("number"))
    back = request.META.get("HTTP_REFERER") or reverse("shipment_list")
    if not shipments:
        messages.error(request, "请先勾选 Load")
        return redirect(back)
    done = [x for x in shipments if _mark_pod_uploaded(x, request.user)]
    skipped = len(shipments) - len(done)
    messages.success(request, f"{len(done)} 个 Load 标为 POD 已上传" + (f"，{skipped} 个之前已经标过" if skipped else ""))
    return redirect(back)


@require_POST
def shipment_note(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    form = NoteForm(request.POST)
    if form.is_valid():
        _note(s, request.user, form.cleaned_data["text"], kind=Note.Kind.NOTE)
    return redirect(reverse("shipment_detail", args=[pk]) + "#timeline")


def mbl_edit(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(can_edit_shipment_ops(request.user, s))
    if not s.master_bl:
        messages.info(request, "请先在 Load 里填 MBL 号")
        return redirect(reverse("shipment_edit", args=[pk]))
    others = s.master_bl.shipments.exclude(pk=pk).count()
    intro = f"该提单下还有 {others} 个其他柜子，修改会同时作用于它们。" if others else None
    return _edit(request, MasterBLForm, s.master_bl, f"提单 {s.master_bl.number}", s.get_absolute_url(), intro=intro)


# --- child rows of a shipment ------------------------------------------------
CHILDREN = {
    "leg": (Leg, LegForm, "运输段"),
    "cargo": (CargoLine, CargoLineForm, "货物明细"),
    "unit": (Unit, UnitForm, "设备序列号"),
    "yard": (YardStay, YardStayForm, "堆场停留"),
    "permit": (LegPermit, LegPermitForm, "许可证"),
}


def _child_initial(kind, s):
    if kind == "leg":
        legs = list(s.legs.all())
        prev = legs[-1] if legs else None
        return {
            "sequence": (prev.sequence + 1) if prev else 1,
            "origin": prev.destination if prev else (s.master_bl.terminal if s.master_bl and s.master_bl.terminal else s.shipper),
            "destination": s.consignee,
            "kind": Leg.Kind.OTR if prev else (Leg.Kind.DRAYAGE if s.container_number else Leg.Kind.OTR),
        }
    if kind == "yard":
        last = s.legs.exclude(actual_delivery=None).last()
        return {"in_date": last.actual_delivery if last else None, "yard": last.destination if last and last.destination and last.destination.kind == Location.Kind.YARD else None}
    if kind == "cargo":
        p = s.project
        if p:
            return {"description": p.cargo_description, "un_number": p.un_number, "hazard_class": p.hazard_class,
                    "hazmat": bool(p.un_number), "nmfc": p.nmfc, "freight_class": p.freight_class}
    return {}


def child_create(request, pk, kind):
    s = get_object_or_404(Shipment, pk=pk)
    _need(can_edit_shipment_ops(request.user, s))
    model, form_class, label = CHILDREN[kind]
    if request.method == "POST":
        form = form_class(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.shipment = s
            obj.save()
            messages.success(request, f"已添加{label}")
            return redirect(reverse("shipment_detail", args=[pk]) + f"#{kind}")
    else:
        form = form_class(initial=_child_initial(kind, s))
    return _form_page(request, form, f"{s.number} · 添加{label}", s.get_absolute_url())


def child_edit(request, kind, cid):
    model, form_class, label = CHILDREN[kind]
    obj = get_object_or_404(model, pk=cid)
    _need(can_edit_shipment_ops(request.user, obj.shipment))
    return _edit(request, form_class, obj, f"{obj.shipment.number} · 编辑{label}",
                 lambda o: reverse("shipment_detail", args=[o.shipment_id]) + f"#{kind}")


def child_delete(request, kind, cid):
    model, _, label = CHILDREN[kind]
    obj = get_object_or_404(model, pk=cid)
    _need(can_edit_shipment_ops(request.user, obj.shipment))
    url = obj.shipment.get_absolute_url()
    return _confirm_delete(request, obj, f"删除{label}", url, url)


# --- permits (per leg, per state) ----------------------------------------------
def permit_create(request, pk, lid):
    s = get_object_or_404(Shipment, pk=pk)
    leg = get_object_or_404(Leg, pk=lid, shipment=s)
    _need(can_edit_shipment_ops(request.user, s))
    if request.method == "POST":
        form = LegPermitForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.leg = leg
            obj.save()
            messages.success(request, f"已添加 {obj.state} 许可证")
            return redirect(reverse("shipment_detail", args=[pk]) + "#leg")
    else:
        form = LegPermitForm(initial={"state": request.GET.get("state", "")})
    return _form_page(request, form, f"{s.number} · 第 {leg.sequence} 段 · 添加许可证", s.get_absolute_url())


def _leg_screen(leg):
    """OOG screen for one leg, from its origin / destination states and the Load's cargo lines."""
    from types import SimpleNamespace

    from . import oog

    s = leg.shipment
    o, d = leg.origin, leg.destination
    lane = SimpleNamespace(
        origin=o.full_address if o else "", destination=d.full_address if d else "",
        origin_state=oog.state_from_text(f", {o.state}") or oog.state_from_text(o.state or "") if o else "",
        destination_state=oog.state_from_text(f", {d.state}") or oog.state_from_text(d.state or "") if d else "",
        via_states=" ".join(p.state for p in leg.permits.all()), trucks=1, trailer_id=None, trailer=None)
    cargo = [SimpleNamespace(qty=1, weight_lbs=c.weight_lbs, length_ft=c.length_ft, width_ft=c.width_ft, height_ft=c.height_ft,
                             divisible=False, hazmat=c.hazmat, description=c.description) for c in s.cargo_lines.all()]
    return oog.screen_lane(lane, cargo, list(TrailerType.objects.filter(active=True)))


@require_POST
def permit_prefill(request, pk, lid):
    """Add a 待办理 permit row for every route state the OOG screen says needs one (escorts / police / limits in the notes)."""
    s = get_object_or_404(Shipment, pk=pk)
    leg = get_object_or_404(Leg, pk=lid, shipment=s)
    _need(can_edit_shipment_ops(request.user, s))
    res = _leg_screen(leg)
    have = {p.state for p in leg.permits.all()}
    added = []
    for r in res["rows"]:
        if r["state"] == "US" or r["state"] in have or not r["permits"]:
            continue
        LegPermit.objects.create(
            leg=leg, state=r["state"], escorts="；".join(r["escorts"] + r["police"])[:255],
            notes="\n".join(r["permits"] + r["superload"] + r["survey"] + r["travel"][:1]))
        added.append(r["state"])
    if added:
        Leg.objects.filter(pk=leg.pk).update(permits_required=True)
        messages.success(request, f"按 OOG 初筛加了 {'、'.join(added)} 的许可证（待办理）。途经州请自己补上，再点一次会按新的路线补齐")
    else:
        msg = "；".join(res["warnings"]) or "OOG 初筛认为这些州不需要许可证"
        messages.info(request, f"没有新增：{msg}")
    return redirect(reverse("shipment_detail", args=[pk]) + "#leg")


# --- charges ---------------------------------------------------------------
def charge_create(request, pk, side):
    s = get_object_or_404(Shipment, pk=pk)
    if side not in Charge.Side.values:
        raise Http404
    _need(can_see_side(request.user, side))
    initial = {}
    if request.GET.get("type") in Charge.Type.values:
        initial["charge_type"] = request.GET["type"]
    if request.GET.get("qty"):
        initial["quantity"] = request.GET["qty"]
    if side == Charge.Side.AP:
        leg = s.current_leg
        if leg and leg.carrier:
            initial["carrier"] = leg.carrier
    if request.method == "POST":
        form = ChargeForm(request.POST, side=side)
        if form.is_valid():
            c = form.save(commit=False)
            c.shipment, c.side = s, side
            c.save()
            messages.success(request, "已添加费用")
            return redirect(reverse("shipment_detail", args=[pk]) + "#charges")
    else:
        form = ChargeForm(initial=initial, side=side)
    return _form_page(request, form, f"{s.number} · 添加{Charge.Side(side).label}", s.get_absolute_url())


def charge_edit(request, cid):
    c = get_object_or_404(Charge, pk=cid)
    _need(can_see_side(request.user, c.side))
    return _edit(request, ChargeForm, c, f"{c.shipment.number} · 编辑{c.get_side_display()}",
                 lambda o: reverse("shipment_detail", args=[o.shipment_id]) + "#charges")


def charge_delete(request, cid):
    c = get_object_or_404(Charge, pk=cid)
    _need(can_see_side(request.user, c.side))
    url = c.shipment.get_absolute_url()
    return _confirm_delete(request, c, "删除费用", url, url, text="建议把状态改为“作废”而不是删除。仍要删除？")


# --- BOL --------------------------------------------------------------------
def shipment_bol(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    leg = s.legs.filter(pk=request.GET.get("leg")).first() if request.GET.get("leg") else s.current_leg
    ctx = bol_context(s, leg)
    pdf = render_bols([ctx])
    _note(s, request.user, f"生成 BOL {ctx['bol_number']}" + (f"（第 {leg.sequence} 段 {leg.get_kind_display()}）" if leg else ""), kind=Note.Kind.BOL)
    return _pdf(pdf, f"BOL-{ctx['bol_number']}.pdf")


# ---------------------------------------------------------------------------
# Carrier rate agreement (rate confirmation) per project and carrier. Shows carrier rates: AP-visible roles only.
# ---------------------------------------------------------------------------
def _rates_allowed(user):
    from .permissions import can_manage_rates

    _need(can_manage_rates(user))


def rate_agreement_new(request, pk):
    from . import ratecon

    p = get_object_or_404(Project, pk=pk)
    _rates_allowed(request.user)
    used = Carrier.objects.filter(legs__shipment__project=p).distinct()
    if request.method == "POST":
        carrier = get_object_or_404(Carrier, pk=request.POST.get("carrier"))
        data, lines = ratecon.prefill(p, carrier)
        a = RateAgreement.objects.create(project=p, carrier=carrier, created_by=request.user, **data)
        for ln in lines:
            RateLine.objects.create(agreement=a, **ln)
        messages.success(request, f"已按{'询报价' if p.leads.exists() else '已有费用'}带出运价，请核对后保存")
        return redirect("rate_agreement_edit", a.pk)
    return render(request, "core/rate_new.html", {"p": p, "used": used, "carriers": Carrier.objects.all(),
                                                  "pre": request.GET.get("carrier", "")})


def rate_agreement(request, pk):
    from . import quotes, ratecon

    a = get_object_or_404(RateAgreement.objects.select_related("project", "carrier"), pk=pk)
    _rates_allowed(request.user)
    ctx = ratecon.context(a, request.user)
    to, subject, body = ratecon.email_draft(ctx)
    return render(request, "core/rate_agreement.html", {"a": a, "ctx": ctx, "problems": ratecon.problems(ctx), "to": to,
                                                        "invoicing": ratecon.invoicing_lines(ctx),
                                                        "subject": subject, "body": body, "gmail": quotes.gmail_compose_url(to, subject, body)})


def rate_agreement_edit(request, pk):
    from .forms import RateAgreementForm, rate_lines_formset

    a = get_object_or_404(RateAgreement, pk=pk)
    _rates_allowed(request.user)
    FS = rate_lines_formset()
    form = RateAgreementForm(request.POST or None, instance=a)
    fs = FS(request.POST or None, instance=a)
    if request.method == "POST" and form.is_valid() and fs.is_valid():
        form.save()
        fs.save()
        messages.success(request, "已保存")
        return redirect(a)
    return render(request, "core/lead_formset.html", {"title": f"运价协议 {a.number} · {a.carrier}", "form": form, "fs": fs,
                                                      "kind": "rate", "cancel_url": a.get_absolute_url(), "back": "返回协议"})


def rate_agreement_pdf(request, pk):
    from . import ratecon

    a = get_object_or_404(RateAgreement.objects.select_related("project", "carrier"), pk=pk)
    _rates_allowed(request.user)
    ctx = ratecon.context(a, request.user)
    pdf = ratecon.render(ctx)
    RateAgreement.objects.filter(pk=a.pk).update(version=ctx["version"], issued_at=timezone.now())
    _note(None, request.user, f"出具运价协议 {a.number}（{a.carrier}，第 {ctx['version']} 版）"
          + (f"，每车 ${ctx['per_truck']:,.2f}" if ctx["per_truck"] else ""), project=a.project)
    return _pdf(pdf, f"RateConfirmation-{a.number}.pdf")


@require_POST
def bulk_bol(request):
    ids = request.POST.getlist("ids")
    shipments = list(visible_loads(request.user).filter(pk__in=ids).order_by("number"))
    if not shipments:
        messages.error(request, "请先勾选 Load")
        return redirect(request.META.get("HTTP_REFERER") or reverse("shipment_list"))
    contexts = []
    for s in shipments:
        leg = s.current_leg
        ctx = bol_context(s, leg)
        contexts.append(ctx)
        _note(s, request.user, f"批量生成 BOL {ctx['bol_number']}", kind=Note.Kind.BOL)
    return _pdf(render_bols(contexts), f"BOL-batch-{len(contexts)}-{timezone.localdate()}.pdf")


def _pdf(data, filename):
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    return resp


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------
def project_list(request):
    qs = visible_projects(request.user, Project.objects.select_related("customer").prefetch_related("owners"))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(number__icontains=q) | Q(monday_item_id__icontains=q) | Q(customer__name__icontains=q))
    st = request.GET.get("status", "open")
    if st == "open":
        qs = qs.filter(status__in=OPEN_PROJECT)
    elif st:
        qs = qs.filter(status=st)
    if request.GET.get("mine"):
        qs = qs.filter(owners=request.user)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "core/project_list.html", {"page": page, "g": request.GET, "statuses": Project.Status.choices})


def _milestones(p, loads):
    """Standard milestones derived from the loads, plus the project's custom ones."""
    today = timezone.localdate()
    live = [x for x in loads if x.status != Status.CANCELLED]
    n = len(live)
    picks = [x.picked_up_on for x in live if x.picked_up_on]
    dels = [x.delivered_on for x in live if x.delivered_on]
    pods = [x.pod_received for x in live if x.pod_received]
    plan_pick = [leg.scheduled_pickup for x in live for leg in x.legs.all() if leg.scheduled_pickup]
    rows = []
    if p.start_date:
        rows.append({"name": "项目开始", "date": p.start_date, "done": p.start_date <= today, "note": ""})
    for m in p.milestones.all():
        rows.append({"name": m.name, "date": m.done_date or m.planned_date, "done": bool(m.done_date), "note": "" if m.done_date else "计划", "obj": m})
    if n:
        rows.append({"name": "首车提货", "date": min(picks) if picks else (min(plan_pick) if plan_pick else None), "done": bool(picks), "note": "" if picks else "计划"})
        rows.append({"name": "末车提货", "date": max(picks) if len(picks) == n else None, "done": len(picks) == n, "note": "" if len(picks) == n else f"{len(picks)}/{n}"})
        rows.append({"name": "末车送达", "date": max(dels) if len(dels) == n else None, "done": len(dels) == n, "note": "" if len(dels) == n else f"{len(dels)}/{n}"})
        filed = [x for x in live if x.pod_done]
        last = max((x.pod_uploaded_at for x in filed if x.pod_uploaded_at), default=None)
        note = "" if len(filed) == n else f"已上传 {len(filed)}/{n}" + (f" · 邮件里已收 {len(pods)}" if len(pods) != len(filed) else "")
        rows.append({"name": "POD 上传齐", "date": timezone.localtime(last).date() if last and len(filed) == n else None,
                     "done": len(filed) == n, "note": note, "warn": len(dels) == n and len(filed) < n})
    rows.append({"name": "完结", "date": None, "done": p.status == Project.Status.DONE, "note": "负责人确认"})
    return rows


def project_detail(request, pk):
    from . import alerts as alert_engine

    p = get_object_or_404(Project.objects.select_related("customer", "default_shipper", "default_consignee").prefetch_related("owners"), pk=pk)
    user = request.user
    sides = visible_sides(user)
    loads = list(p.shipments.order_by("number").select_related("master_bl", "consignee").prefetch_related("legs__carrier", "legs__origin", "legs__destination", "units", "charges"))
    for x in loads:
        live = [c for c in x.charges.all() if c.state != Charge.State.VOID and c.side in sides]
        x.ar = sum((c.amount for c in live if c.side == "AR"), Decimal("0")) if "AR" in sides else None
        x.ap = sum((c.amount for c in live if c.side == "AP"), Decimal("0")) if "AP" in sides else None
        x.margin = (x.ar - x.ap) if can_see_margin(user) else None
        x.extra = sum((c.amount for c in live if c.charge_type in alert_engine.ACCESSORIAL), Decimal("0"))
    live_loads = [x for x in loads if x.status != Status.CANCELLED]
    money = _project_money(p, user)
    kpi = {
        "n": len(live_loads),
        "delivered": sum(1 for x in live_loads if x.status in (Status.DELIVERED, Status.CLOSED)),
        "pod": sum(1 for x in live_loads if x.pod_received or x.pod_done),
        "pod_done": sum(1 for x in live_loads if x.pod_done),
        "extra": sum((x.extra for x in live_loads), Decimal("0")) if sides else None,
        "pct": (money["margin"] / money["ar"] * 100) if money.get("margin") is not None and money.get("ar") else None,
    }
    return render(request, "core/project_detail.html", {
        "p": p,
        "shipments": loads,
        "money": money,
        "kpi": kpi,
        "milestones": _milestones(p, loads),
        "can_rates": can_manage_rates(user),
        "rate_agreements": list(p.rate_agreements.select_related("carrier").prefetch_related("lines")) if can_manage_rates(user) else [],
        "carriers_missing": list(Carrier.objects.filter(legs__shipment__project=p).exclude(rate_agreements__project=p).distinct())
        if can_manage_rates(user) else [],
        "alerts": alert_engine.collect(user, project=p),
        "timeline": audit.project_timeline(p, can_revert=is_management(user)),
        "note_form": NoteForm(),
        "see_ar": "AR" in sides, "see_ap": "AP" in sides, "see_margin": can_see_margin(user),
    })


def milestone_edit(request, pk, mid=None):
    from .forms import MilestoneForm
    from .models import Milestone

    p = get_object_or_404(Project, pk=pk)
    _need(can_edit_ops(request.user))
    m = get_object_or_404(Milestone, pk=mid, project=p) if mid else Milestone(project=p)
    if request.method == "POST" and request.POST.get("delete") and m.pk:
        m.delete()
        return redirect(p)
    if request.method == "POST":
        form = MilestoneForm(request.POST, instance=m)
        if form.is_valid():
            form.save()
            return redirect(reverse("project_detail", args=[p.pk]) + "#milestones")
    else:
        form = MilestoneForm(instance=m)
    return _form_page(request, form, f"{p.name} · {'编辑' if m.pk else '添加'}里程碑", p.get_absolute_url())


def _project_money(project, user):
    out = {"ar": None, "ap": None, "margin": None}
    base = Charge.objects.filter(shipment__project=project, shipment__is_deleted=False).exclude(state=Charge.State.VOID)
    for side in visible_sides(user):
        out[side.lower()] = base.filter(side=side).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    if can_see_margin(user):
        out["margin"] = out["ar"] - out["ap"]
    return out


def project_create(request):
    _need(can_edit_ops(request.user))
    initial = {"owners": [request.user.pk]}  # the person creating it owns it unless they choose otherwise
    if request.GET.get("customer"):
        initial["customer"] = request.GET.get("customer")
    if request.method == "POST":
        form = ProjectForm(request.POST)
        if form.is_valid():
            p = form.save()
            if not p.owners.exists():
                p.owners.add(request.user)
            from .notify import notify

            notify(p.owners.all(), f"{request.user.get_full_name() or request.user.email} 新建项目 {p.number} {p.name}，交给你负责",
                   p.get_absolute_url(), exclude=request.user)
            messages.success(request, f"已创建项目 {p.number}")
            return redirect(p)
    else:
        form = ProjectForm(initial=initial)
    return _form_page(request, form, "新建项目", reverse("project_list"))


def project_edit(request, pk):
    p = get_object_or_404(Project, pk=pk)
    _need(can_edit_ops(request.user))
    before = set(p.owners.values_list("pk", flat=True))
    resp = _edit(request, ProjectForm, p, f"编辑项目 {p.number}", lambda o: o.get_absolute_url())
    if request.method == "POST" and resp.status_code == 302:
        from .notify import notify

        added = p.owners.exclude(pk__in=before)
        notify(added, f"{request.user.get_full_name() or request.user.email} 把项目 {p.number} {p.name} 交给你负责", p.get_absolute_url(),
               exclude=request.user)
    return resp


@require_POST
def project_close(request, pk):
    """One click for the owner once every Load is delivered and every POD uploaded."""
    p = get_object_or_404(Project, pk=pk)
    _need(can_edit_ops(request.user))
    missing = p.pods_missing()
    if missing:
        messages.error(request, f"还有 {len(missing)} 个 Load 的 POD 没确认上传，项目不能完结")
    elif p.status != Project.Status.DELIVERED:
        messages.error(request, "还有 Load 没送达，项目不能完结")
    else:
        p.manual_status = Project.Status.DONE
        p.save()
        _note(None, request.user, "确认项目完结（全部送达，POD 已齐）", kind=Note.Kind.NOTE, project=p)
        messages.success(request, f"项目 {p.number} 已完结")
    return redirect(p)


@require_POST
def project_note(request, pk):
    p = get_object_or_404(Project, pk=pk)
    form = NoteForm(request.POST)
    if form.is_valid():
        _note(None, request.user, form.cleaned_data["text"], kind=Note.Kind.NOTE, project=p)
    return redirect(reverse("project_detail", args=[pk]) + "#timeline")


def project_bulk_shipments(request, pk):
    p = get_object_or_404(Project, pk=pk)
    _need(can_edit_ops(request.user))
    if request.method == "POST":
        form = BulkShipmentForm(request.POST)
        if form.is_valid():
            rows = form.parsed()
            created, skipped = [], []
            with transaction.atomic():
                for cntr, mbl, serial in rows:
                    cntr = cntr.upper()
                    if Shipment.objects.filter(project=p, container_number=cntr).exists():
                        skipped.append(cntr)
                        continue
                    s = Shipment(
                        customer=p.customer, project=p, service=form.cleaned_data["service"],
                        equipment=form.cleaned_data["equipment"], container_number=cntr,
                        shipper=p.default_shipper, consignee=p.default_consignee, monday_item_id=p.monday_item_id,
                    )
                    if mbl:
                        s.master_bl, _ = MasterBL.objects.get_or_create(number=mbl.upper())
                    s.save()
                    if serial:
                        Unit.objects.create(shipment=s, serial_number=serial)
                    _add_default_cargo(s)
                    created.append(s.number)
            messages.success(request, f"已创建 {len(created)} 个 Load" + (f"；跳过已存在的柜号：{', '.join(skipped)}" if skipped else ""))
            return redirect(p)
    else:
        form = BulkShipmentForm()
    return _form_page(request, form, f"{p.name} · 批量新建 Load", p.get_absolute_url(),
                      intro="每行一个柜子。客户、项目、默认提货地/收货地和货物描述会自动从项目带入。")


# ---------------------------------------------------------------------------
# master data
# ---------------------------------------------------------------------------
MASTER = {
    "customer": (Customer, CustomerForm, "客户", ["name", "code", "contact_name", "contact_phone"]),
    "carrier": (Carrier, CarrierForm, "承运商", ["name", "code", "kind", "scac", "contact_name", "contact_phone", "compliance_status"]),
    "location": (Location, LocationForm, "地址", ["code", "name", "kind", "full_address", "contact_phone"]),
    "trailer": (TrailerType, TrailerTypeForm, "车型", ["name", "deck_height_in", "deck_length_ft", "trailer_tare_lbs",
                                                      "tractor_tare_lbs", "rated_capacity_lbs", "sort", "active"]),
}


def master_list(request, kind):
    model, _, label, cols = MASTER[kind]
    qs = model.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        fields = {"customer": ["name", "code"], "carrier": ["name", "code", "scac", "mc_number"], "location": ["code", "name", "address", "city"],
                  "trailer": ["name", "key", "notes"]}[kind]
        cond = Q()
        for f in fields:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    computed = {"full_address": "地址", "compliance_status": "资质"}
    headers = [computed[c] if c in computed else model._meta.get_field(c).verbose_name for c in cols]
    def cell(o, c):
        v = o.get_kind_display() if c == "kind" else getattr(o, c)
        return ("是" if v else "否") if isinstance(v, bool) else v
    rows = [(o, [cell(o, c) for c in cols]) for o in page.object_list]
    can_edit = is_management(request.user) if kind == "trailer" else can_edit_ops(request.user)
    return render(request, "core/master_list.html", {"kind": kind, "label": label, "page": page, "headers": headers, "rows": rows,
                                                     "g": request.GET, "can_edit": can_edit})


def master_detail(request, kind, pk):
    model, _, label, _ = MASTER[kind]
    obj = get_object_or_404(model, pk=pk)
    def shown(f):
        v = obj._get_FIELD_display(f) if f.choices else getattr(obj, f.name)
        return ("是" if v else "否") if isinstance(v, bool) else v
    fields = [(f.verbose_name, shown(f)) for f in model._meta.fields if f.name not in ("id", "is_deleted", "created_at", "updated_at")]
    related = []
    if kind == "customer":
        related = [("项目", obj.projects.all()[:100]), ("最近 Load", obj.shipments.select_related("project")[:100])]
    elif kind == "carrier":
        related = [("最近运输段", Leg.objects.filter(carrier=obj, shipment__in=visible_loads(request.user)).select_related("shipment", "origin", "destination").order_by("-id")[:100])]
    elif kind == "location":
        related = [("最近 Load（收货地）", visible_loads(request.user, Shipment.objects.filter(consignee=obj))[:100])]
    can_edit = is_management(request.user) if kind == "trailer" else can_edit_ops(request.user)
    problems = obj.compliance_problems() if kind == "carrier" else []
    return render(request, "core/master_detail.html", {"kind": kind, "label": label, "obj": obj, "fields": fields, "related": related,
                                                       "problems": problems,
                                                       "can_edit": can_edit})


def master_edit(request, kind, pk=None):
    _need(is_management(request.user) if kind == "trailer" else can_edit_ops(request.user))
    model, form_class, label, _ = MASTER[kind]
    obj = get_object_or_404(model, pk=pk) if pk else model()
    title = f"编辑{label}" if pk else f"新建{label}"
    return _edit(request, form_class, obj, title, lambda o: reverse("master_detail", args=[kind, o.pk]),
                 cancel_url=reverse("master_list", args=[kind]))


# ---------------------------------------------------------------------------
# search, history, team
# ---------------------------------------------------------------------------
def search(request):
    from .views_workspace import search_loads, search_projects

    q = request.GET.get("q", "").strip()
    results = {}
    if len(q) >= 2:
        loads, filters, text = search_loads(q, limit=100, user=request.user)
        results["Load"] = list(loads)
        results["项目"] = list(search_projects(text, filters, 30, user=request.user))
        if text and not filters:
            results["客户"] = Customer.objects.filter(Q(name__icontains=text) | Q(code__icontains=text))[:20]
            results["承运商"] = Carrier.objects.filter(Q(name__icontains=text) | Q(code__icontains=text) | Q(mc_number__icontains=text))[:20]
            results["地址"] = Location.objects.filter(Q(code__icontains=text) | Q(name__icontains=text) | Q(address__icontains=text))[:20]
            sides = visible_sides(request.user)
            results["费用（发票号）"] = Charge.objects.filter(invoice_number__icontains=text, side__in=sides, shipment__in=visible_loads(request.user)).select_related("shipment")[:20]
    hits = [(k, v) for k, v in results.items() if len(v)]
    if len(hits) == 1 and len(hits[0][1]) == 1:
        return redirect(hits[0][1][0] if hits[0][0] != "费用（发票号）" else hits[0][1][0].shipment)
    return render(request, "core/search.html", {"q": q, "results": results})


@require_POST
def history_revert(request, key, history_id):
    _need(is_management(request.user))
    if key not in audit.TRACKED:
        raise Http404
    try:
        obj = audit.revert(key, history_id, request.user)
    except (ValueError, PermissionError) as e:
        messages.error(request, str(e) or "无法回滚")
        return redirect(request.META.get("HTTP_REFERER") or "/")
    messages.success(request, "已回滚该改动（回滚本身也记录在时间线里）")
    shipment = getattr(obj, "shipment", None) if not isinstance(obj, Shipment) else obj
    if isinstance(obj, MasterBL):
        shipment = obj.shipments.first()
    if isinstance(obj, Project):
        return redirect(obj)
    return redirect(reverse("shipment_detail", args=[shipment.pk]) + "#timeline" if shipment else "/")


def team(request):
    _need(is_management(request.user))
    User = get_user_model()
    if request.method == "POST":
        user = get_object_or_404(User, pk=request.POST.get("user"))
        roles = [r for r in request.POST.getlist("roles") if r in Role.values]
        if user == request.user and Role.MANAGEMENT not in roles:
            messages.error(request, "不能取消自己的管理层角色")
        else:
            profile, _ = Profile.objects.get_or_create(user=user)
            before = profile.role_set
            profile.set_roles(roles)
            profile.save()
            if profile.role_set != before:
                from .notify import notify

                label = profile.roles_label or "无（已停用）"
                notify(user, f"{request.user.get_full_name() or request.user.email} 把你的角色设为：{label}", "/", exclude=request.user)
            messages.success(request, f"已更新 {user.email or user.username} 的角色：{profile.roles_label or '未分配'}")
        return redirect("team")
    users = User.objects.order_by("email").select_related("profile")
    for u in users:
        u.role_set = {"MGMT"} if u.is_superuser else (u.profile.role_set if hasattr(u, "profile") else set())
    return render(request, "core/team.html", {"users": users, "roles": Role.choices})


# --- in-app notifications ---------------------------------------------------
def notifications(request):
    from .models import Notification

    qs = Notification.objects.filter(user=request.user)
    if request.method == "POST":
        qs.filter(read_at__isnull=True).update(read_at=timezone.now())
        return redirect("notifications")
    return render(request, "core/notifications.html", {"items": qs[:200]})


def notification_go(request, nid):
    from .models import Notification

    n = get_object_or_404(Notification, pk=nid, user=request.user)
    if n.read_at is None:
        n.read_at = timezone.now()
        n.save(update_fields=["read_at"])
    return redirect(n.url or "notifications")


def notification_poll(request):
    """New notices since the page loaded: pop-ups (and desktop alerts) while someone keeps a page open."""
    from django.http import JsonResponse

    from .models import Notification

    qs = Notification.objects.filter(user=request.user)
    new = list(qs.filter(shown_at__isnull=True, read_at__isnull=True)[:5])
    if new:
        Notification.objects.filter(pk__in=[n.pk for n in new]).update(shown_at=timezone.now())
    return JsonResponse({"unread": qs.filter(read_at__isnull=True).count(),
                         "new": [{"id": n.pk, "text": n.text, "url": reverse("notification_go", args=[n.pk])} for n in new]})


def import_upload(request):
    """Upload a JSON export (see core/importer.py) and import it — no server shell needed."""
    _need(is_management(request.user))
    output = None
    if request.method == "POST" and request.FILES.get("file"):
        from .importer import ImportError_, import_data, wipe_operational_data

        upload = request.FILES["file"]
        if upload.size > 20 * 1024 * 1024 or not upload.name.lower().endswith(".json"):
            messages.error(request, "请上传 20MB 以内的 .json 文件")
            return redirect("import_upload")
        lines = []
        try:
            data = json.loads(upload.read().decode("utf-8"))
            with transaction.atomic():
                if request.POST.get("wipe") == "1":
                    if request.POST.get("confirm", "").strip() != "DELETE":
                        raise ImportError_("要清空旧数据，请在确认框里输入 DELETE")
                    lines.append(wipe_operational_data())
                lines += import_data(data, user=request.user, dry_run=bool(request.POST.get("dry_run")))
        except (ValueError, ImportError_) as e:
            lines.append(f"导入失败：{e}")
        output = "\n".join(lines)
    return render(request, "core/import.html", {"output": output})


def healthz(request):
    return HttpResponse("ok")


class DevLoginView(auth_views.LoginView):
    template_name = "core/dev_login.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.TMS_PASSWORD_LOGIN:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


def set_language(request, code):
    """One-click switch between Chinese and English; remembered for a year in this browser."""
    from django.utils.http import url_has_allowed_host_and_scheme

    from .i18n import COOKIE

    nxt = request.GET.get("next") or request.META.get("HTTP_REFERER") or "/"
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        nxt = "/"
    resp = redirect(nxt)
    resp.set_cookie(COOKIE, "en" if code == "en" else "zh", max_age=365 * 24 * 3600, samesite="Lax")
    return resp
