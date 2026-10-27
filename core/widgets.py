"""Home-screen widgets. Each user arranges their own board (core.models.Dashboard).

A widget is {"id", "type", "size", "config"}. Sizes are grid spans on a
4-column board: S 1×1, M 2×1, L 2×2, W 4×1 (full row), XL 4×2.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from . import alerts as alert_engine
from .models import Charge, Dashboard, Leg, Project, Shipment, Status
from .permissions import can_edit_ops, can_see_margin, visible_sides

SIZES = {"S": (1, 1), "M": (2, 1), "L": (2, 2), "W": (4, 1), "XL": (4, 2)}
SIZE_LABELS = {"S": "小", "M": "中", "L": "大", "W": "整行", "XL": "超大"}

PROJECT_COLUMNS = [
    ("number", "编号"), ("name", "项目"), ("customer", "客户"), ("company", "公司"), ("status", "状态"),
    ("start", "开始"), ("end", "结束"), ("owners", "负责人"), ("monday", "Monday ID"),
    ("loads", "Load 数"), ("picked", "已提货"), ("delivered", "已送达"), ("pod", "POD"),
    ("alerts", "待办"), ("ar", "应收"), ("ap", "应付"), ("margin", "毛利"),
]
DEFAULT_PROJECT_COLUMNS = ["name", "customer", "company", "status", "end", "owners", "delivered", "pod", "alerts"]


class W:
    def __init__(self, key, label, sizes, default, desc, allowed=None, configurable=False):
        self.key, self.label, self.sizes, self.default, self.desc = key, label, sizes, default, desc
        self.allowed = allowed or (lambda u: True)
        self.configurable = configurable


def _ap(u):
    return "AP" in visible_sides(u)


def _ar(u):
    return "AR" in visible_sides(u)


CATALOG = [
    W("todo", "今日待办", ["M", "L", "XL"], "L", "按紧急程度排列的待办"),
    W("my_projects", "我的项目", ["W", "XL"], "XL", "列和范围都可以自己选", configurable=True),
    W("project", "置顶项目", ["S", "M", "L"], "M", "盯一个项目的进度", configurable=True),
    W("watched_loads", "关注的 Load", ["M", "L"], "M", "在 Load 页点「关注」加进来"),
    W("lfd", "LFD 倒计时", ["S"], "S", "3 天内到期、还没提的柜子"),
    W("appts", "今日 / 明日预约", ["S", "M", "L"], "S", "提货和送货"),
    W("pod_pending", "POD 待收", ["S", "M"], "S", "已送达、POD 还没收到"),
    W("in_transit", "在途 Load", ["S", "M", "L"], "S", "已提货未送达"),
    W("in_yard", "在堆场", ["S", "M", "L"], "S", "堆场里的柜子"),
    W("extra_charges", "待审额外费用", ["S", "M"], "S", "承运商附加费待审 / 争议", allowed=_ap),
    W("ap_invoice", "承运商发票未到", ["S"], "S", "已送达但没有承运商发票号", allowed=_ap),
    W("ar_uninvoiced", "应收未开票", ["S"], "S", "已送达但应收还没开票", allowed=_ar),
    W("margin_month", "本月毛利", ["S", "M"], "S", "本月送达 Load 的应收 − 应付", allowed=can_see_margin),
    W("by_customer", "按客户汇总", ["M", "L"], "M", "各客户进行中的 Load"),
    W("quick", "快捷操作", ["S", "M"], "S", "新建项目 / Load 等"),
    W("note", "便签", ["S", "M", "L"], "S", "自己的备忘", configurable=True),
    W("saved_view", "保存的筛选", ["M", "L", "W", "XL"], "L", "把常用的 Load 筛选条件钉在首页", configurable=True),
]
CATALOG_MAP = {w.key: w for w in CATALOG}

DEFAULT_LAYOUT = [
    ("todo", "L", {}), ("pod_pending", "S", {}), ("lfd", "S", {}), ("appts", "S", {}), ("in_transit", "S", {}),
    ("my_projects", "XL", {"columns": DEFAULT_PROJECT_COLUMNS, "scope": "mine", "statuses": ["PENDING", "ACTIVE", "DELIVERED"]}),
    ("extra_charges", "S", {}), ("quick", "S", {}),
]


def new_widget(kind, size=None, config=None):
    w = CATALOG_MAP[kind]
    return {"id": uuid.uuid4().hex[:8], "type": kind, "size": size if size in w.sizes else w.default, "config": config or {}}


def get_layout(user):
    board, created = Dashboard.objects.get_or_create(user=user)
    if created or not board.layout:
        board.layout = [new_widget(k, s, c) for k, s, c in DEFAULT_LAYOUT if CATALOG_MAP[k].allowed(user)]
        board.save(update_fields=["layout"])
    return board


def clean_layout(user, layout):
    """Validate a layout posted by the browser."""
    out, seen = [], set()
    for item in layout if isinstance(layout, list) else []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind not in CATALOG_MAP or not CATALOG_MAP[kind].allowed(user):
            continue
        wid = str(item.get("id") or uuid.uuid4().hex[:8])[:16]
        if wid in seen:
            continue
        seen.add(wid)
        size = item.get("size") if item.get("size") in CATALOG_MAP[kind].sizes else CATALOG_MAP[kind].default
        config = item.get("config") if isinstance(item.get("config"), dict) else {}
        out.append({"id": wid, "type": kind, "size": size, "config": config})
    return out[:40]


# --------------------------------------------------------------------------
# Data for each widget
# --------------------------------------------------------------------------
def _loads(user, scope="mine"):
    qs = Shipment.objects.exclude(status__in=[Status.CLOSED, Status.CANCELLED])
    if scope == "mine":
        qs = qs.filter(project__owners=user)
    return qs


def _project_rows(user, config):
    cols = [c for c in config.get("columns") or DEFAULT_PROJECT_COLUMNS if c in dict(PROJECT_COLUMNS)]
    sides = visible_sides(user)
    cols = [c for c in cols if not (c == "ar" and "AR" not in sides) and not (c == "ap" and "AP" not in sides)
            and not (c == "margin" and not can_see_margin(user))]
    statuses = config.get("statuses") or ["PENDING", "ACTIVE", "DELIVERED"]
    qs = Project.objects.filter(status__in=statuses).select_related("customer").prefetch_related("owners")
    if config.get("scope", "mine") == "mine":
        qs = qs.filter(owners=user)
    qs = qs.annotate(
        n_loads=Count("shipments", filter=Q(shipments__is_deleted=False) & ~Q(shipments__status=Status.CANCELLED), distinct=True),
        n_delivered=Count("shipments", filter=Q(shipments__is_deleted=False, shipments__status__in=[Status.DELIVERED, Status.CLOSED]), distinct=True),
        n_picked=Count("shipments", filter=Q(shipments__is_deleted=False, shipments__status__in=[Status.IN_TRANSIT, Status.IN_YARD, Status.DELIVERED, Status.CLOSED]), distinct=True),
        n_pod=Count("shipments", filter=Q(shipments__is_deleted=False, shipments__pod_received__isnull=False), distinct=True),
    ).order_by("end_date", "-created_at")[:50]
    projects = list(qs)
    money = {}
    if any(c in cols for c in ("ar", "ap", "margin")):
        for row in Charge.objects.filter(shipment__project__in=projects).exclude(state=Charge.State.VOID).values("shipment__project", "side").annotate(t=Sum("amount")):
            money.setdefault(row["shipment__project"], {})[row["side"]] = row["t"] or Decimal("0")
    alert_counts = {}
    if "alerts" in cols:
        for a in alert_engine.collect(user):
            if a.project:
                alert_counts[a.project.pk] = alert_counts.get(a.project.pk, 0) + 1
    rows = []
    for p in projects:
        m = money.get(p.pk, {})
        rows.append({
            "p": p,
            "cells": {
                "number": p.number, "name": p.name, "customer": p.customer.name, "company": p.company,
                "status": p.get_status_display(), "start": p.start_date, "end": p.end_date,
                "owners": ", ".join((u.get_full_name() or u.email or u.username) for u in p.owners.all()) or "未指定",
                "monday": p.monday_item_id, "loads": p.n_loads,
                "picked": f"{p.n_picked}/{p.n_loads}", "delivered": f"{p.n_delivered}/{p.n_loads}",
                "pod": f"{p.n_pod}/{p.n_loads}", "alerts": alert_counts.get(p.pk, 0),
                "ar": m.get("AR"), "ap": m.get("AP"),
                "margin": (m.get("AR", 0) - m.get("AP", 0)) if m else None,
            },
        })
    return {"columns": [(c, dict(PROJECT_COLUMNS)[c]) for c in cols], "rows": rows, "scope": config.get("scope", "mine")}


def widget_data(user, w, todo=None):
    kind, cfg = w["type"], w.get("config", {})
    today = timezone.localdate()
    scope = cfg.get("scope", "mine")
    if kind == "todo":
        items = todo if todo is not None else alert_engine.collect(user)
        limit = {"M": 3, "L": 7, "XL": 10}.get(w["size"], 5)
        return {"items": items[:limit], "total": len(items), "counts": alert_engine.counts(items)}
    if kind == "my_projects":
        return _project_rows(user, cfg)
    if kind == "project":
        p = Project.objects.filter(pk=cfg.get("project")).first()
        if not p:
            return {"missing": True}
        loads = list(p.shipments.exclude(status=Status.CANCELLED).prefetch_related("legs"))
        n = len(loads)
        data = {"p": p, "n": n, "delivered": sum(1 for s in loads if s.status in (Status.DELIVERED, Status.CLOSED)),
                "pod": sum(1 for s in loads if s.pod_received)}
        if can_see_margin(user):
            agg = Charge.objects.filter(shipment__project=p).exclude(state=Charge.State.VOID).values("side").annotate(t=Sum("amount"))
            m = {r["side"]: r["t"] for r in agg}
            data["margin"] = (m.get("AR") or 0) - (m.get("AP") or 0)
        data["pct"] = int(data["delivered"] * 100 / n) if n else 0
        return data
    if kind == "watched_loads":
        ids = cfg.get("loads") or []
        return {"loads": list(Shipment.objects.filter(pk__in=ids).select_related("project").prefetch_related("legs__carrier"))}
    if kind == "lfd":
        qs = _loads(user, scope).filter(lfd__lte=today + timedelta(days=3), status__in=alert_engine.NOT_PICKED)
        return {"count": qs.count(), "items": list(qs.order_by("lfd")[:5])}
    if kind == "appts":
        days = [today, today + timedelta(days=1)]
        legs = Leg.objects.filter(shipment__in=_loads(user, scope)).filter(
            Q(scheduled_pickup__in=days, actual_pickup__isnull=True) | Q(scheduled_delivery__in=days, actual_delivery__isnull=True)
        ).select_related("shipment", "origin", "destination", "carrier")
        legs = list(legs[:30])
        return {"count": len(legs), "items": legs[:8], "today": today}
    if kind == "pod_pending":
        qs = _loads(user, scope).filter(status=Status.DELIVERED, pod_received__isnull=True)
        return {"count": qs.count(), "projects": qs.values("project").distinct().count(), "items": list(qs.select_related("project")[:6])}
    if kind == "in_transit":
        qs = _loads(user, scope).filter(status=Status.IN_TRANSIT)
        return {"count": qs.count(), "items": list(qs.select_related("project").prefetch_related("legs__carrier")[:8])}
    if kind == "in_yard":
        qs = _loads(user, scope).filter(status=Status.IN_YARD)
        return {"count": qs.count(), "items": list(qs.select_related("project")[:8])}
    if kind == "extra_charges":
        qs = Charge.objects.filter(shipment__in=_loads(user, scope), side="AP", charge_type__in=alert_engine.ACCESSORIAL,
                                   state__in=[Charge.State.OPEN, Charge.State.DISPUTED])
        return {"count": qs.count(), "total": qs.aggregate(t=Sum("amount"))["t"] or 0, "items": list(qs.select_related("shipment", "carrier")[:6])}
    if kind == "ap_invoice":
        qs = Charge.objects.filter(shipment__in=_loads(user, scope).filter(status=Status.DELIVERED), side="AP", state=Charge.State.OPEN, invoice_number="")
        return {"count": qs.values("shipment").distinct().count()}
    if kind == "ar_uninvoiced":
        qs = Charge.objects.filter(shipment__in=_loads(user, scope).filter(status=Status.DELIVERED), side="AR", state=Charge.State.OPEN)
        return {"count": qs.values("shipment").distinct().count(), "total": qs.aggregate(t=Sum("amount"))["t"] or 0}
    if kind == "margin_month":
        start = today.replace(day=1)
        loads = Shipment.objects.filter(legs__actual_delivery__gte=start, status__in=[Status.DELIVERED, Status.CLOSED]).distinct()
        agg = Charge.objects.filter(shipment__in=loads).exclude(state=Charge.State.VOID).values("side").annotate(t=Sum("amount"))
        m = {r["side"]: r["t"] or Decimal("0") for r in agg}
        ar, ap = m.get("AR", Decimal("0")), m.get("AP", Decimal("0"))
        return {"ar": ar, "ap": ap, "margin": ar - ap, "pct": ((ar - ap) / ar * 100) if ar else None, "month": start}
    if kind == "by_customer":
        rows = _loads(user, scope).values("customer__name").annotate(n=Count("id")).order_by("-n")[:8]
        return {"rows": list(rows)}
    if kind == "quick":
        return {"can_edit": can_edit_ops(user)}
    if kind == "note":
        return {"text": cfg.get("text", "")}
    if kind == "saved_view":
        from .views import _shipment_filters  # local import: views imports this module
        from django.http import QueryDict

        q = QueryDict(cfg.get("query", ""), mutable=False)
        req = type("R", (), {"GET": q})()
        qs = _shipment_filters(req, Shipment.objects.select_related("project", "customer").prefetch_related("legs__carrier"))
        limit = {"M": 3, "L": 7, "W": 4, "XL": 10}.get(w["size"], 5)
        return {"title": cfg.get("title") or "保存的筛选", "count": qs.count(), "items": list(qs[:limit]), "query": cfg.get("query", "")}
    return {}


def render_board(user):
    board = get_layout(user)
    todo = alert_engine.collect(user)
    widgets = []
    for w in board.layout:
        meta = CATALOG_MAP.get(w["type"])
        if not meta or not meta.allowed(user):
            continue
        cols, rows = SIZES.get(w["size"], SIZES[meta.default])
        widgets.append({**w, "meta": meta, "cols": cols, "rows": rows, "data": widget_data(user, w, todo)})
    return board, widgets, todo
