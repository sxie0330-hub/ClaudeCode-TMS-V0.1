"""Pages for AI email reading: paste → review → apply."""
from collections import OrderedDict

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import ai
from .models import AiChange, AiExtraction, CargoLine, Carrier, Company, Customer, Location, Project, Shipment
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
            docs = []
            for f in request.FILES.getlist("files") + request.FILES.getlist("sheet"):
                mime = ai.doc_mime(f.name)
                if mime:
                    if f.size > ai.MAX_DOC_BYTES:
                        raise ai.AiError(f"「{f.name}」太大（超过 {ai.MAX_DOC_BYTES // 1024 // 1024}MB），请压缩或只保留相关页")
                    docs.append({"name": f.name, "mime": mime, "data": f.read()})
                else:
                    sheet = ai.sheet_to_text(f)
                    text = (text + "\n\n=== 附带表格：" + f.name + " ===\n" + sheet).strip()
            ext = ai.extract(text, request.user, project=project, shipment=shipment, attachments=docs)
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
        "companies": Company.choices, "equipments": Shipment.Equipment.choices, "handling_types": CargoLine.HandlingType.choices, "services": Shipment.Service.choices,
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


# ---------------------------------------------------------------------------
# AI usage (management only)
# ---------------------------------------------------------------------------
def ai_usage(request):
    """Gemini tokens and estimated cost by person and by feature, for one month, plus the last 12 months."""
    from datetime import date

    from django.conf import settings
    from django.utils import timezone

    from .models import AiUsage
    from .permissions import is_management

    _need(is_management(request.user))
    today = timezone.localdate()
    try:
        y, m = (int(x) for x in request.GET.get("month", "").split("-"))
        month = date(y, m, 1)
    except ValueError:
        month = today.replace(day=1)
    nxt = (month.replace(day=28) + timezone.timedelta(days=4)).replace(day=1)
    prev = (month - timezone.timedelta(days=1)).replace(day=1)

    def blank():
        return {"calls": 0, "input": 0, "output": 0, "thinking": 0, "cost": 0.0}

    def add(acc, r):
        acc["calls"] += 1
        acc["input"] += r.input_tokens
        acc["output"] += r.output_tokens
        acc["thinking"] += r.thinking_tokens
        acc["cost"] += ai.usage_cost(r.input_tokens, r.output_tokens + r.thinking_tokens, timezone.localtime(r.created_at).date())

    rows = AiUsage.objects.filter(created_at__date__gte=month, created_at__date__lt=nxt).select_related("user")
    total, by_user, by_feature = blank(), {}, {}
    labels = dict(AiUsage.Feature.choices)
    for r in rows:
        add(total, r)
        who = r.user.get_full_name() or r.user.email if r.user else "（系统 / 已删除用户）"
        add(by_user.setdefault(who, blank()), r)
        add(by_feature.setdefault(labels.get(r.feature, r.feature), blank()), r)
        by_user[who].setdefault("features", set()).add(labels.get(r.feature, r.feature))

    year_start = (month.replace(day=1) - timezone.timedelta(days=330)).replace(day=1)
    months = OrderedDict()
    for r in AiUsage.objects.filter(created_at__date__gte=year_start, created_at__date__lt=nxt).only(
            "created_at", "input_tokens", "output_tokens", "thinking_tokens"):
        key = timezone.localtime(r.created_at).strftime("%Y-%m")
        add(months.setdefault(key, blank()), r)
    for u in by_feature.values():
        u["avg"] = u["cost"] / u["calls"] if u["calls"] else 0
    pin, pout = ai.price_on(today)
    return render(request, "core/ai_usage.html", {
        "month": month, "prev": prev, "next": nxt if nxt <= today.replace(day=1) else None, "total": total,
        "by_user": sorted(by_user.items(), key=lambda kv: -kv[1]["cost"]),
        "by_feature": sorted(by_feature.items(), key=lambda kv: -kv[1]["cost"]),
        "months": sorted(months.items(), reverse=True), "model": settings.TMS_AI_MODEL, "price_in": pin, "price_out": pout,
        "prices": settings.TMS_AI_PRICES,
    })
