"""Personal workspace: widget home, 今日待办, rule settings, global search suggest."""
import json
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import alerts as alert_engine
from . import widgets as W
from .models import AlertSetting, AlertSnooze, Carrier, Charge, Customer, CustomRule, Location, Profile, Project, Shipment, Status
from .permissions import visible_sides


# --------------------------------------------------------------------------
# Home
# --------------------------------------------------------------------------
def home(request):
    board, widgets, todo = W.render_board(request.user)
    catalog = [w for w in W.CATALOG if w.allowed(request.user)]
    return render(request, "core/home.html", {
        "widgets": widgets,
        "catalog": catalog,
        "sizes": W.SIZE_LABELS,
        "layout_json": json.dumps(board.layout),
        "catalog_json": json.dumps({w.key: {"label": w.label, "sizes": w.sizes, "default": w.default} for w in catalog}),
        "counts": alert_engine.counts(todo),
        "today": timezone.localdate(),
    })


@require_POST
def dashboard_save(request):
    try:
        layout = json.loads(request.body.decode() or "[]")
    except ValueError:
        return JsonResponse({"ok": False}, status=400)
    board = W.get_layout(request.user)
    old = {w["id"]: w for w in board.layout}
    cleaned = W.clean_layout(request.user, layout)
    for w in cleaned:  # the browser never edits config; keep what the server has
        if w["id"] in old:
            w["config"] = old[w["id"]].get("config", {})
        elif w["type"] == "my_projects" and not w["config"]:
            w["config"] = {"columns": W.DEFAULT_PROJECT_COLUMNS, "scope": "mine", "statuses": ["PENDING", "ACTIVE", "DELIVERED"]}
    board.layout = cleaned
    board.save(update_fields=["layout", "updated_at"])
    return JsonResponse({"ok": True})


@require_POST
def dashboard_reset(request):
    board = W.get_layout(request.user)
    board.layout = []
    board.save(update_fields=["layout"])
    W.get_layout(request.user)
    messages.success(request, "已恢复默认布局")
    return redirect("home")


def widget_config(request, wid):
    board = W.get_layout(request.user)
    w = next((x for x in board.layout if x["id"] == wid), None)
    if not w:
        raise Http404
    meta = W.CATALOG_MAP[w["type"]]
    cfg = dict(w.get("config", {}))
    if request.method == "POST":
        p = request.POST
        if w["type"] == "my_projects":
            cfg["columns"] = [c for c in p.getlist("columns") if c in dict(W.PROJECT_COLUMNS)] or W.DEFAULT_PROJECT_COLUMNS
            cfg["scope"] = "all" if p.get("scope") == "all" else "mine"
            cfg["statuses"] = [s for s in p.getlist("statuses") if s in Project.Status.values] or ["PENDING", "ACTIVE", "DELIVERED"]
        elif w["type"] == "project":
            pid = p.get("project", "")
            cfg["project"] = int(pid) if pid.isdigit() else None
        elif w["type"] == "note":
            cfg["text"] = p.get("text", "")[:2000]
        elif w["type"] == "saved_view":
            cfg["title"] = p.get("title", "")[:60]
            cfg["query"] = p.get("query", "").lstrip("?")[:500]
        if "scope" in p and w["type"] not in ("my_projects",):
            cfg["scope"] = "all" if p.get("scope") == "all" else "mine"
        w["config"] = cfg
        board.save(update_fields=["layout"])
        messages.success(request, "组件已更新")
        return redirect("home")
    return render(request, "core/widget_config.html", {
        "w": w, "meta": meta, "cfg": cfg,
        "columns": W.PROJECT_COLUMNS, "statuses": Project.Status.choices,
        "projects": Project.objects.exclude(status__in=[Project.Status.CANCELLED]).order_by("-created_at")[:200],
    })


@require_POST
def pin_project(request, pk):
    p = get_object_or_404(Project, pk=pk)
    board = W.get_layout(request.user)
    if not any(x["type"] == "project" and x.get("config", {}).get("project") == p.pk for x in board.layout):
        board.layout.insert(0, W.new_widget("project", "M", {"project": p.pk}))
        board.save(update_fields=["layout"])
    messages.success(request, f"已把「{p.name}」置顶到工作台")
    return redirect(p)


@require_POST
def watch_load(request, pk):
    s = get_object_or_404(Shipment, pk=pk)
    board = W.get_layout(request.user)
    w = next((x for x in board.layout if x["type"] == "watched_loads"), None)
    if not w:
        w = W.new_widget("watched_loads", "M", {"loads": []})
        board.layout.insert(0, w)
    ids = w["config"].setdefault("loads", [])
    if s.pk in ids:
        ids.remove(s.pk)
        messages.info(request, "已取消关注")
    else:
        ids.insert(0, s.pk)
        del ids[20:]
        messages.success(request, "已关注，首页「关注的 Load」组件里可以看到")
    board.save(update_fields=["layout"])
    return redirect(s)


