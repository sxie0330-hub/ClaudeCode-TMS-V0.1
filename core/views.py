import csv
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
    LocationForm,
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
    Location,
    MasterBL,
    Note,
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
    can_see_side,
    is_management,
    visible_sides,
)

PAGE_SIZE = 50


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
    qs = Shipment.objects.select_related("customer", "project", "master_bl", "consignee")
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
        "projects": Project.objects.filter(status=Project.Status.ACTIVE).select_related("customer"),
        "services": Shipment.Service.choices,
        "querystring": params.urlencode(),
        "g": request.GET,
        "today": timezone.localdate(),
    })


def shipment_export(request):
    qs = _shipment_filters(request, Shipment.objects.select_related("customer", "project", "master_bl", "consignee", "shipper"))
    qs = _with_money(qs, request.user).prefetch_related("legs__carrier", "units")
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="shipments-{timezone.localdate()}.csv"'
    resp.write("﻿")  # Excel/Sheets open UTF-8 correctly with a BOM
    w = csv.writer(resp)
    see_ar = can_see_side(request.user, "AR")
    see_ap = can_see_side(request.user, "AP")
    head = ["运单号", "状态", "客户", "项目", "Monday ID", "客户参考号", "服务", "MBL", "柜号", "柜型", "序列号", "ETA", "LFD",
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
    return render(request, "core/shipment_detail.html", {
        "s": s,
        "legs": s.legs.select_related("origin", "destination", "carrier"),
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
    project = Project.objects.filter(pk=request.GET.get("project")).first()
    if request.method == "POST":
        form = ShipmentForm(request.POST)
        if form.is_valid():
            s = form.save()
            _add_default_cargo(s)
            messages.success(request, f"已创建运单 {s.number}")
            return redirect(s)
    else:
        form = ShipmentForm(initial=_new_shipment_defaults(project))
    return _form_page(request, form, "新建运单", project.get_absolute_url() if project else reverse("shipment_list"))


def shipment_edit(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(can_edit_shipment_ops(request.user, s))
    return _edit(request, ShipmentForm, s, f"编辑运单 {s.number}", lambda o: o.get_absolute_url())


def shipment_docs(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(can_edit_ops(request.user))
    return _edit(request, ShipmentDocsForm, s, f"{s.number} 文件与还空", lambda o: o.get_absolute_url(),
                 intro="送达后仍可编辑。文件请存在 Google Drive，这里贴共享链接。")


def shipment_delete(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(is_management(request.user))
    return _confirm_delete(request, s, f"删除运单 {s.number}", reverse("shipment_list"), s.get_absolute_url())


@require_POST
def shipment_unlock(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(is_management(request.user))
    Shipment.objects.filter(pk=pk).update(is_locked=False)
    _note(s, request.user, "管理层解锁了运单（已送达/已结案的数据可再次编辑）")
    messages.warning(request, "已解锁。改完后请点“重新锁定”。")
    return redirect(s)


@require_POST
def shipment_lock(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    _need(is_management(request.user))
    Shipment.objects.filter(pk=pk).update(is_locked=True)
    _note(s, request.user, "管理层重新锁定了运单")
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
            messages.error(request, "只有已送达的运单可以结案")
            return redirect(s)
        s.closed_at = timezone.now()
        _note(s, request.user, "管理层结案")
    s.save()
    return redirect(s)


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
        messages.info(request, "请先在运单里填 MBL 号")
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


@require_POST
def bulk_bol(request):
    ids = request.POST.getlist("ids")
    shipments = list(Shipment.objects.filter(pk__in=ids).order_by("number"))
    if not shipments:
        messages.error(request, "请先勾选运单")
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
    qs = Project.objects.select_related("customer")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(number__icontains=q) | Q(monday_item_id__icontains=q) | Q(customer__name__icontains=q))
    if request.GET.get("status", "ACTIVE"):
        qs = qs.filter(status=request.GET.get("status", "ACTIVE"))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "core/project_list.html", {"page": page, "g": request.GET, "statuses": Project.Status.choices})


def project_detail(request, pk):
    p = get_object_or_404(Project.objects.select_related("customer", "default_shipper", "default_consignee"), pk=pk)
    shipments = p.shipments.select_related("master_bl", "consignee").prefetch_related("legs__carrier", "units")
    counts = {}
    for st in shipments.values_list("status", flat=True):
        counts[st] = counts.get(st, 0) + 1
    money = _project_money(p, request.user)
    return render(request, "core/project_detail.html", {
        "p": p,
        "shipments": shipments,
        "counts": [(Status(k).label, v) for k, v in sorted(counts.items(), key=lambda kv: list(Status.values).index(kv[0]))],
        "money": money,
        "timeline": audit.project_timeline(p, can_revert=is_management(request.user)),
        "note_form": NoteForm(),
    })


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
    initial = {"customer": request.GET.get("customer")} if request.GET.get("customer") else None
    if request.method == "POST":
        form = ProjectForm(request.POST)
        if form.is_valid():
            p = form.save()
            messages.success(request, f"已创建项目 {p.number}")
            return redirect(p)
    else:
        form = ProjectForm(initial=initial)
    return _form_page(request, form, "新建项目", reverse("project_list"))


def project_edit(request, pk):
    p = get_object_or_404(Project, pk=pk)
    _need(can_edit_ops(request.user))
    return _edit(request, ProjectForm, p, f"编辑项目 {p.number}", lambda o: o.get_absolute_url())


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
            messages.success(request, f"已创建 {len(created)} 票运单" + (f"；跳过已存在的柜号：{', '.join(skipped)}" if skipped else ""))
            return redirect(p)
    else:
        form = BulkShipmentForm()
    return _form_page(request, form, f"{p.name} · 批量新建运单", p.get_absolute_url(),
                      intro="每行一个柜子。客户、项目、默认提货地/收货地和货物描述会自动从项目带入。")


# ---------------------------------------------------------------------------
# master data
# ---------------------------------------------------------------------------
MASTER = {
    "customer": (Customer, CustomerForm, "客户", ["name", "code", "contact_name", "contact_phone"]),
    "carrier": (Carrier, CarrierForm, "承运商", ["name", "code", "kind", "scac", "contact_name", "contact_phone"]),
    "location": (Location, LocationForm, "地址", ["code", "name", "kind", "full_address", "contact_phone"]),
}


def master_list(request, kind):
    model, _, label, cols = MASTER[kind]
    qs = model.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        fields = {"customer": ["name", "code"], "carrier": ["name", "code", "scac", "mc_number"], "location": ["code", "name", "address", "city"]}[kind]
        cond = Q()
        for f in fields:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    headers = [model._meta.get_field(c).verbose_name if c != "full_address" else "地址" for c in cols]
    rows = [(o, [o.get_kind_display() if c == "kind" else getattr(o, c) for c in cols]) for o in page.object_list]
    return render(request, "core/master_list.html", {"kind": kind, "label": label, "page": page, "headers": headers, "rows": rows, "g": request.GET})


def master_detail(request, kind, pk):
    model, _, label, _ = MASTER[kind]
    obj = get_object_or_404(model, pk=pk)
    fields = [(f.verbose_name, obj._get_FIELD_display(f) if f.choices else getattr(obj, f.name)) for f in model._meta.fields
              if f.name not in ("id", "is_deleted", "created_at", "updated_at")]
    related = []
    if kind == "customer":
        related = [("项目", obj.projects.all()[:100]), ("最近运单", obj.shipments.select_related("project")[:100])]
    elif kind == "carrier":
        related = [("最近运输段", Leg.objects.filter(carrier=obj).select_related("shipment", "origin", "destination").order_by("-id")[:100])]
    elif kind == "location":
        related = [("最近运单（收货地）", Shipment.objects.filter(consignee=obj)[:100])]
    return render(request, "core/master_detail.html", {"kind": kind, "label": label, "obj": obj, "fields": fields, "related": related})


def master_edit(request, kind, pk=None):
    _need(can_edit_ops(request.user))
    model, form_class, label, _ = MASTER[kind]
    obj = get_object_or_404(model, pk=pk) if pk else model()
    title = f"编辑{label}" if pk else f"新建{label}"
    return _edit(request, form_class, obj, title, lambda o: reverse("master_detail", args=[kind, o.pk]),
                 cancel_url=reverse("master_list", args=[kind]))


# ---------------------------------------------------------------------------
# search, history, team
# ---------------------------------------------------------------------------
def search(request):
    q = request.GET.get("q", "").strip()
    results = {}
    if len(q) >= 2:
        results["运单"] = _shipment_filters(request, Shipment.objects.select_related("customer", "project", "master_bl"))[:50]
        results["项目"] = Project.objects.filter(Q(name__icontains=q) | Q(number__icontains=q) | Q(monday_item_id__icontains=q))[:20]
        results["客户"] = Customer.objects.filter(Q(name__icontains=q) | Q(code__icontains=q))[:20]
        results["承运商"] = Carrier.objects.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(mc_number__icontains=q))[:20]
        results["地址"] = Location.objects.filter(Q(code__icontains=q) | Q(name__icontains=q) | Q(address__icontains=q))[:20]
        sides = visible_sides(request.user)
        results["费用（发票号）"] = Charge.objects.filter(invoice_number__icontains=q, side__in=sides).select_related("shipment")[:20]
    if len(results.get("运单", [])) == 1 and not any(len(v) for k, v in results.items() if k != "运单"):
        return redirect(results["运单"][0])
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
        role = request.POST.get("role", "")
        if role not in ("",) + tuple(Role.values):
            raise Http404
        if user == request.user and role != Role.MANAGEMENT:
            messages.error(request, "不能取消自己的管理层角色")
        else:
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.role = role
            profile.save()
            messages.success(request, f"已更新 {user.email or user.username} 的角色")
        return redirect("team")
    users = User.objects.order_by("email").select_related("profile")
    return render(request, "core/team.html", {"users": users, "roles": Role.choices})


def import_trina_upload(request):
    """Upload the Trina workbook and import it — no server shell needed."""
    _need(is_management(request.user))
    output = None
    if request.method == "POST" and request.FILES.get("file"):
        import io
        import tempfile

        from django.core.management import call_command

        upload = request.FILES["file"]
        if upload.size > 20 * 1024 * 1024 or not upload.name.lower().endswith(".xlsx"):
            messages.error(request, "请上传 20MB 以内的 .xlsx 文件")
            return redirect("import_trina")
        out = io.StringIO()
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as tmp:
            for chunk in upload.chunks():
                tmp.write(chunk)
            tmp.flush()
            try:
                call_command("import_trina", tmp.name, dry_run=bool(request.POST.get("dry_run")), stdout=out)
            except Exception as e:  # show the reason instead of a 500 page
                out.write(f"导入失败：{e}")
        output = out.getvalue()
    return render(request, "core/import.html", {"output": output})


def healthz(request):
    return HttpResponse("ok")


class DevLoginView(auth_views.LoginView):
    template_name = "core/dev_login.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.TMS_PASSWORD_LOGIN:
            raise Http404
        return super().dispatch(request, *args, **kwargs)
