"""AI email reading: paste an email, Gemini proposes field updates, a person confirms.

Flow:
  1. extract(): send the email plus the candidate Loads to Gemini (Vertex AI),
     get structured JSON back, match each item to a Load, and store one
     AiChange per field that would actually change (status PENDING).
  2. apply_changes(): a person ticks what is right; each change is re-checked
     (permission, lock, value unchanged since extraction) and written through
     the normal model save, so history, status and locking work as usual.

Only the fields in FIELDS can ever be written. Money (charges) is never touched.
"""
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from .models import AiChange, AiExtraction, Leg, Note, Shipment, Status
from .permissions import can_edit_ops, can_edit_shipment_ops

log = logging.getLogger(__name__)


class AiError(Exception):
    """Shown to the user as-is."""


@dataclass(frozen=True)
class FieldSpec:
    target: str  # "leg" or "shipment"
    kind: str  # date / time / datetime / bool / text / url
    label: str
    lock_exempt: bool = False  # editable after delivery (documents, POD)


# The only things AI may change.
FIELDS = {
    "scheduled_pickup": FieldSpec("leg", "date", "计划提货"),
    "pickup_appt": FieldSpec("leg", "time", "提货预约时间"),
    "pickup_appt_confirmed": FieldSpec("leg", "bool", "提货预约已确认"),
    "actual_pickup": FieldSpec("leg", "date", "实际提货"),
    "pickup_arrived_at": FieldSpec("leg", "datetime", "到达提货点"),
    "pickup_departed_at": FieldSpec("leg", "datetime", "离开提货点"),
    "scheduled_delivery": FieldSpec("leg", "date", "计划送达"),
    "delivery_appt": FieldSpec("leg", "time", "送达预约时间"),
    "delivery_appt_confirmed": FieldSpec("leg", "bool", "送货预约已确认"),
    "delivery_arrived_at": FieldSpec("leg", "datetime", "到达收货点"),
    "delivery_departed_at": FieldSpec("leg", "datetime", "离开收货点"),
    "actual_delivery": FieldSpec("leg", "date", "实际送达"),
    "driver_name": FieldSpec("leg", "text", "司机"),
    "driver_phone": FieldSpec("leg", "text", "司机电话"),
    "truck_trailer": FieldSpec("leg", "text", "车牌 / 拖车号"),
    "carrier_load_number": FieldSpec("leg", "text", "承运商 Load# / PRO#"),
    "tracking_url": FieldSpec("leg", "url", "跟踪链接"),
    "lfd": FieldSpec("shipment", "date", "LFD"),
    "pod_received": FieldSpec("shipment", "date", "POD 收到日期", lock_exempt=True),
    "pod_link": FieldSpec("shipment", "url", "POD 链接", lock_exempt=True),
    "empty_return_due": FieldSpec("shipment", "date", "还空期限", lock_exempt=True),
    "empty_return_date": FieldSpec("shipment", "date", "还空日期", lock_exempt=True),
}
NOTE_FIELD = "note"  # a timeline note, not a field

MAX_EMAIL_CHARS = 30000
MAX_CANDIDATES = 300


def enabled():
    return settings.TMS_AI_ENABLED


# ---------------------------------------------------------------------------
# Values: parse what the model returns, serialize for storage and comparison
# ---------------------------------------------------------------------------
def _tz():
    return ZoneInfo(settings.TIME_ZONE)


def parse_value(kind, raw):
    """Model output (string) -> Python value. Raises ValueError if unusable."""
    if raw is None:
        raise ValueError("空值")
    s = str(raw).strip()
    if kind == "text":
        if not s:
            raise ValueError("空值")
        return s[:200]
    if kind == "url":
        if not re.match(r"^https?://\S+$", s):
            raise ValueError("不是网址")
        return s[:500]
    if kind == "bool":
        if s.lower() in ("true", "yes", "1", "是"):
            return True
        if s.lower() in ("false", "no", "0", "否"):
            return False
        raise ValueError("不是是/否")
    if kind == "date":
        d = parse_date(s[:10])
        if d is None:
            raise ValueError("日期格式不对")
        return d
    if kind == "time":
        t = parse_time(s[:8])
        if t is None:
            raise ValueError("时间格式不对")
        return t.replace(second=0, microsecond=0)
    if kind == "datetime":
        dt = parse_datetime(s.replace(" ", "T", 1) if "T" not in s else s)
        if dt is None:
            raise ValueError("时间格式不对")
        if timezone.is_naive(dt):
            dt = dt.replace(tzinfo=_tz())
        return dt.replace(second=0, microsecond=0)
    raise ValueError(kind)