# --------------------------------------------------------------------------
# 今日待办
# --------------------------------------------------------------------------
def todo(request):
    show = request.GET.get("show", "")
    items = alert_engine.collect(request.user, include_snoozed=True)
    active = [a for a in items if not a.snoozed]
    snoozed = [a for a in items if a.snoozed]
    level = request.GET.get("level", "")
    listed = snoozed if show == "snoozed" else active
    if level:
        listed = [a for a in listed if a.level == level]
    return render(request, "core/todo.html", {
        "items": listed, "counts": alert_engine.counts(active), "n_all": len(active), "n_snoozed": len(snoozed),
        "level": level, "show": show, "levels": alert_engine.LEVELS,
    })


@require_POST
def snooze(request):
    key = request.POST.get("key", "")[:120]
    days = request.POST.get("days", "1")
    if not key:
        raise Http404
    if days == "0":
        AlertSnooze.objects.filter(user=request.user, key=key).delete()
    else:
        until = timezone.now() + timedelta(days=min(int(days), 365) if days.isdigit() else 1)
        AlertSnooze.objects.update_or_create(user=request.user, key=key, defaults={"until": until})
    return redirect(request.POST.get("next") or reverse("todo"))


def alert_settings(request):
    user = request.user
    if request.method == "POST":
        p = request.POST
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.email_digest = p.get("email_digest") == "on"
        profile.save(update_fields=["email_digest"])
        for st in alert_engine.effective_settings(user):
            r = st["rule"]
            thr = p.get(f"{r.key}_threshold")
            AlertSetting.objects.update_or_create(user=user, rule=r.key, defaults={
                "enabled": p.get(f"{r.key}_enabled") == "on",
                "threshold": int(thr) if thr and thr.lstrip("-").isdigit() else r.threshold,
                "scope": "all" if p.get(f"{r.key}_scope") == "all" else "mine",
                "level": p.get(f"{r.key}_level") if p.get(f"{r.key}_level") in dict(alert_engine.LEVELS) else r.level,
            })
        messages.success(request, "待办规则已保存")
        return redirect("alert_settings")
    settings_ = alert_engine.effective_settings(user)
    groups = {}
    for st in settings_:
        groups.setdefault(st["rule"].category, []).append(st)
    profile = Profile.objects.filter(user=user).first()
    return render(request, "core/alert_settings.html", {
        "groups": groups.items(), "levels": alert_engine.LEVELS,
        "email_digest": profile.email_digest if profile else True, "email_enabled": settings.TMS_EMAIL_ENABLED,
        "custom": CustomRule.objects.filter(user=user), "fields": CustomRule.Field.choices,
    })


@require_POST
def alert_settings_reset(request):
    AlertSetting.objects.filter(user=request.user).delete()
    messages.success(request, "已恢复默认规则")
    return redirect("alert_settings")


@require_POST
def custom_rule_add(request):
    p = request.POST
    f1, v1 = p.get("field1", ""), p.get("value1", "").strip()
    if f1 not in CustomRule.Field.values or not v1:
        messages.error(request, "请至少填写一个条件")
        return redirect("alert_settings")
    f2 = p.get("field2", "") if p.get("field2", "") in CustomRule.Field.values else ""
    CustomRule.objects.create(
        user=request.user, name=(p.get("name") or f"{dict(CustomRule.Field.choices)[f1]} = {v1}")[:120],
        field1=f1, value1=v1[:120], field2=f2, value2=p.get("value2", "").strip()[:120] if f2 else "",
        level=p.get("level") if p.get("level") in dict(alert_engine.LEVELS) else "today",
    )
    messages.success(request, "已添加自定义规则")
    return redirect(reverse("alert_settings") + "#custom")


@require_POST
def custom_rule_delete(request, pk):
    CustomRule.objects.filter(user=request.user, pk=pk).delete()
    return redirect(reverse("alert_settings") + "#custom")


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
PREFIXES = {
    "客户": "customer", "customer": "customer", "承运商": "carrier", "carrier": "carrier",
    "状态": "status", "status": "status", "项目": "project", "project": "project",
    "柜号": "container", "container": "container", "pod": "pod", "POD": "pod", "公司": "company",
}


def parse_query(q):
    """'客户:Hithium 承运商:Harris 4475' → ({'customer': 'Hithium', 'carrier': 'Harris'}, '4475')"""
    filters, words = {}, []
    for token in q.replace("：", ":").split():
        if ":" in token:
            k, _, v = token.partition(":")
            if k in PREFIXES and v:
                filters[PREFIXES[k]] = v
                continue
        words.append(token)
    return filters, " ".join(words)


