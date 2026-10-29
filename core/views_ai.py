"""Pages for AI email reading: paste → review → apply."""
from collections import OrderedDict

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import ai
from .models import AiChange, AiExtraction, Carrier, Company, Customer, Location, Project, Shipment
from .permissions import can_edit_ops
from .views import _need


def ai_new(request):
    _need(can_edit_ops(request.user))
    project = shipment = None
    src = request.POST if request.method == "POST" else request.GET
    if src.get("load"):
        shipment = get_object_or_404(Shipment, pk=src["load"])
    elif src.get("project"):
        project = get_object_or_404(Project, pk=src["project"])
    text = request.POST.get("text", "")
    if request.method == "POST" and ai.enabled():
        try:
            if request.FILES.get("sheet"):
                sheet = ai.sheet_to_text(request.FILES["sheet"])
                text = (text + "\n\n=== 附带表格：" + request.FILES["sheet"].name + " ===\n" + sheet).strip()
            ext = ai.extract(text, request.user, project=project, shipment=shipment)
        except ai.AiError as e:
            messages.error(request, str(e))
        else:
            return redirect(ext)
    return render(request, "core/ai_new.html", {
        "project": project, "shipment": shipment, "text": text, "enabled": ai.enabled(),
        "max_chars": ai.MAX_EMAIL_CHARS,
    })


def ai_review(request, pk):
    _need(can_edit_ops(request.user))
    ext = get_object_or_404(AiExtraction, pk=pk)
    changes = list(ext.changes.select_related("shipment", "leg", "project"))
    groups, pgroups = OrderedDict(), OrderedDict()
    for c in changes:
        c.blocked = ai.blocked_reason(c, request.user) if c.status == AiChange.State.PENDING else ""
        if c.shipment_id:
            groups.setdefault(c.shipment, []).append(c)
        else:
            pgroups.setdefault(c.project, []).append(c)
    props = ai.proposals(ext)
    created_ids = [p["project_id"] for p in props if p["state"] == "created"]
    created = list(Project.objects.filter(pk__in=created_ids))
    open_props = [p for p in props if p["state"] == "open"] if ext.status == AiExtraction.State.PENDING else []
    return render(request, "core/ai_review.html", {
        "ext": ext, "groups": groups, "pgroups": pgroups, "pending": any(c.status == AiChange.State.PENDING for c in changes),
        "props": open_props, "prop_total": len(props), "created": created,
        "open_projects": Project.objects.exclude(status__in=[Project.Status.DONE, Project.Status.CANCELLED]).order_by("-created_at")[:200] if open_props else [],
        "loc_data": list(Location.objects.values("code", "name", "address", "city", "state", "postal_code")) if open_props else [],
        "customer_names": list(Customer.objects.values_list("name", flat=True)) if open_props else [],
        "carrier_names": list(Carrier.objects.values_list("name", flat=True)) if open_props else [],
        "companies": Company.choices, "equipments": Shipment.Equipment.choices, "services": Shipment.Service.choices,
    })


@require_POST
def ai_create_project(request, pk):
    _need(can_edit_ops(request.user))
    ext = get_object_or_404(AiExtraction, pk=pk)
    if ext.status != AiExtraction.State.PENDING:
        messages.warning(request, "这次识别已经处理过了")
        return redirect(ext)
    if request.POST.get("action") == "skip":
        idx = request.POST.get("np_index", "0")
        ai.skip_project(ext, request.user, int(idx) if idx.isdigit() else 0)
        messages.info(request, "已跳过这个新项目")
        return redirect(ext)
    try:
        p = ai.create_project(ext, request.user, request.POST)
    except ai.AiError as e:
        messages.error(request, str(e))
        return redirect(ext)
    if request.POST.get("np_target", "new") != "new":
        messages.success(request, f"已加到项目 {p.number}，详见项目时间线")
    else:
        messages.success(request, f"已创建项目 {p.number}：{p.shipments.count()} 个 Load")
    ext.refresh_from_db()
    if ai.open_proposals(ext):  # more new projects from the same text are waiting
        return redirect(ext)
    return redirect(p)


@require_POST
def ai_apply(request, pk):
    _need(can_edit_ops(request.user))
    ext = get_object_or_404(AiExtraction, pk=pk)
    if ext.status != AiExtraction.State.PENDING:
        messages.warning(request, "这次识别已经处理过了")
        return redirect(ext)
    if request.POST.get("action") == "discard":
        ai.discard(ext, request.user)
        messages.info(request, "已放弃，没有写入任何内容")
        return redirect(ext)
    applied, skipped = ai.apply_changes(ext, request.user, request.POST.getlist("change"))
    if applied:
        messages.success(request, f"已写入 {len(applied)} 项")
    if skipped:
        messages.warning(request, f"{len(skipped)} 项未写入，原因见下表")
    if not applied and not skipped:
        messages.info(request, "没有勾选任何内容，全部标为未采用")
    return redirect(ext)


def ai_list(request):
    _need(can_edit_ops(request.user))
    mine = request.GET.get("all") != "1"
    qs = AiExtraction.objects.select_related("created_by", "project", "shipment")
    if mine:
        qs = qs.filter(created_by=request.user)
    return render(request, "core/ai_list.html", {"items": qs[:100], "mine": mine, "enabled": ai.enabled()})