def serialize(kind, value):
    """Python value -> canonical string ('' for empty) used for storage and stale checks."""
    if value in (None, ""):
        return ""
    if kind == "datetime":
        return value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%MZ")
    if kind == "time":
        return value.strftime("%H:%M")
    if kind == "date":
        return value.isoformat()
    if kind == "bool":
        return "true" if value else ""  # False is the "empty" default
    return str(value)


def deserialize(kind, s):
    if s == "":
        return False if kind == "bool" else (None if kind in ("date", "time", "datetime") else "")
    if kind == "datetime":
        return datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=ZoneInfo("UTC"))
    if kind == "time":
        return time.fromisoformat(s)
    if kind == "date":
        return date.fromisoformat(s)
    if kind == "bool":
        return s == "true"
    return s


def display(kind, s):
    """Canonical string -> what people read."""
    if s == "":
        return "否" if kind == "bool" else "—"
    v = deserialize(kind, s)
    if kind == "datetime":
        local = v.astimezone(_tz())
        return local.strftime("%Y-%m-%d %H:%M ") + local.tzname()
    if kind == "bool":
        return "是" if v else "否"
    return s


# ---------------------------------------------------------------------------
# Candidates and matching
# ---------------------------------------------------------------------------
def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def candidates(project=None, shipment=None):
    if shipment is not None:
        return [shipment]
    qs = Shipment.objects.select_related("project").prefetch_related("legs__carrier")
    if project is not None:
        qs = qs.filter(project=project)
    else:
        qs = qs.exclude(status__in=[Status.CLOSED, Status.CANCELLED])
    return list(qs.order_by("-created_at")[:MAX_CANDIDATES])


def _leg_for(shipment, carrier_load_number=""):
    legs = list(shipment.legs.all())
    if not legs:
        return None
    key = _norm(carrier_load_number)
    if key:
        for leg in legs:
            if _norm(leg.carrier_load_number) == key:
                return leg
    return shipment.current_leg


def match(item, cands, single=None):
    """Find the Load an extracted item is about."""
    if single is not None:
        return single
    cntr, bln = _norm(item.get("container_number")), _norm(item.get("carrier_load_number"))
    if cntr:
        for s in cands:
            if _norm(s.container_number) == cntr:
                return s
    if bln:
        for s in cands:
            if any(_norm(leg.carrier_load_number) == bln for leg in s.legs.all()):
                return s
    return None


def _current(obj, key):
    spec = FIELDS[key]
    return serialize(spec.kind, getattr(obj, key) if obj is not None else None)


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING", "description": "一两句中文概括这封邮件说了什么"},
        "loads": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "container_number": {"type": "STRING"},
                    "carrier_load_number": {"type": "STRING"},
                    "updates": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "field": {"type": "STRING", "format": "enum", "enum": list(FIELDS)},
                                "value": {"type": "STRING"},
                                "evidence": {"type": "STRING", "description": "邮件原文中支持这个值的那句话"},
                            },
                            "required": ["field", "value", "evidence"],
                        },
                    },
                    "note": {"type": "STRING", "description": "值得记到时间线的其他事实，中文，没有就留空"},
                },
                "required": ["container_number", "carrier_load_number", "updates", "note"],
            },
        },
        "unmatched": {"type": "STRING", "description": "邮件中提到但无法对应到任何 Load 的信息"},
    },
    "required": ["summary", "loads", "unmatched"],
}

SYSTEM_PROMPT = """你是一家美国物流公司 TMS 系统的数据录入助手。用户会粘贴一封与运输相关的邮件（承运商、司机、客户或堆场发来的），以及系统里可能相关的 Load 列表。
你的任务：只提取邮件中**明确写出**的、属于下面字段的事实，输出 JSON。

规则：
1. 只用邮件里明确写出的信息。不要推测、不要补全。拿不准就不要输出该字段。
2. 每个 Load 用柜号（container_number）或承运商单号（carrier_load_number，例如 BLN30784、PRO#）识别，按邮件原文填写；不知道就留空字符串。
3. 日期用 YYYY-MM-DD。邮件只写月/日时，按“今天”推断年份（取离今天最近的那个日期）。
4. 时刻（*_appt）用 HH:MM（24 小时制）。
5. 时间点（*_at 字段）用 ISO 8601，例如 2026-10-13T09:20:00-04:00。邮件写明时区（ET/EST/EDT/CT/PT 等）或地点能明确时区时，带上对应的偏移量；无法确定时区时不要加偏移量。
6. 是/否字段写 true 或 false。预约“已确认”只在邮件明确确认了预约时才写 true。
7. 字段含义：
   - actual_pickup / actual_delivery：实际提货 / 送达的日期（已经发生）。
   - scheduled_*：计划日期；*_appt：预约时刻。
   - pickup_arrived_at / pickup_departed_at：司机到达 / 离开提货点的时间；delivery_arrived_at / delivery_departed_at：到达 / 离开收货点的时间。
   - pod_received：收到签收单（POD）的日期；pod_link：POD 文件链接。
   - lfd：码头免费期最后一天；empty_return_due：还空期限；empty_return_date：实际还空日期。
8. evidence 填邮件里支持该值的原句（原文语言，尽量短）。
9. 其他值得记录的事实（延误原因、故障、改约、现场情况、签收人等）用中文简短写进该 Load 的 note。
10. 绝不提取任何金额、费率、费用。
11. 如果邮件和列表中的任何 Load 都无关，loads 返回空数组，在 unmatched 里说明。"""