def search_loads(q, limit=50):
    filters, text = parse_query(q)
    qs = Shipment.objects.select_related("project", "customer", "master_bl")
    if text:
        qs = qs.filter(
            Q(number__icontains=text) | Q(container_number__icontains=text) | Q(master_bl__number__icontains=text)
            | Q(customer_ref__icontains=text) | Q(monday_item_id__icontains=text) | Q(seal_number__icontains=text)
            | Q(units__serial_number__icontains=text) | Q(legs__carrier_load_number__icontains=text)
            | Q(legs__bol_number__icontains=text) | Q(project__name__icontains=text) | Q(project__monday_item_id__icontains=text)
            | Q(customer__name__icontains=text)
        )
    if "customer" in filters:
        qs = qs.filter(customer__name__icontains=filters["customer"])
    if "carrier" in filters:
        qs = qs.filter(Q(legs__carrier__name__icontains=filters["carrier"]) | Q(legs__carrier__code__iexact=filters["carrier"]))
    if "project" in filters:
        qs = qs.filter(Q(project__name__icontains=filters["project"]) | Q(project__number__icontains=filters["project"]))
    if "container" in filters:
        qs = qs.filter(container_number__icontains=filters["container"])
    if "company" in filters:
        qs = qs.filter(project__company__iexact=filters["company"])
    if "status" in filters:
        v = filters["status"]
        qs = qs.filter(status__in=[k for k, label in Status.choices if v.lower() in (k.lower(), label)] or ["__none__"])
    if "pod" in filters:
        qs = qs.filter(pod_received__isnull=filters["pod"] in ("未收", "no", "无", "missing"))
    return qs.distinct()[:limit], filters, text


def search_projects(text, filters, limit=10):
    qs = Project.objects.select_related("customer")
    if text:
        qs = qs.filter(Q(name__icontains=text) | Q(number__icontains=text) | Q(monday_item_id__icontains=text)
                       | Q(customer__name__icontains=text) | Q(notes__icontains=text) | Q(terms__icontains=text))
    if "customer" in filters:
        qs = qs.filter(customer__name__icontains=filters["customer"])
    if "company" in filters:
        qs = qs.filter(company__iexact=filters["company"])
    if "project" in filters:
        qs = qs.filter(name__icontains=filters["project"])
    return qs[:limit]


def suggest(request):
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"groups": []})
    loads, filters, text = search_loads(q, limit=6)
    groups = []
    projects = list(search_projects(text, filters, 4)) if (text or filters.keys() & {"customer", "company", "project"}) else []
    if projects:
        groups.append({"label": "项目", "items": [
            {"title": p.name, "sub": f"{p.customer.name} · {p.company} · {p.get_status_display()}", "url": p.get_absolute_url(), "tag": "项目"}
            for p in projects]})
    loads = list(loads)
    if loads:
        groups.append({"label": "Load", "items": [
            {"title": s.container_number or s.number,
             "sub": " · ".join(x for x in [s.number, s.get_status_display(), s.project.name if s.project else s.customer.name] if x),
             "url": s.get_absolute_url(), "tag": "Load"} for s in loads]})
    if text and not filters:
        cust = Customer.objects.filter(Q(name__icontains=text) | Q(code__icontains=text))[:3]
        carr = Carrier.objects.filter(Q(name__icontains=text) | Q(code__icontains=text) | Q(mc_number__icontains=text))[:3]
        locs = Location.objects.filter(Q(code__icontains=text) | Q(name__icontains=text) | Q(address__icontains=text))[:3]
        if cust:
            groups.append({"label": "客户", "items": [{"title": c.name, "sub": "", "url": c.get_absolute_url(), "tag": "客户"} for c in cust]})
        if carr:
            groups.append({"label": "承运商", "items": [{"title": c.name, "sub": c.code, "url": c.get_absolute_url(), "tag": "承运商"} for c in carr]})
        if locs:
            groups.append({"label": "地址", "items": [{"title": l.code, "sub": l.full_address, "url": l.get_absolute_url(), "tag": "地址"} for l in locs]})
        sides = visible_sides(request.user)
        inv = Charge.objects.filter(invoice_number__icontains=text, side__in=sides).select_related("shipment")[:3]
        if inv:
            groups.append({"label": "发票", "items": [
                {"title": c.invoice_number, "sub": f"{c.side} · {c.shipment.container_number or c.shipment.number}",
                 "url": c.shipment.get_absolute_url() + "#charges", "tag": "发票"} for c in inv]})
    return JsonResponse({"groups": groups, "all_url": reverse("search") + "?q=" + q})


@require_POST
def digest_test(request):
    from . import digest

    if not settings.TMS_EMAIL_ENABLED:
        messages.error(request, "邮件还没有开通，请管理员按 docs/notifications.md 设置")
    elif not request.user.email:
        messages.error(request, "你的账号没有邮箱")
    else:
        try:
            sent = digest.send_to(request.user)
        except Exception as e:  # noqa: BLE001
            messages.error(request, f"发送失败：{e}")
        else:
            if sent:
                messages.success(request, f"已发送到 {request.user.email}")
            else:
                messages.info(request, "你今天没有待办，所以不会发邮件（正式发送时也一样）")
    return redirect("alert_settings")


@csrf_exempt
@require_POST
def task_daily_digest(request):
    """Called by Cloud Scheduler with header X-TMS-Task-Key."""
    from django.utils.crypto import constant_time_compare

    from . import digest

    key = settings.TMS_TASK_KEY
    if not key or not constant_time_compare(request.headers.get("X-TMS-Task-Key", ""), key):
        return JsonResponse({"error": "forbidden"}, status=403)
    if not settings.TMS_EMAIL_ENABLED:
        return JsonResponse({"error": "email not configured"}, status=503)
    return JsonResponse(digest.send_all())
