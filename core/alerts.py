"""今日待办 — management by exception.

Each rule looks for something that will cost money, cause a delay or hold up
payment, and returns Alert rows. Users can switch rules on/off, change the
threshold, the scope (my projects / everything) and the level, snooze single
alerts, and add simple custom rules.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from .models import (
    AiExtraction,
    AlertSetting,
    AlertSnooze,
    Charge,
    CustomRule,
    Leg,
    Milestone,
    Project,
    Shipment,
    Status,
    YardStay,
)
from .permissions import can_see_margin, visible_sides

LEVELS = [("urgent", "紧急"), ("today", "今天"), ("week", "本周")]
LEVEL_ORDER = {k: i for i, (k, _) in enumerate(LEVELS)}
OPEN_LOAD = ~Q(status__in=[Status.CLOSED, Status.CANCELLED])
NOT_PICKED = [Status.PLANNING, Status.AWAITING, Status.ARRIVED]
ACCESSORIAL = [t for t in Charge.Type.values if t not in ("LINEHAUL", "DRAYAGE", "FUEL", "TRANSLOAD", "STORAGE")]


@dataclass
class Alert:
    rule: str
    obj: str
    level: str
    tag: str
    title: str
    detail: str = ""
    url: str = ""
    project: object = None
    sort_date: object = None
    snoozed: bool = False

    @property
    def key(self):
        return f"{self.rule}:{self.obj}"


@dataclass
class Rule:
    key: str
    category: str
    label: str
    why: str
    level: str
    threshold: int | None = None
    unit: str = ""  # shown next to the threshold box, e.g. "天内"
    prefix: str = ""
    scope: str = "mine"
    enabled: bool = True
    side: str = ""  # AR / AP / MARGIN — who may see this rule at all
    func: object = field(default=None, repr=False)


RULES: list[Rule] = []


def rule(**kw):
    def deco(fn):
        RULES.append(Rule(func=fn, **kw))
        return fn
    return deco


class Ctx:
    def __init__(self, user, scope, threshold, level, project=None):
        self.user, self.scope, self.n, self.level, self.project = user, scope, threshold, level, project
        self.today = timezone.localdate()
        self.now = timezone.now()

    def loads(self):
        qs = Shipment.objects.filter(OPEN_LOAD).exclude(project__status__in=[Project.Status.DONE, Project.Status.CANCELLED])
        if self.project:
            return qs.filter(project=self.project)
        if self.scope == "mine":
            qs = qs.filter(project__owners=self.user)
        return qs

    def projects(self):
        qs = Project.objects.filter(status__in=[Project.Status.PENDING, Project.Status.ACTIVE, Project.Status.DELIVERED])
        if self.project:
            return qs.filter(pk=self.project.pk)
        if self.scope == "mine":
            qs = qs.filter(owners=self.user)
        return qs

    def legs(self):
        return Leg.objects.filter(shipment__in=self.loads()).select_related("shipment__project", "carrier", "origin", "destination")

    def alert(self, rule_key, obj, tag, title, detail="", url="", project=None, sort_date=None, level=None):
        return Alert(rule_key, str(obj), level or self.level, tag, title, detail, url, project, sort_date)


def _load_label(s):
    return s.container_number or s.number


def _proj(s):
    return s.project.name if s.project else "无项目"


def _days(d, today):
    return (d - today).days


def _group(ctx, rule_key, tag, items, title_fn, detail_fn=None, level=None):
    """One alert per project for rules that would otherwise list every load."""
    by_project = defaultdict(list)
    for s in items:
        by_project[s.project_id].append(s)
    out = []
    for pid, loads in by_project.items():
        p = loads[0].project
        url = p.get_absolute_url() if p else loads[0].get_absolute_url()
        if len(loads) == 1:
            url = loads[0].get_absolute_url()
        out.append(ctx.alert(rule_key, f"p{pid}", tag, title_fn(loads), detail_fn(loads) if detail_fn else _proj(loads[0]),
                             url, p, level=level))
    return out


# --------------------------------------------------------------------------
# 港口与堆场
# --------------------------------------------------------------------------
@rule(key="eta", category="港口与堆场 · 会产生滞期费", label="柜子即将到港 / 已过 ETA 未确认到港", why="提前安排 DO、车队和提柜，避免产生滞期费",
      level="today", threshold=3, unit="天前")
def r_eta(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    qs = ctx.loads().filter(status=Status.AWAITING, master_bl__eta__isnull=False, master_bl__eta__lte=horizon, master_bl__ata__isnull=True)
    for s in qs.select_related("project", "master_bl").prefetch_related("legs"):
        eta = s.master_bl.eta
        d = _days(eta, ctx.today)
        missing = []
        if not any(leg.carrier_id for leg in s.legs.all()):
            missing.append("车队")
        if not s.lfd:
            missing.append("LFD")
        if d < 0:
            title, level = f"{_load_label(s)} 已过 ETA {-d} 天（{eta:%m-%d}），还没确认到港", "urgent"
        else:
            title, level = f"{_load_label(s)} {'今天' if d == 0 else f'{d} 天后'}到港（ETA {eta:%m-%d}）", None
        detail = _proj(s) + (f" · 还没有：{'、'.join(missing)}" if missing else " · 确认 DO、提柜预约")
        yield ctx.alert("eta", s.pk, "到港", title, detail, s.get_absolute_url(), s.project, eta, level=level)


@rule(key="milestone", category="开工前检查 · 没确认就会延误", label="项目节点（里程碑）临近或已过期未完成", why="例如船到港 ETA、开工日",
      level="today", threshold=3, unit="天前")
def r_milestone(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    qs = Milestone.objects.filter(project__in=ctx.projects(), done_date__isnull=True, planned_date__isnull=False, planned_date__lte=horizon)
    for m in qs.select_related("project"):
        d = _days(m.planned_date, ctx.today)
        when = f"已过期 {-d} 天" if d < 0 else ("今天" if d == 0 else f"{d} 天后")
        yield ctx.alert("milestone", m.pk, "节点", f"{m.project.name}：{m.name} {when}（{m.planned_date:%m-%d}）",
                        "完成后在项目页填上完成日期", m.project.get_absolute_url(), m.project, m.planned_date,
                        level="urgent" if d < 0 else None)


@rule(key="lfd", category="港口与堆场 · 会产生滞期费", label="LFD 临近，柜子还没提", why="逾期按天计 Demurrage",
      level="urgent", threshold=2, unit="天内")
def r_lfd(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    for s in ctx.loads().filter(lfd__lte=horizon, status__in=NOT_PICKED).select_related("project"):
        d = _days(s.lfd, ctx.today)
        when = f"已过 LFD {-d} 天" if d < 0 else ("LFD 今天到期" if d == 0 else f"LFD {d} 天后（{s.lfd:%m-%d}）")
        yield ctx.alert("lfd", s.pk, "LFD", f"{_load_label(s)} {when}，尚未提柜", f"{_proj(s)} · 逾期按天计 Demurrage",
                        s.get_absolute_url(), s.project, s.lfd)


@rule(key="empty_return", category="港口与堆场 · 会产生滞期费", label="还空期临近，空柜 / 车架未还", why="Per Diem / 车架日租",
      level="today", threshold=2, unit="天内")
def r_empty(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    qs = ctx.loads().filter(empty_return_due__lte=horizon, empty_return_date__isnull=True).exclude(status__in=NOT_PICKED)
    for s in qs.select_related("project"):
        yield ctx.alert("empty_return", s.pk, "还空", f"{_load_label(s)} 还空期 {s.empty_return_due:%m-%d}，尚未还空",
                        f"{_proj(s)} · 逾期按天计 Per Diem", s.get_absolute_url(), s.project, s.empty_return_due)


@rule(key="yard_free", category="港口与堆场 · 会产生滞期费", label="堆场停留快超过免费天数", why="仓储费开始计费",
      level="week", threshold=3, prefix="提前", unit="天")
def r_yard(ctx):
    for y in YardStay.objects.filter(shipment__in=ctx.loads(), in_date__isnull=False, out_date__isnull=True, free_days__gt=0).select_related(
        "shipment__project", "yard"
    ):
        free_end = y.in_date + timedelta(days=y.free_days - 1)
        if free_end <= ctx.today + timedelta(days=ctx.n):
            s = y.shipment
            yield ctx.alert("yard_free", y.pk, "堆场", f"{_load_label(s)} 在 {y.yard} 免费期 {free_end:%m-%d} 结束",
                            f"{_proj(s)} · 之后开始计仓储费", s.get_absolute_url(), s.project, free_end)


# --------------------------------------------------------------------------
# 开工前检查
# --------------------------------------------------------------------------
def _start_date(s):
    leg = s.current_leg
    if leg and leg.scheduled_pickup:
        return leg.scheduled_pickup
    return s.project.start_date if s.project else None


@rule(key="no_carrier", category="开工前检查 · 没确认就会延误", label="即将开始，还没指派承运商", why="临时找车更贵或找不到",
      level="urgent", threshold=5, unit="天前")
def r_no_carrier(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    items = []
    for s in ctx.loads().filter(status__in=NOT_PICKED).select_related("project").prefetch_related("legs__carrier"):
        start = _start_date(s)
        if start and start <= horizon and not any(leg.carrier_id for leg in s.legs.all()):
            items.append(s)
    return _group(ctx, "no_carrier", "开工前", items,
                  lambda ls: f"{len(ls)} 个 Load 即将开始，还没指派承运商" if len(ls) > 1 else f"{_load_label(ls[0])} 即将开始，还没指派承运商",
                  lambda ls: f"{_proj(ls[0])} · 最早 {min(_start_date(s) for s in ls):%m-%d} 开始")


@rule(key="appt", category="开工前检查 · 没确认就会延误", label="提货 / 送货预约未确认", why="空跑 TONU、等待费",
      level="today", threshold=2, unit="天前")
def r_appt(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    items = {}
    for leg in ctx.legs():
        if leg.scheduled_pickup and leg.scheduled_pickup <= horizon and not leg.actual_pickup and not leg.pickup_appt_confirmed:
            items.setdefault(leg.shipment_id, leg.shipment)
        elif leg.scheduled_delivery and leg.scheduled_delivery <= horizon and not leg.actual_delivery and not leg.delivery_appt_confirmed:
            items.setdefault(leg.shipment_id, leg.shipment)
    return _group(ctx, "appt", "预约", list(items.values()),
                  lambda ls: f"{len(ls)} 个 Load 的预约还没确认" if len(ls) > 1 else f"{_load_label(ls[0])} 预约还没确认")


@rule(key="permits", category="开工前检查 · 没确认就会延误", label="超限 / 超重 Load 的许可证、护送未确认", why="没许可证不能上路",
      level="urgent", threshold=7, unit="天前")
def r_permits(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    items = {}
    for leg in ctx.legs().filter(permits_required=True, permits_confirmed=False, actual_pickup__isnull=True):
        if (leg.scheduled_pickup or ctx.today) <= horizon:
            items.setdefault(leg.shipment_id, leg.shipment)
    return _group(ctx, "permits", "许可证", list(items.values()),
                  lambda ls: f"{len(ls)} 个 Load 许可证 / 护送未确认" if len(ls) > 1 else f"{_load_label(ls[0])} 许可证 / 护送未确认")


@rule(key="bol", category="开工前检查 · 没确认就会延误", label="BOL 缺关键信息（UN 编号、重量、收货联系人）", why="危险品单据不全会被拒收",
      level="today", threshold=2, unit="天前")
def r_bol(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    for s in ctx.loads().filter(status__in=NOT_PICKED).select_related("project", "consignee").prefetch_related("legs", "cargo_lines"):
        start = _start_date(s)
        if not start or start > horizon:
            continue
        lines = list(s.cargo_lines.all())
        missing = []
        if not lines:
            missing.append("货物明细")
        if any(l.hazmat and not l.un_number for l in lines):
            missing.append("UN 编号")
        if lines and not any(l.weight_lbs for l in lines):
            missing.append("重量")
        dest = (s.current_leg.destination if s.current_leg and s.current_leg.destination else s.consignee)
        if dest is None:
            missing.append("收货地")
        elif not dest.contact_phone:
            missing.append("收货联系人电话")
        if missing:
            yield ctx.alert("bol", s.pk, "BOL", f"{_load_label(s)} BOL 缺：{'、'.join(missing)}", f"{_proj(s)} · {start:%m-%d} 提货",
                            s.get_absolute_url(), s.project, start)


# --------------------------------------------------------------------------
# 在途
# --------------------------------------------------------------------------
@rule(key="ai_pending", category="在途 · 等待与异常", label="AI 从邮件里发现的更新，等你确认", why="确认后才会写入 TMS",
      level="today")
def r_ai_pending(ctx):
    qs = AiExtraction.objects.filter(status=AiExtraction.State.PENDING)
    qs = qs.filter(Q(project__in=ctx.projects()) | Q(created_by=ctx.user, project__isnull=True))
    for e in qs.select_related("project").prefetch_related("changes"):
        n = sum(1 for c in e.changes.all() if c.status == "PENDING")
        what = f"{n} 项更新" if n else "新项目信息"
        src = "邮箱扫描" if e.source == AiExtraction.Source.GMAIL else "粘贴识别"
        title = f"{e.project.name if e.project else 'AI 识别'}：AI 发现 {what}待确认"
        yield ctx.alert("ai_pending", e.pk, "AI", title, f"{src} #{e.pk} · {e.summary[:80]}", e.get_absolute_url(),
                        e.project, e.created_at.date())


@rule(key="lead_due", category="在途 · 等待与异常", label="询价快到报价截止日 / 承运商超过 2 天没回价", why="报价晚了就丢单",
      level="today")
def r_lead_due(ctx):
    from .models import CarrierQuote, Lead

    leads = Lead.objects.filter(owner=ctx.user, status__in=Lead.OPEN)
    for lead in leads.filter(quote_due__lte=ctx.today + timedelta(days=1)).exclude(status=Lead.Status.QUOTED):
        word = "已过" if lead.quote_due < ctx.today else ("今天" if lead.quote_due == ctx.today else "明天")
        yield ctx.alert("lead_due", lead.pk, "询价", f"{lead.number} {lead.title}：报价截止{word}（{lead.quote_due:%m/%d}）",
                        lead.customer_label, lead.get_absolute_url(), None, lead.quote_due)
    stale = CarrierQuote.objects.filter(lead__in=leads, status=CarrierQuote.Status.REQUESTED,
                                        requested_at__date__lte=ctx.today - timedelta(days=2)).select_related("lead", "carrier")
    by_lead = defaultdict(list)
    for q in stale:
        by_lead[q.lead].append(q.carrier_label)
    for lead, names in by_lead.items():
        yield ctx.alert("lead_due", f"cq{lead.pk}", "询价", f"{lead.number}：{len(names)} 家承运商超过 2 天没回价",
                        "、".join(names[:5]), lead.get_absolute_url(), None, ctx.today)


@rule(key="appts_today", category="在途 · 等待与异常", label="今天 / 明天的提货和送货", why="每日确认", level="today")
def r_today(ctx):
    days = [ctx.today, ctx.today + timedelta(days=1)]
    pu, dl = defaultdict(list), defaultdict(list)
    for leg in ctx.legs():
        if leg.scheduled_pickup in days and not leg.actual_pickup:
            pu[(leg.shipment.project_id, leg.scheduled_pickup)].append(leg)
        if leg.scheduled_delivery in days and not leg.actual_delivery:
            dl[(leg.shipment.project_id, leg.scheduled_delivery)].append(leg)
    for kind, groups in (("提货", pu), ("送货", dl)):
        for (pid, d), legs in groups.items():
            word = "今天" if d == ctx.today else "明天"
            s = legs[0].shipment
            names = "、".join(_load_label(l.shipment) for l in legs[:4]) + ("…" if len(legs) > 4 else "")
            yield ctx.alert("appts_today", f"{kind}{pid}{d}", "预约", f"{word}{kind} {len(legs)} 个：{names}", _proj(s),
                            s.project.get_absolute_url() if s.project and len(legs) > 1 else s.get_absolute_url(), s.project, d)


@rule(key="detention", category="在途 · 等待与异常", label="司机到场超过免费等待时间（Detention 计时）", why="可向客户收，也要防承运商多报",
      level="urgent", threshold=2, prefix="免费", unit="小时")
def r_detention(ctx):
    for leg in ctx.legs().filter(Q(delivery_arrived_at__isnull=False, delivery_departed_at__isnull=True) | Q(pickup_arrived_at__isnull=False, pickup_departed_at__isnull=True)):
        s = leg.shipment
        free = s.project.detention_free_hours if s.project else ctx.n
        for where, w in (("收货点", leg.delivery_wait(free)), ("提货点", leg.pickup_wait(free))):
            if w and w["running"] and w["billable"] > 0:
                money = ""
                if s.project and s.project.detention_rate:
                    money = f" · 按 ${s.project.detention_rate}/hr 可收约 ${(w['billable'] * s.project.detention_rate).quantize(Decimal('1'))}"
                yield ctx.alert("detention", f"{leg.pk}{where}", "Detention", f"{_load_label(s)} 在{where}已等 {w['hours']} 小时（免费 {free:g}）",
                                f"{_proj(s)}{money} · 让对方在 BOL 上签到场、离场时间", s.get_absolute_url(), s.project)


@rule(key="tracking", category="在途 · 等待与异常", label="在途超过 N 小时没有跟踪更新", why="晚到才发现就来不及通知客户",
      level="week", threshold=12, unit="小时", enabled=False)
def r_tracking(ctx):
    cutoff = ctx.now - timedelta(hours=ctx.n)
    items = {}
    for leg in ctx.legs().filter(actual_pickup__isnull=False, actual_delivery__isnull=True).filter(Q(last_tracking_at__isnull=True) | Q(last_tracking_at__lt=cutoff)):
        items.setdefault(leg.shipment_id, leg.shipment)
    return _group(ctx, "tracking", "跟踪", list(items.values()),
                  lambda ls: f"{len(ls)} 个在途 Load 超过 {ctx.n} 小时没有跟踪更新" if len(ls) > 1 else f"{_load_label(ls[0])} 超过 {ctx.n} 小时没有跟踪更新")


@rule(key="late", category="在途 · 等待与异常", label="已过计划日期仍未提货 / 送达", why="Layover、客户索赔", level="today")
def r_late(ctx):
    items = {}
    for leg in ctx.legs():
        if leg.scheduled_delivery and leg.scheduled_delivery < ctx.today and not leg.actual_delivery:
            items.setdefault(leg.shipment_id, (leg.shipment, "送达"))
        elif leg.scheduled_pickup and leg.scheduled_pickup < ctx.today and not leg.actual_pickup:
            items.setdefault(leg.shipment_id, (leg.shipment, "提货"))
    return _group(ctx, "late", "晚于计划", [s for s, _ in items.values()],
                  lambda ls: f"{len(ls)} 个 Load 已过计划日期仍未完成" if len(ls) > 1 else f"{_load_label(ls[0])} 已过计划{items[ls[0].pk][1]}日期")


# --------------------------------------------------------------------------
# 回款与利润
# --------------------------------------------------------------------------
def _delivered_before(ctx, days):
    cutoff = ctx.today - timedelta(days=days)
    out = []
    for s in ctx.loads().filter(status=Status.DELIVERED).select_related("project").prefetch_related("legs", "charges"):
        d = s.delivered_on
        if d and d <= cutoff:
            out.append(s)
    return out


@rule(key="pod", category="回款与利润", label="已送达，POD 还没收到", why="没有 POD 开不了票", level="urgent", threshold=2, unit="天后")
def r_pod(ctx):
    items = [s for s in _delivered_before(ctx, ctx.n) if not s.pod_received and not s.pod_done]
    return _group(ctx, "pod", "POD", items,
                  lambda ls: f"{len(ls)} 个 Load 已送达，POD 还没收到" if len(ls) > 1 else f"{_load_label(ls[0])} 已送达 {(ctx.today - ls[0].delivered_on).days} 天，POD 还没收到",
                  lambda ls: f"{_proj(ls[0])} · 影响开票")


@rule(key="pod_upload", category="回款与利润", label="POD 已收到，还没核对上传", why="POD 没上传，Load 不能结案、项目不能完结、财务开不了票",
      level="urgent", threshold=1, unit="天后")
def r_pod_upload(ctx):
    cutoff = ctx.today - timedelta(days=ctx.n)
    items = [s for s in ctx.loads().filter(status__in=[Status.DELIVERED, Status.CLOSED], pod_received__isnull=False,
                                           pod_received__lte=cutoff, pod_uploaded_at__isnull=True, pod_waived_reason="")
             .select_related("project").prefetch_related("legs", "charges")]
    return _group(ctx, "pod_upload", "POD", items,
                  lambda ls: f"{len(ls)} 个 Load 的 POD 已收到，还没核对上传" if len(ls) > 1 else f"{_load_label(ls[0])} 的 POD 已收到，还没核对上传",
                  lambda ls: f"{_proj(ls[0])} · 上传后点“POD 已上传”")


@rule(key="ratecon_missing", category="开工前检查 · 没确认就会延误", label="已派承运商、快提货了，还没出运价确认单", why="没有签回的运价确认单，运价和条款容易起纠纷",
      level="today", threshold=2, unit="天内提货", side="AP")
def r_ratecon_missing(ctx):
    horizon = ctx.today + timedelta(days=ctx.n)
    legs = (Leg.objects.filter(shipment__in=ctx.loads(), carrier__isnull=False, actual_pickup__isnull=True,
                               scheduled_pickup__isnull=False, scheduled_pickup__lte=horizon, ratecon_version=0)
            .select_related("shipment__project", "carrier"))
    items = []
    for leg in legs:
        s = leg.shipment
        s._rc_leg = leg
        items.append(s)
    return _group(ctx, "ratecon_missing", "运价", items,
                  lambda ls: f"{len(ls)} 个 Load 已派车还没出运价确认单" if len(ls) > 1 else
                  f"{_load_label(ls[0])} 第 {ls[0]._rc_leg.sequence} 段（{ls[0]._rc_leg.carrier}）{ls[0]._rc_leg.scheduled_pickup:%m-%d} 提货，还没出运价确认单",
                  lambda ls: f"{_proj(ls[0])} · 在 Load 页运输段里点“运价确认”")


@rule(key="extra_charges", category="回款与利润", label="承运商额外费用待审批 / 有争议", why="防重复付款", level="urgent", side="AP")
def r_extra(ctx):
    qs = Charge.objects.filter(shipment__in=ctx.loads(), side="AP", charge_type__in=ACCESSORIAL, state__in=[Charge.State.OPEN, Charge.State.DISPUTED])
    items = {}
    for c in qs.select_related("shipment__project"):
        items.setdefault(c.shipment_id, c.shipment)
    return _group(ctx, "extra_charges", "额外费用", list(items.values()),
                  lambda ls: f"{len(ls)} 个 Load 有待审的承运商额外费用" if len(ls) > 1 else f"{_load_label(ls[0])} 有待审的承运商额外费用")


@rule(key="low_margin", category="回款与利润", label="Load 毛利低于阈值", why="及时补收附加费", level="week", threshold=8, prefix="<", unit="%", side="MARGIN")
def r_margin(ctx):
    zero = Value(Decimal("0"), output_field=DecimalField(max_digits=12, decimal_places=2))
    live = Q(charges__is_deleted=False) & ~Q(charges__state=Charge.State.VOID)
    qs = ctx.loads().annotate(
        ar=Coalesce(Sum("charges__amount", filter=live & Q(charges__side="AR")), zero),
        ap=Coalesce(Sum("charges__amount", filter=live & Q(charges__side="AP")), zero),
    ).filter(ar__gt=0, ap__gt=0).select_related("project")
    for s in qs:
        pct = (s.ar - s.ap) / s.ar * 100
        if pct < ctx.n:
            yield ctx.alert("low_margin", s.pk, "毛利", f"{_load_label(s)} 毛利 {pct:.1f}%（应收 ${s.ar:,.0f} / 应付 ${s.ap:,.0f}）",
                            _proj(s), s.get_absolute_url(), s.project)


@rule(key="ar_uninvoiced", category="回款与利润", label="已送达 N 天还没向客户开票", why="账期顺延", level="week", threshold=7, unit="天", side="AR", enabled=False)
def r_ar(ctx):
    items = []
    for s in _delivered_before(ctx, ctx.n):
        ar = [c for c in s.charges.all() if c.side == "AR" and c.state != Charge.State.VOID]
        if not ar or any(c.state == Charge.State.OPEN for c in ar):
            items.append(s)
    return _group(ctx, "ar_uninvoiced", "应收", items,
                  lambda ls: f"{len(ls)} 个 Load 已送达还没开票" if len(ls) > 1 else f"{_load_label(ls[0])} 已送达还没开票")


@rule(key="ap_invoice", category="回款与利润", label="已送达 N 天，承运商发票还没到", why="月底对账缺票", level="week", threshold=7, unit="天", side="AP", enabled=False)
def r_ap(ctx):
    items = []
    for s in _delivered_before(ctx, ctx.n):
        if any(c.side == "AP" and c.state == Charge.State.OPEN and not c.invoice_number for c in s.charges.all()):
            items.append(s)
    return _group(ctx, "ap_invoice", "应付", items,
                  lambda ls: f"{len(ls)} 个 Load 的承运商发票还没到" if len(ls) > 1 else f"{_load_label(ls[0])} 承运商发票还没到")


@rule(key="no_owner", category="回款与利润", label="项目没有负责人", why="没人跟进", level="today", scope="all")
def r_no_owner(ctx):
    for p in ctx.projects().filter(owners__isnull=True).distinct():
        yield ctx.alert("no_owner", p.pk, "负责人", "项目还没有指定负责人", f"{p.name} · 无人负责的项目不会出现在任何人的工作台",
                        p.get_absolute_url(), p)


RULE_MAP = {r.key: r for r in RULES}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
def rule_allowed(r, user):
    if r.side == "MARGIN":
        return can_see_margin(user)
    if r.side:
        return r.side in visible_sides(user)
    return True


def effective_settings(user):
    saved = {s.rule: s for s in AlertSetting.objects.filter(user=user)}
    out = []
    for r in RULES:
        if not rule_allowed(r, user):
            continue
        s = saved.get(r.key)
        out.append({
            "rule": r,
            "enabled": s.enabled if s else r.enabled,
            "threshold": s.threshold if s and s.threshold is not None else r.threshold,
            "scope": s.scope if s else r.scope,
            "level": (s.level if s and s.level else r.level),
        })
    return out


def _custom_alerts(user, ctx_project=None):
    out = []
    today = timezone.localdate()
    for cr in CustomRule.objects.filter(user=user, enabled=True):
        qs = Shipment.objects.filter(OPEN_LOAD)
        if ctx_project:
            qs = qs.filter(project=ctx_project)
        for f, v in ((cr.field1, cr.value1), (cr.field2, cr.value2)):
            if not f:
                continue
            if f == "customer":
                qs = qs.filter(customer__name__icontains=v)
            elif f == "carrier":
                qs = qs.filter(legs__carrier__name__icontains=v)
            elif f == "project":
                qs = qs.filter(project__name__icontains=v)
            elif f == "status":
                codes = [k for k, label in Status.choices if v in (k, label)]
                qs = qs.filter(status__in=codes)
            elif f in ("pickup_within", "delivery_within"):
                try:
                    n = int(v)
                except ValueError:
                    n = 0
                col = "legs__scheduled_pickup" if f == "pickup_within" else "legs__scheduled_delivery"
                qs = qs.filter(**{f"{col}__gte": today, f"{col}__lte": today + timedelta(days=n)})
        loads = list(qs.distinct().select_related("project")[:50])
        if loads:
            s = loads[0]
            names = "、".join(_load_label(x) for x in loads[:5]) + ("…" if len(loads) > 5 else "")
            same_project = len({x.project_id for x in loads}) == 1 and s.project
            url = s.get_absolute_url() if len(loads) == 1 else (s.project.get_absolute_url() if same_project else reverse("shipment_list"))
            out.append(Alert("custom", str(cr.pk), cr.level, "自定义", f"{cr.name}（{len(loads)}）", names, url, s.project))
    return out


def collect(user, project=None, include_snoozed=False):
    """All alerts for this user (or for one project page)."""
    alerts = []
    for st in effective_settings(user):
        if not st["enabled"]:
            continue
        r = st["rule"]
        ctx = Ctx(user, "all" if project else st["scope"], st["threshold"], st["level"], project)
        alerts.extend(r.func(ctx) or [])
    alerts.extend(_custom_alerts(user, project))
    snoozed = set(AlertSnooze.objects.filter(user=user, until__gt=timezone.now()).values_list("key", flat=True))
    for a in alerts:
        a.snoozed = a.key in snoozed
    if not include_snoozed:
        alerts = [a for a in alerts if not a.snoozed]
    far = timezone.localdate() + timedelta(days=3650)
    alerts.sort(key=lambda a: (LEVEL_ORDER.get(a.level, 9), a.sort_date or far))
    return alerts


def counts(alerts):
    c = {k: 0 for k, _ in LEVELS}
    for a in alerts:
        c[a.level] = c.get(a.level, 0) + 1
    return c