def _context_block(cands, single):
    rows = []
    for s in cands:
        leg = s.current_leg
        rows.append({
            "container_number": s.container_number,
            "carrier_load_numbers": [leg.carrier_load_number for leg in s.legs.all() if leg.carrier_load_number],
            "project": s.project.name if s.project_id else "",
            "status": s.get_status_display(),
            "current": {k: display(FIELDS[k].kind, _current(leg if FIELDS[k].target == "leg" else s, k))
                        for k in FIELDS if (leg if FIELDS[k].target == "leg" else s) is not None},
        })
    head = "这封邮件就是关于下面这一个 Load 的（只需输出一个 Load）。" if single else "系统里可能相关的 Load："
    return head + "\n" + json.dumps(rows, ensure_ascii=False)


def call_gemini(email_text, context, today):
    """Return the parsed JSON dict from Gemini on Vertex AI."""
    import google.auth
    import google.auth.transport.requests
    import requests

    try:
        creds, default_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as e:  # noqa: BLE001
        raise AiError(f"无法获得 Google Cloud 凭据：{e}") from e
    project = settings.TMS_AI_PROJECT or default_project
    loc = settings.TMS_AI_LOCATION
    host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    url = (f"https://{host}/v1/projects/{project}/locations/{loc}/publishers/google/models/"
           f"{settings.TMS_AI_MODEL}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": f"今天是 {today.isoformat()}。\n\n{context}\n\n=== 邮件开始 ===\n{email_text}\n=== 邮件结束 ==="}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA},
    }
    try:
        r = requests.post(url, json=body, headers={"Authorization": f"Bearer {creds.token}"}, timeout=50)
    except requests.RequestException as e:
        raise AiError(f"连接 Gemini 失败：{e}") from e
    if r.status_code != 200:
        log.warning("Gemini HTTP %s: %s", r.status_code, r.text[:500])
        raise AiError(f"Gemini 返回错误 {r.status_code}：{r.text[:300]}")
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return json.loads(text), data.get("usageMetadata", {})
    except (KeyError, IndexError, ValueError) as e:
        raise AiError("Gemini 返回的内容无法识别，请重试") from e


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def extract(email_text, user, project=None, shipment=None, caller=None):
    """Run Gemini on an email and store the proposed changes. Returns AiExtraction."""
    if not can_edit_ops(user):
        raise AiError("只有操作和管理层可以使用 AI 识别")
    email_text = (email_text or "").strip()
    if not email_text:
        raise AiError("请先粘贴邮件内容")
    if len(email_text) > MAX_EMAIL_CHARS:
        raise AiError(f"内容太长（{len(email_text)} 字），请只粘贴相关的邮件正文，{MAX_EMAIL_CHARS} 字以内")
    since = timezone.now() - timedelta(days=1)
    if AiExtraction.objects.filter(created_by=user, created_at__gte=since).count() >= settings.TMS_AI_DAILY_LIMIT:
        raise AiError(f"今天已识别 {settings.TMS_AI_DAILY_LIMIT} 次，达到每人每日上限")

    cands = candidates(project, shipment)
    if not cands:
        raise AiError("范围内没有 Load 可以对应")
    context = _context_block(cands, shipment is not None)
    result, usage = (caller or call_gemini)(email_text, context, timezone.localdate())

    ext = AiExtraction.objects.create(
        created_by=user, project=project, shipment=shipment, source_text=email_text,
        model_name=settings.TMS_AI_MODEL, summary=str(result.get("summary", ""))[:1000],
        raw_result=result, usage=usage or {},
    )
    unmatched = [str(result.get("unmatched") or "").strip()]
    for item in result.get("loads") or []:
        s = match(item, cands, shipment)
        label = item.get("container_number") or item.get("carrier_load_number") or "?"
        if s is None:
            unmatched.append(f"找不到 {label}")
            continue
        leg = _leg_for(s, item.get("carrier_load_number"))
        for u in item.get("updates") or []:
            key = u.get("field")
            if key not in FIELDS:
                continue
            spec = FIELDS[key]
            obj = leg if spec.target == "leg" else s
            if obj is None:
                unmatched.append(f"{label} 还没有运输段，无法写入「{spec.label}」")
                continue
            try:
                new = serialize(spec.kind, parse_value(spec.kind, u.get("value")))
            except ValueError as e:
                unmatched.append(f"{label}「{spec.label}」无法识别：{u.get('value')}（{e}）")
                continue
            old = _current(obj, key)
            if new == old:
                continue  # already correct
            naive = spec.kind == "datetime" and not re.search(r"(Z|[+-]\d\d:?\d\d)$", str(u.get("value")).strip())
            AiChange.objects.create(
                extraction=ext, shipment=s, leg=leg if spec.target == "leg" else None, field=key,
                old_value=old, new_value=new, evidence=str(u.get("evidence", ""))[:500], tz_assumed=naive,
            )
        note = str(item.get("note") or "").strip()
        if note:
            AiChange.objects.create(extraction=ext, shipment=s, field=NOTE_FIELD, new_value=note[:1000])
    ext.unmatched = "\n".join(x for x in unmatched if x)[:2000]
    ext.save(update_fields=["unmatched"])
    return ext


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def blocked_reason(change, user):
    """Why this change can't be applied by this user right now ('' if it can)."""
    if change.field == NOTE_FIELD:
        return "" if can_edit_ops(user) else "没有编辑权限"
    spec = FIELDS[change.field]
    s = change.shipment
    if not can_edit_ops(user):
        return "没有编辑权限"
    if not spec.lock_exempt and not can_edit_shipment_ops(user, s):
        return "Load 已送达锁定，需管理层解锁"
    if spec.target == "leg" and (change.leg is None or change.leg.is_deleted):
        return "运输段已不存在"
    return ""


@transaction.atomic
def apply_changes(ext, user, approve_ids):
    """Apply the ticked changes, reject the rest. Returns (applied, skipped) lists."""
    approve_ids = {int(i) for i in approve_ids}
    applied, skipped = [], []
    pending = list(ext.changes.select_related("shipment", "leg").filter(status=AiChange.State.PENDING))
    by_obj = {}
    now = timezone.now()
    for c in pending:
        if c.id not in approve_ids:
            c.status = AiChange.State.REJECTED
        elif reason := blocked_reason(c, user):
            c.status, c.result = AiChange.State.BLOCKED, reason
            skipped.append(c)
        elif c.field == NOTE_FIELD:
            Note.objects.create(shipment=c.shipment, created_by=user, kind=Note.Kind.NOTE, text=f"[AI 邮件识别] {c.new_value}")
            c.status = AiChange.State.APPLIED
            applied.append(c)
        else:
            spec = FIELDS[c.field]
            obj = c.leg if spec.target == "leg" else c.shipment
            key = (spec.target, obj.pk)
            if key not in by_obj:
                by_obj[key] = (Leg if spec.target == "leg" else Shipment).all_objects.select_for_update().get(pk=obj.pk)
            obj = by_obj[key]
            if _current(obj, c.field) != c.old_value:
                c.status, c.result = AiChange.State.STALE, f"识别后已被改为 {display(spec.kind, _current(obj, c.field))}，未覆盖"
                skipped.append(c)
            else:
                setattr(obj, c.field, deserialize(spec.kind, c.new_value))
                c.status = AiChange.State.APPLIED
                applied.append(c)
        c.decided_by, c.decided_at = user, now
        c.save()

    reason = f"AI 邮件识别 #{ext.pk}"
    for obj in by_obj.values():
        obj._change_reason = reason
        obj.save()
    # Shipment.save() / the Leg signal recompute status and locking.
    for s in {c.shipment for c in applied}:
        items = [c for c in applied if c.shipment_id == s.pk and c.field != NOTE_FIELD]
        if items:
            text = "；".join(f"{FIELDS[c.field].label} {c.new_display}" for c in items)
            Note.objects.create(shipment=s, created_by=user, kind=Note.Kind.SYSTEM, text=f"通过 AI 邮件识别 #{ext.pk} 更新：{text}")
    ext.status = AiExtraction.State.DONE
    ext.reviewed_by, ext.reviewed_at = user, now
    ext.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return applied, skipped


def discard(ext, user):
    ext.changes.filter(status=AiChange.State.PENDING).update(status=AiChange.State.REJECTED, decided_by=user, decided_at=timezone.now())
    ext.status = AiExtraction.State.DISCARDED
    ext.reviewed_by, ext.reviewed_at = user, timezone.now()
    ext.save(update_fields=["status", "reviewed_by", "reviewed_at"])
