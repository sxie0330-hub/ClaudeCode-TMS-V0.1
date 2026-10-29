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
import base64
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

from . import units
from .models import (AiChange, AiExtraction, CargoLine, Carrier, Company, Customer, Leg, Location, MasterBL, Milestone, Note,
                     Project, Shipment, Status)
from .permissions import can_edit_ops, can_edit_shipment_ops

log = logging.getLogger(__name__)


class AiError(Exception):
    """Shown to the user as-is."""


@dataclass(frozen=True)
class FieldSpec:
    # Load level: "leg", "shipment", "mbl" (the Load's master bill of lading).
    # Project level: "project", "consignee" / "shipper" (the project's default addresses).
    target: str
    kind: str  # date / time / datetime / bool / text / url / container / equipment / mbl / carrier / cargo
    label: str
    lock_exempt: bool = False  # editable after delivery (documents, POD)
    attr: str = ""  # model attribute when it differs from the key


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
    "eta": FieldSpec("mbl", "date", "预计到港 ETA"),
    "ata": FieldSpec("mbl", "date", "实际到港 ATA"),
    "lfd": FieldSpec("shipment", "date", "LFD"),
    "pod_received": FieldSpec("shipment", "date", "POD 收到日期", lock_exempt=True),
    "pod_link": FieldSpec("shipment", "url", "POD 链接", lock_exempt=True),
    "empty_return_due": FieldSpec("shipment", "date", "还空期限", lock_exempt=True),
    "empty_return_date": FieldSpec("shipment", "date", "还空日期", lock_exempt=True),
    # Filling in what was unknown when the project was created
    "container_number": FieldSpec("shipment", "container", "柜号"),
    "equipment": FieldSpec("shipment", "equipment", "柜型"),
    "mbl_number": FieldSpec("shipment", "mbl", "MBL"),
    "carrier": FieldSpec("leg", "carrier", "承运商 / 车队"),
    "cargo_line": FieldSpec("shipment", "cargo", "货物明细"),  # adds a CargoLine; value is JSON
    # Project level
    "start_date": FieldSpec("project", "date", "项目开始日期"),
    "end_date": FieldSpec("project", "date", "项目结束日期"),
    "cargo_description": FieldSpec("project", "text", "货物描述"),
    "consignee_name": FieldSpec("consignee", "text", "送货地 名称", attr="name"),
    "consignee_address": FieldSpec("consignee", "text", "送货地 地址", attr="address"),
    "consignee_city": FieldSpec("consignee", "text", "送货地 城市", attr="city"),
    "consignee_state": FieldSpec("consignee", "text", "送货地 州", attr="state"),
    "consignee_postal_code": FieldSpec("consignee", "text", "送货地 邮编", attr="postal_code"),
    "consignee_contact": FieldSpec("consignee", "text", "送货地 联系人", attr="contact_name"),
    "consignee_phone": FieldSpec("consignee", "text", "送货地 电话", attr="contact_phone"),
    "shipper_name": FieldSpec("shipper", "text", "提货地 名称", attr="name"),
    "shipper_address": FieldSpec("shipper", "text", "提货地 地址", attr="address"),
    "shipper_city": FieldSpec("shipper", "text", "提货地 城市", attr="city"),
    "shipper_state": FieldSpec("shipper", "text", "提货地 州", attr="state"),
    "shipper_postal_code": FieldSpec("shipper", "text", "提货地 邮编", attr="postal_code"),
}
LOAD_KEYS = [k for k, f in FIELDS.items() if f.target in ("leg", "shipment", "mbl") and f.kind != "cargo"]
PROJECT_KEYS = [k for k, f in FIELDS.items() if f.target in ("project", "consignee", "shipper")]
NOTE_FIELD = "note"  # a timeline note, not a field

MAX_EMAIL_CHARS = 60000
MAX_CANDIDATES = 300

# Documents Gemini reads directly (scanned or not): PDFs and photos.
# Sent inline; Vertex AI caps a whole request at 20 MB and base64 adds a third.
DOC_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif"}
MAX_DOC_BYTES = 8 * 1024 * 1024
MAX_DOCS_BYTES = 12 * 1024 * 1024
MAX_DOCS = 10


def doc_mime(name):
    """MIME type if Gemini can read this file as a document, else ''."""
    name = (name or "").lower()
    return next((m for ext, m in DOC_TYPES.items() if name.endswith(ext)), "")


def limit_docs(docs):
    """Keep what fits in one request, in order. Returns (kept, skipped names)."""
    kept, skipped, total = [], [], 0
    for d in docs:
        size = len(d["data"])
        if len(kept) >= MAX_DOCS or size > MAX_DOC_BYTES or total + size > MAX_DOCS_BYTES:
            skipped.append(d["name"])
            continue
        kept.append(d)
        total += size
    return kept, skipped


def sheet_to_text(upload):
    """An uploaded .xlsx / .csv / .tsv / .txt file -> tab-separated text for the model."""
    name = upload.name.lower()
    if upload.size > 5 * 1024 * 1024:
        raise AiError("表格文件太大（超过 5MB），请只保留需要的工作表和行")
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook

        try:
            wb = load_workbook(upload, read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001
            raise AiError(f"无法读取这个 Excel 文件：{e}") from e
        parts = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else (v.strftime("%Y-%m-%d %H:%M").removesuffix(" 00:00") if hasattr(v, "strftime") else str(v).strip())
                         for v in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if cells:
                    rows.append("\t".join(cells))
            if rows:
                parts.append(f"## 工作表：{ws.title}\n" + "\n".join(rows))
        wb.close()
        return "\n\n".join(parts)
    if name.endswith((".csv", ".tsv", ".txt")):
        raw = upload.read()
        for enc in ("utf-8-sig", "gb18030", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    raise AiError("只支持 .xlsx、.csv、.tsv、.txt 文件（Google Sheets 请用 文件 → 下载 → .xlsx）")


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
    if kind in ("text", "carrier"):
        if not s:
            raise ValueError("空值")
        return s[:200]
    if kind == "container":
        v = re.sub(r"[\s\-]", "", s).upper()
        if not re.fullmatch(r"[A-Z0-9]{5,20}", v):
            raise ValueError("不像柜号")
        return v
    if kind == "mbl":
        v = re.sub(r"\s", "", s).upper()
        if not re.fullmatch(r"[A-Z0-9\-]{5,40}", v):
            raise ValueError("不像提单号")
        return v
    if kind == "equipment":
        v = s.upper().replace("'", "").replace(" ", "").replace("-", "")
        if v not in Shipment.Equipment.values:
            raise ValueError("不在柜型列表里")
        return v
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
    if kind == "equipment":
        return dict(Shipment.Equipment.choices).get(s, s)
    if kind == "cargo":
        return cargo_text(json.loads(s))
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
    num = _norm(item.get("load_number"))
    if num:
        for s in cands:
            if _norm(s.number) == num:
                return s
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


def target_obj(spec, shipment, leg, project=None):
    if spec.target == "leg":
        return leg
    if spec.target == "mbl":
        return shipment.master_bl
    if spec.target == "shipment":
        return shipment
    project = project or (shipment.project if shipment is not None else None)
    if project is None:
        return None
    if spec.target == "project":
        return project
    return project.default_consignee if spec.target == "consignee" else project.default_shipper


def _current(obj, key):
    spec = FIELDS[key]
    if spec.kind == "mbl":
        return obj.master_bl.number if obj is not None and obj.master_bl_id else ""
    if spec.kind == "carrier":
        return obj.carrier.name if obj is not None and obj.carrier_id else ""
    return serialize(spec.kind, getattr(obj, spec.attr or key) if obj is not None else None)


# ---------------------------------------------------------------------------
# Gemini call
# ---------------------------------------------------------------------------
_LOCATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "code": {"type": "STRING", "description": "地址库代号；已有地址请用完全相同的代号，新地址起一个简短代号"},
        "name": {"type": "STRING"}, "address": {"type": "STRING"}, "city": {"type": "STRING"},
        "state": {"type": "STRING", "description": "两位州代码"}, "postal_code": {"type": "STRING"},
    },
    "required": ["code", "name", "address", "city", "state", "postal_code"],
}

_CARGO_SCHEMA = {
    "type": "ARRAY",
    "description": "货物明细：每种货物一项（同样的多件写一项，qty 写件数）。重量、尺寸照邮件原文写数字和单位，不要自己换算",
    "items": {"type": "OBJECT", "properties": {
        "description": {"type": "STRING", "description": "英文品名，如 Transformer、BESS unit、Steel coils"},
        "qty": {"type": "STRING", "description": "件数，未写就 1"},
        "handling_type": {"type": "STRING", "description": "包装单位代码之一：" + ", ".join(f"{v}={l}" for v, l in CargoLine.HandlingType.choices) + "；不确定写 UNIT"},
        "weight": {"type": "STRING", "description": "重量，照原文带单位，如 35,000 kg、77,161 lbs、20 t；没写留空"},
        "weight_is_each": {"type": "STRING", "description": "原文重量是每件的写 true，是这一项总重的写 false"},
        "length": {"type": "STRING", "description": "长，照原文带单位，如 40'、13'6\"、12.19 m、480 in；没写留空"},
        "width": {"type": "STRING"},
        "height": {"type": "STRING"}},
        "required": ["description", "qty", "handling_type", "weight", "weight_is_each", "length", "width", "height"]},
}

_PROJECT_SCHEMA = {
        "type": "OBJECT",
        "description": "一个系统里还没有的新项目",
        "properties": {
            "name": {"type": "STRING", "description": "项目名，参照已有项目的命名方式"},
            "company": {"type": "STRING", "description": "我方公司：ATS 或 PTT，不确定留空"},
            "customer": {"type": "STRING", "description": "真正付钱/委托我们的客户名称（绝不能是 ATS 或 PTT）；已有客户请用完全相同的名字"},
            "customer_ref": {"type": "STRING"},
            "existing_project": {"type": "STRING", "description": "如果这批货属于“已有项目”列表里的某个项目（后续批次、补充柜子），填那个项目的 number（如 PRJ-0002）；新项目留空"},
            "monday_item_id": {"type": "STRING", "description": "项目编号，如 #4560 写 P-04560"},
            "start_date": {"type": "STRING", "description": "YYYY-MM-DD"},
            "cargo_description": {"type": "STRING"},
            "notes": {"type": "STRING", "description": "中文：待办事项、注意事项（如 DO 未放、需确认的信息）"},
            "shipper": _LOCATION_SCHEMA,
            "consignee": _LOCATION_SCHEMA,
            "milestones": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "name": {"type": "STRING"}, "planned_date": {"type": "STRING", "description": "YYYY-MM-DD"}},
                "required": ["name", "planned_date"]}},
            "loads": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "container_number": {"type": "STRING", "description": "柜号，未知留空"},
                "equipment": {"type": "STRING", "description": "柜型/车型代码，必须是其中之一：" + "; ".join(f"{v}={l}" for v, l in Shipment.Equipment.choices) + "。列表里没有的写 OTHER，并把原文柜型写进 notes"},
                "service": {"type": "STRING", "description": "服务代码之一：" + ", ".join(Shipment.Service.values)},
                "mbl": {"type": "STRING"},
                "carrier": {"type": "STRING", "description": "承运商/车队名称；已有承运商请用完全相同的名字"},
                "notes": {"type": "STRING", "description": "中文，如 HBL 号、DO 状态、品名"},
                "cargo": _CARGO_SCHEMA},
                "required": ["container_number", "equipment", "service", "mbl", "carrier", "notes"]}},
        },
        "required": ["existing_project", "name", "company", "customer", "customer_ref", "monday_item_id", "start_date",
                     "cargo_description", "notes", "shipper", "consignee", "milestones", "loads"],
    }

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING", "description": "一两句中文概括这封邮件说了什么"},
        "loads": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "load_number": {"type": "STRING", "description": "系统 Load#（如 SHP-2610-0011），没有柜号的 Load 用它识别"},
                    "container_number": {"type": "STRING"},
                    "carrier_load_number": {"type": "STRING"},
                    "updates": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "field": {"type": "STRING", "format": "enum", "enum": LOAD_KEYS},
                                "value": {"type": "STRING"},
                                "evidence": {"type": "STRING", "description": "邮件原文中支持这个值的那句话"},
                            },
                            "required": ["field", "value", "evidence"],
                        },
                    },
                    "note": {"type": "STRING", "description": "值得记到时间线的其他事实，中文，没有就留空"},
                    "cargo": {**_CARGO_SCHEMA, "description": "只在“已有资料”里这个 Load 的 cargo_lines 为空、而内容写明了件数/重量/尺寸时输出；否则空数组。" + _CARGO_SCHEMA["description"]},
                },
                "required": ["load_number", "container_number", "carrier_load_number", "updates", "note"],
            },
        },
        "unmatched": {"type": "STRING", "description": "邮件中提到但无法对应到任何 Load 的信息"},
        "project_updates": {
            "type": "ARRAY",
            "description": "已有项目的项目级信息补全（地址、开始日期等）",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "project": {"type": "STRING", "description": "项目 number（PRJ-xxxx）或项目编号（如 P-04915）"},
                    "field": {"type": "STRING", "format": "enum", "enum": PROJECT_KEYS},
                    "value": {"type": "STRING"},
                    "evidence": {"type": "STRING"},
                },
                "required": ["project", "field", "value", "evidence"],
            },
        },
        "new_projects": {"type": "ARRAY", "description": "内容里每个系统中还没有的新项目一项；没有就空数组", "items": _PROJECT_SCHEMA},
    },
    "required": ["summary", "loads", "unmatched", "project_updates", "new_projects"],
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
   - eta / ata：船预计 / 实际到港日期（属于这个柜子的提单）。
8. evidence 填邮件里支持该值的原句（原文语言，尽量短）。
9. 其他值得记录的事实（延误原因、故障、改约、现场情况、签收人等）用中文简短写进该 Load 的 note。
10. 绝不提取任何金额、费率、费用。
10b. **补全空缺**：系统里还空着的信息（柜号“待定”的 Load、缺 MBL、缺柜型、缺车队，项目的送货地/提货地地址、开始日期等）如果内容里给出了，也要输出：
   - Load 级：container_number / mbl_number / equipment / carrier 写在该 Load 的 updates 里。没有柜号的 Load 用 load_number（系统 Load#）识别，结合项目、柜型、顺序判断是哪一个；判断不了就不要输出。
   - 项目级：写进 project_updates（project 填项目 number 或项目编号）。地址拆开填：address 只写门牌和街道，城市、州、邮编分别填。
11. 如果邮件和列表中的任何 Load 都无关，loads 返回空数组，在 unmatched 里说明。
12. 如果内容是“项目开始”、新订单、新货物安排等，说的是系统里还没有的项目或货物，写进 new_projects，每个项目一项。内容里有几个不同的项目就输出几项（项目编号、客户或送货地不同就是不同项目；同一项目的多个柜子放在同一项的 loads 里）。没有新项目时 new_projects 为空数组。每一项：
   - **先看是不是已有项目的后续批次**：内容里的项目编号（如 #4560 / P-04560）、项目名或客户 + 送货地和“已有资料”里的某个已有项目对得上时，这是给已有项目补充的货，existing_project 填该项目的 number，loads 只写新增的柜子。只有对不上任何已有项目时才是新项目（existing_project 留空）。
   - ATS 和 PTT 是我们自己的公司，只能填在 company，**绝不能填成 customer**。内部邮件里写“客户: PTT”时，指的是这单由 PTT 接，真正的客户是邮件里的发货方 / 委托方（例如“发货方: 环世 Worldwide Logistics USA”就填它）。
   - 每一个柜子 / 每一车货写一条 loads（“×2”就写两条）；柜号未知就留空。柜型按列表代码填（如 20' 框架箱 / 20FR / 20' Flat Rack 填 20FR，海柜 20' 普柜填 20GP）。
   - 客户、承运商、地址如果在“已有资料”里，必须用完全相同的名字或代号。
   - 送货地址写进 consignee；港口、码头或提货地写进 shipper（只有港口名时 code 写港口名、地址留空）。
   - ETA、计划开始日等写进 milestones（例如 name=“船到港 ETA”）。
   - 待确认事项（如 DO 未放、柜号待定、车队待定）用中文写进 notes。
   - 不要编造：邮件没写的就留空。
   此时 loads（更新已有 Load）通常为空。
13. 内容也可能是从表格复制或上传的数据（制表符分隔，可能带“工作表”标题）：按表头理解各列，通常每行是一个柜子 / 一车货。表里已有的柜号按规则 1–11 更新，系统里没有的柜号按规则 12 放进对应项目的 loads。
14. 货物重量和尺寸（cargo）：照原文抄数字和单位（如 13'6"、4.1 m、480 in、35,000 kg、20 t、77,161 lbs），**不要自己换算单位**，系统会统一换成 ft 和 lbs。原文没写的留空。原文一行写“L×W×H”时拆成 length / width / height，各自带上单位（如 "12.19 x 2.44 x 2.59 m" 拆成 12.19 m、2.44 m、2.59 m）。
15. 内容后面可能附有 PDF 或图片（POD 签收单、BOL、Rate Confirmation、Delivery Receipt、到港通知、DO、提货单、照片等），和邮件正文同等对待，规则 1–14 同样适用：
   - 先看单据上的柜号、BOL 号、PRO#、承运商单号，对应到 Load；对不上的不要猜。
   - 签字的 POD / Delivery Receipt：签收日期写 pod_received（同时可以作为 actual_delivery 的依据），签收人、件数异常、货损备注写进 note。
   - evidence 写“附件 文件名：单据上的原文”。
   - 单据上的金额、运费、费率一律忽略（规则 10）。"""


def _context_block(cands, single, scope_project=None):
    rows = []
    for s in cands:
        leg = s.current_leg
        current = {}
        for k in LOAD_KEYS:
            obj = target_obj(FIELDS[k], s, leg)
            if obj is not None or FIELDS[k].kind in ("carrier", "mbl"):
                current[k] = display(FIELDS[k].kind, _current(obj, k))
        rows.append({
            "load_number": s.number,
            "container_number": s.container_number,
            "carrier_load_numbers": [leg.carrier_load_number for leg in s.legs.all() if leg.carrier_load_number],
            "project": f"{s.project.number} {s.project.name}" if s.project_id else "",
            "status": s.get_status_display(),
            "current": current,
            "cargo_lines": [cargo_text(cargo_dict(c)) for c in s.cargo_lines.all()],
        })
    head = "这封邮件就是关于下面这一个 Load 的（只需输出一个 Load）。" if single else "系统里可能相关的 Load："
    text = head + "\n" + json.dumps(rows, ensure_ascii=False)
    projects = {s.project_id: s.project for s in cands if s.project_id}
    if scope_project is not None:
        projects[scope_project.pk] = scope_project
    if projects:
        prows = []
        for p in projects.values():
            prows.append({"number": p.number, "project_code": p.monday_item_id, "name": p.name,
                          "current": {k: display(FIELDS[k].kind, _current(target_obj(FIELDS[k], None, None, p), k))
                                      for k in PROJECT_KEYS if target_obj(FIELDS[k], None, None, p) is not None}})
        text += "\n\n这些 Load 所属的项目（项目级信息，空着的可以补全）：\n" + json.dumps(prows, ensure_ascii=False)
    if not single:
        ref = {
            "customers": list(Customer.objects.values_list("name", flat=True)[:300]),
            "carriers": list(Carrier.objects.values_list("name", flat=True)[:300]),
            "locations": list(Location.objects.values_list("code", flat=True)[:300]),
            "existing_projects": [
                {"number": p.number, "name": p.name, "project_code": p.monday_item_id, "customer": p.customer.name}
                for p in Project.objects.exclude(status__in=[Project.Status.DONE, Project.Status.CANCELLED])
                .select_related("customer").order_by("-created_at")[:200]
            ],
        }
        text += "\n\n已有资料（新建项目时用来对应名字）：\n" + json.dumps(ref, ensure_ascii=False)
    return text


def call_gemini(email_text, context, today, attachments=None):
    """Return the parsed JSON dict from Gemini on Vertex AI. attachments: PDFs / images sent inline."""
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
        "contents": [{"role": "user", "parts": [{"text": f"今天是 {today.isoformat()}。\n\n{context}\n\n=== 邮件开始 ===\n{email_text}\n=== 邮件结束 ==="}]
                      + [part for d in (attachments or []) for part in (
                          {"text": f"=== 附件：{d['name']} ==="},
                          {"inlineData": {"mimeType": d["mime"], "data": base64.b64encode(d["data"]).decode()}})]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA},
    }
    try:
        r = requests.post(url, json=body, headers={"Authorization": f"Bearer {creds.token}"}, timeout=200)
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
def extract(email_text, user, project=None, shipment=None, caller=None, source=AiExtraction.Source.PASTE, attachments=None):
    """Run Gemini on an email (plus PDF / image attachments) and store the proposed changes. Returns AiExtraction.

    attachments: [{"name", "mime", "data": bytes}]; what does not fit in one request is skipped and listed."""
    if not can_edit_ops(user):
        raise AiError("只有操作和管理层可以使用 AI 识别")
    email_text = (email_text or "").strip()
    docs, skipped_docs = limit_docs([d for d in (attachments or []) if d.get("data") and d.get("mime")])
    skipped_docs = [d["name"] for d in (attachments or []) if not d.get("data")] + skipped_docs
    if not email_text and not docs:
        raise AiError("请先粘贴邮件内容或上传文件")
    if len(email_text) > MAX_EMAIL_CHARS:
        raise AiError(f"内容太长（{len(email_text)} 字），请只粘贴相关的邮件正文，{MAX_EMAIL_CHARS} 字以内")
    since = timezone.now() - timedelta(days=1)
    if AiExtraction.objects.filter(created_by=user, created_at__gte=since).count() >= settings.TMS_AI_DAILY_LIMIT:
        raise AiError(f"今天已识别 {settings.TMS_AI_DAILY_LIMIT} 次，达到每人每日上限")

    cands = candidates(project, shipment)
    if not cands and shipment is not None:
        raise AiError("范围内没有 Load 可以对应")
    context = _context_block(cands, shipment is not None, project)
    names = [d["name"] for d in docs]
    prompt_text = email_text or "（只有附件，没有正文）"
    if names:
        prompt_text += "\n\n（附上的文件：" + "、".join(names) + "）"
    kwargs = {"attachments": docs} if docs else {}
    result, usage = (caller or call_gemini)(prompt_text, context, timezone.localdate(), **kwargs)
    if names or skipped_docs:
        result = {**result, "read_attachments": names, "skipped_attachments": skipped_docs}

    ext = AiExtraction.objects.create(
        created_by=user, source=source, project=project, shipment=shipment, source_text=email_text,
        model_name=settings.TMS_AI_MODEL, summary=str(result.get("summary", ""))[:1000],
        raw_result=result, usage=usage or {},
    )
    unmatched = [str(result.get("unmatched") or "").strip()]
    if skipped_docs:
        unmatched.append("附件太大或太多，没有读取：" + "、".join(skipped_docs))
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
            obj = target_obj(spec, s, leg)
            if obj is None and spec.kind != "carrier":
                what = "MBL" if spec.target == "mbl" else "运输段"
                unmatched.append(f"{label} 还没有{what}，无法写入「{spec.label}」")
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
        new_cargo = clean_cargo(item.get("cargo"))
        if new_cargo and s.cargo_lines.exists():
            unmatched.append(f"{label} 已有货物明细，未改动：" + "；".join(cargo_text(c) for c in new_cargo))
        else:
            for c in new_cargo:
                problems = c.pop("problems")
                original = c.pop("original")
                AiChange.objects.create(
                    extraction=ext, shipment=s, field="cargo_line", new_value=json.dumps(c, ensure_ascii=False),
                    evidence=("原文：" + original if original else "") + ("；无法识别：" + "；".join(problems) if problems else ""))
        note = str(item.get("note") or "").strip()
        if note:
            AiChange.objects.create(extraction=ext, shipment=s, field=NOTE_FIELD, new_value=note[:1000])
    for u in result.get("project_updates") or []:
        key = u.get("field")
        if key not in PROJECT_KEYS:
            continue
        spec = FIELDS[key]
        p = match_project({"existing_project": u.get("project"), "monday_item_id": u.get("project")}, project)
        if p is None:
            unmatched.append(f"找不到项目 {u.get('project')}（{spec.label}：{u.get('value')}）")
            continue
        obj = target_obj(spec, None, None, p)
        if obj is None:
            unmatched.append(f"{p.number} 还没有{'送货地' if spec.target == 'consignee' else '提货地'}，无法写入「{spec.label}」：{u.get('value')}")
            continue
        try:
            new = serialize(spec.kind, parse_value(spec.kind, u.get("value")))
        except ValueError as e:
            unmatched.append(f"{p.number}「{spec.label}」无法识别：{u.get('value')}（{e}）")
            continue
        old = _current(obj, key)
        if new != old:
            AiChange.objects.create(extraction=ext, project=p, field=key, old_value=old, new_value=new,
                                    evidence=str(u.get("evidence", ""))[:500])
    ext.unmatched = "\n".join(x for x in unmatched if x)[:2000]
    if not ext.changes.exists() and not proposals(ext):
        ext.status = AiExtraction.State.DONE  # nothing to confirm
    ext.save(update_fields=["unmatched", "status"])
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
    if spec.target in ("project", "consignee", "shipper"):
        if change.project is None or target_obj(spec, None, None, change.project) is None:
            return "项目或地址已不存在"
        return ""
    if spec.kind == "container":
        dup = Shipment.objects.filter(container_number=change.new_value).exclude(pk=s.pk).first()
        if dup:
            return f"柜号已用在 {dup.number}"
    if not spec.lock_exempt and not can_edit_shipment_ops(user, s):
        return "Load 已送达锁定，需管理层解锁"
    if spec.target == "leg" and spec.kind != "carrier" and (change.leg is None or change.leg.is_deleted):
        return "运输段已不存在"
    if spec.target == "mbl" and s.master_bl_id is None:
        return "这个 Load 没有 MBL"
    return ""


@transaction.atomic
def apply_changes(ext, user, approve_ids):
    """Apply the ticked changes, reject the rest. Returns (applied, skipped) lists."""
    approve_ids = {int(i) for i in approve_ids}
    applied, skipped = [], []
    pending = list(ext.changes.select_related("shipment", "leg", "project").filter(status=AiChange.State.PENDING))
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
            if spec.kind == "cargo":
                CargoLine.objects.create(shipment=c.shipment, **cargo_fields(json.loads(c.new_value)))
                c.status = AiChange.State.APPLIED
                applied.append(c)
                c.decided_by, c.decided_at = user, now
                c.save()
                continue
            obj = target_obj(spec, c.shipment, c.leg, c.project)
            if obj is None and spec.kind == "carrier":  # no leg yet: create one with the carrier
                s = c.shipment
                Leg.objects.create(
                    shipment=s, sequence=1, origin=s.shipper, destination=s.consignee,
                    kind=Leg.Kind.DRAYAGE if (s.service or "").startswith("DRAY") else Leg.Kind.OTR,
                    carrier=Carrier.objects.filter(name=c.new_value).first() or Carrier.objects.create(name=c.new_value))
                c.status = AiChange.State.APPLIED
                applied.append(c)
                c.decided_by, c.decided_at = user, now
                c.save()
                continue
            group = "location" if spec.target in ("consignee", "shipper") else spec.target
            key = (group, obj.pk)
            if key not in by_obj:
                model = {"leg": Leg, "mbl": MasterBL, "shipment": Shipment, "project": Project, "location": Location}[group]
                by_obj[key] = model.all_objects.select_for_update().get(pk=obj.pk)
            obj = by_obj[key]
            if _current(obj, c.field) != c.old_value:
                c.status, c.result = AiChange.State.STALE, f"识别后已被改为 {display(spec.kind, _current(obj, c.field))}，未覆盖"
                skipped.append(c)
            else:
                value = deserialize(spec.kind, c.new_value)
                if spec.kind == "mbl":
                    obj.master_bl = MasterBL.objects.get_or_create(number=value)[0]
                elif spec.kind == "carrier":
                    obj.carrier = Carrier.objects.filter(name=value).first() or Carrier.objects.create(name=value)
                else:
                    setattr(obj, spec.attr or c.field, value)
                c.status = AiChange.State.APPLIED
                applied.append(c)
        c.decided_by, c.decided_at = user, now
        c.save()

    reason = f"AI 邮件识别 #{ext.pk}"
    for (target, _), obj in by_obj.items():
        obj._change_reason = reason
        obj.save()
        if target == "mbl":  # ATA moves its Loads from 待到港 to 已到港
            for s in obj.shipments.all():
                s.refresh_status()
    # Shipment.save() / the Leg signal recompute status and locking.
    for s in {c.shipment for c in applied if c.shipment_id}:
        items = [c for c in applied if c.shipment_id == s.pk and c.field != NOTE_FIELD]
        if items:
            text = "；".join(f"{FIELDS[c.field].label} {c.new_display}" for c in items)
            Note.objects.create(shipment=s, created_by=user, kind=Note.Kind.SYSTEM, text=f"通过 AI 邮件识别 #{ext.pk} 更新：{text}")
    for p in {c.project for c in applied if c.project_id}:
        items = [c for c in applied if c.project_id == p.pk]
        text = "；".join(f"{FIELDS[c.field].label} {c.new_display}" for c in items)
        Note.objects.create(project=p, created_by=user, kind=Note.Kind.SYSTEM, text=f"通过 AI 邮件识别 #{ext.pk} 补全：{text}")
    if not open_proposals(ext):
        ext.status = AiExtraction.State.DONE  # else a new-project proposal is still waiting
    ext.reviewed_by, ext.reviewed_at = user, now
    ext.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return applied, skipped


def discard(ext, user):
    ext.changes.filter(status=AiChange.State.PENDING).update(status=AiChange.State.REJECTED, decided_by=user, decided_at=timezone.now())
    ext.status = AiExtraction.State.DISCARDED
    ext.reviewed_by, ext.reviewed_at = user, timezone.now()
    ext.save(update_fields=["status", "reviewed_by", "reviewed_at"])


# ---------------------------------------------------------------------------
# New project proposal (for "project start" / new-order emails)
# ---------------------------------------------------------------------------
LOC_FIELDS = ("code", "name", "address", "city", "state", "postal_code")


def _s(v, n=255):
    return str(v or "").strip()[:n]


MAX_NEW_PROJECTS = 20


def _raw_projects(ext):
    raw = ext.raw_result or {}
    items = raw.get("new_projects")
    if items is None:  # older single-project format
        old = raw.get("new_project") or {}
        items = [old] if old.get("is_new") else []
    return [np for np in (items or []) if isinstance(np, dict) and np.get("is_new", True)][:MAX_NEW_PROJECTS]


def _np_state(ext):
    raw = ext.raw_result or {}
    state = dict(raw.get("np_state") or {})
    if raw.get("created_project_id") and "0" not in state:  # older format
        state["0"] = {"status": "created", "project_id": raw["created_project_id"]}
    return state


def proposals(ext):
    """All new-project proposals, cleaned, each with its index and state (open / created / skipped)."""
    state = _np_state(ext)
    out = []
    for i, np in enumerate(_raw_projects(ext)):
        p = _clean_proposal(np, ext.project)
        st = state.get(str(i), {})
        p.update(index=i, state=st.get("status", "open"), project_id=st.get("project_id"))
        out.append(p)
    return out


def open_proposals(ext):
    return [p for p in proposals(ext) if p["state"] == "open"]


def proposal(ext, index=0):
    ps = proposals(ext)
    return ps[index] if index < len(ps) else None


def _finish_if_done(ext):
    if not open_proposals(ext) and not ext.changes.filter(status=AiChange.State.PENDING).exists():
        ext.status = AiExtraction.State.DONE


def _set_np_state(ext, index, **st):
    state = _np_state(ext)
    state[str(index)] = st
    ext.raw_result = {**(ext.raw_result or {}), "np_state": state}


def skip_project(ext, user, index):
    _set_np_state(ext, index, status="skipped")
    _finish_if_done(ext)
    ext.reviewed_by, ext.reviewed_at = user, timezone.now()
    ext.save(update_fields=["raw_result", "status", "reviewed_by", "reviewed_at"])


def _digits(v):
    d = re.sub(r"\D", "", str(v or "")).lstrip("0")
    return d


def match_project(np, scope_project=None):
    """The existing project a proposal belongs to, if any."""
    ref = _s(np.get("existing_project"), 60)
    open_projects = Project.objects.exclude(status__in=[Project.Status.DONE, Project.Status.CANCELLED])
    if ref:
        p = open_projects.filter(number__iexact=ref).first() or open_projects.filter(name=ref).first()
        if p:
            return p
    code = _digits(np.get("monday_item_id")) or (_digits(ref) if ref and not ref.upper().startswith("PRJ") else "")
    if code:
        for p in open_projects.exclude(monday_item_id=""):
            if _digits(p.monday_item_id) == code:
                return p
    name = _s(np.get("name"), 200)
    if name:
        p = open_projects.filter(name=name).first()
        if p:
            return p
    return scope_project


def clean_cargo(items):
    """Model cargo items (original units) -> dicts in ft / lbs. Unreadable values are left blank and noted."""
    out = []
    for c in items or []:
        desc = _s(c.get("description"), 255)
        if not desc:
            continue
        try:
            qty = max(int(float(str(c.get("qty") or "1").replace(",", ""))), 1)
        except ValueError:
            qty = 1
        ht = str(c.get("handling_type") or "").upper()
        line = {"description": desc, "qty": qty,
                "handling_type": ht if ht in CargoLine.HandlingType.values else CargoLine.HandlingType.UNIT, "problems": []}
        raw = []
        try:
            w = units.parse_weight_lbs(c.get("weight"))
            if w is not None and str(c.get("weight_is_each")).lower() == "true":
                w = (w * qty).quantize(units.LBS)
            line["weight_lbs"] = units.plain(w)
        except ValueError as e:
            line["weight_lbs"] = ""
            line["problems"].append(str(e))
        if _s(c.get("weight")):
            raw.append(f"{c.get('weight')}{' 每件' if str(c.get('weight_is_each')).lower() == 'true' else ''}")
        dims = []
        for d in ("length", "width", "height"):
            try:
                line[f"{d}_ft"] = units.plain(units.parse_length_ft(c.get(d)))
            except ValueError as e:
                line[f"{d}_ft"] = ""
                line["problems"].append(str(e))
            if _s(c.get(d)):
                dims.append(_s(c.get(d)))
        if dims:
            raw.append(" x ".join(dims))
        line["original"] = "；".join(raw)
        out.append(line)
    return out


def cargo_dict(c):
    """CargoLine -> the same dict shape clean_cargo() makes."""
    return {"description": c.description, "qty": c.handling_qty, "handling_type": c.handling_type,
            "weight_lbs": units.plain(c.weight_lbs), "length_ft": units.plain(c.length_ft),
            "width_ft": units.plain(c.width_ft), "height_ft": units.plain(c.height_ft)}


def cargo_text(c):
    parts = [f"{c.get('qty') or 1} × {c.get('description')}"]
    if c.get("weight_lbs"):
        parts.append(f"{float(c['weight_lbs']):,.10g} lbs")
    dims = [c.get(f"{d}_ft") for d in ("length", "width", "height")]
    if any(dims):
        parts.append(" x ".join(x or "?" for x in dims) + " ft")
    return " · ".join(parts)


def cargo_fields(c):
    """Clean dict -> CargoLine keyword arguments."""
    from decimal import Decimal

    dec = lambda v: Decimal(v) if v not in (None, "") else None  # noqa: E731
    return {"handling_qty": c.get("qty") or 1, "handling_type": c.get("handling_type") or CargoLine.HandlingType.UNIT,
            "description": c["description"][:255], "weight_lbs": dec(c.get("weight_lbs")),
            "length_ft": dec(c.get("length_ft")), "width_ft": dec(c.get("width_ft")), "height_ft": dec(c.get("height_ft"))}


def _clean_proposal(np, scope_project=None):
    target = match_project(np, scope_project)
    loads = []
    for ld in np.get("loads") or []:
        cntr = re.sub(r"\s", "", _s(ld.get("container_number"), 20)).upper()
        eq = _s(ld.get("equipment"), 20).upper().replace("'", "").replace(" ", "")
        notes = _s(ld.get("notes"), 1000)
        if eq and eq not in Shipment.Equipment.values:
            notes = f"柜型：{ld.get('equipment')}" + (f"；{notes}" if notes else "")
            eq = Shipment.Equipment.OTHER
        loads.append({
            "container_number": cntr,
            "equipment": eq,
            "service": ld.get("service") if ld.get("service") in Shipment.Service.values else Shipment.Service.OTR,
            "mbl": _s(ld.get("mbl"), 40).upper(),
            "carrier": _s(ld.get("carrier"), 120),
            "notes": notes,
            "exists": bool(cntr) and Shipment.objects.filter(container_number=cntr).exists(),
            "cargo": clean_cargo(ld.get("cargo")),
        })
    locs = {}
    for role in ("shipper", "consignee"):
        loc = {k: _s((np.get(role) or {}).get(k), 160) for k in LOC_FIELDS}
        loc["code"] = loc["code"][:60]
        loc["address"] = clean_street(loc["address"], loc["city"], loc["state"], loc["postal_code"])
        loc["exists"] = bool(loc["code"]) and Location.objects.filter(code=loc["code"]).exists()
        locs[role] = loc
    start = np.get("start_date")
    try:
        start = parse_value("date", start).isoformat() if start else ""
    except ValueError:
        start = ""
    milestones = []
    for m in np.get("milestones") or []:
        try:
            d = parse_value("date", m.get("planned_date")).isoformat()
        except ValueError:
            continue
        if _s(m.get("name")):
            milestones.append({"name": _s(m.get("name"), 120), "planned_date": d})
    customer = _s(np.get("customer"), 120)
    company = np.get("company") if np.get("company") in Company.values else ""
    own = own_company(customer)
    if own:  # the model put our own company in as the customer
        company, customer = company or own, ""
    return {
        "target": target.pk if target else None, "target_name": f"{target.number} {target.name}" if target else "",
        "name": _s(np.get("name"), 200), "company": company, "customer_was_own": bool(own),
        "customer": customer, "customer_exists": Customer.objects.filter(name=customer).exists(),
        "customer_ref": _s(np.get("customer_ref"), 80), "monday_item_id": _s(np.get("monday_item_id"), 40),
        "start_date": start, "cargo_description": _s(np.get("cargo_description")), "notes": _s(np.get("notes"), 2000),
        "shipper": locs["shipper"], "consignee": locs["consignee"], "milestones": milestones, "loads": loads,
    }


def clean_street(address, city, state, zip_code):
    """'43851 Division ST Lancaster CA 93535' -> '43851 Division ST' when city/state/zip are filled separately."""
    a = address.strip().rstrip(",")
    for part in (zip_code, state, city):
        if part and a.upper().endswith(part.upper()):
            a = a[: -len(part)].rstrip(" ,")
    return a or address


def own_company(name):
    """'PTT' if a name is really one of our own companies, else ''."""
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not n:
        return ""
    for code, aliases in settings.TMS_OWN_COMPANY_ALIASES.items():
        # Short codes must match exactly (so "PTTEP Co" is not PTT); longer names may have a suffix.
        if any(n == a if len(a) <= 4 else n.startswith(a) for a in aliases):
            return code
    return ""


def create_project(ext, user, form):
    """Create the project and its Loads from the (edited) proposal form. Returns the Project."""
    from .importer import ImportError_, import_data

    if not can_edit_ops(user):
        raise AiError("没有新建项目的权限")
    try:
        index = int(form.get("np_index") or 0)
    except ValueError as e:
        raise AiError("表单有误，请刷新页面重试") from e
    prop = proposal(ext, index)
    if prop is None or prop["state"] != "open":
        raise AiError("这个新项目已经处理过了")
    target = form.get("np_target", "new")
    if target != "new":
        project = Project.objects.filter(pk=int(target) if str(target).isdigit() else 0).first()
        if project is None:
            raise AiError("选择的项目不存在")
        return _add_to_project(ext, user, form, index, prop, project)
    g = lambda k, n=255: _s(form.get(k), n)  # noqa: E731
    name, customer = g("np_name", 200), g("np_customer", 120)
    if not name or not customer:
        raise AiError("项目名称和客户必须填写")
    if own_company(customer):
        raise AiError(f"「{customer}」是我们自己的公司，请在“公司”里选它，客户填真正的委托方")
    data = {"customers": [{"name": customer}], "carriers": [], "locations": [], "projects": []}
    proj = {
        "name": name, "customer": customer, "company": g("np_company") if g("np_company") in Company.values else Company.ATS,
        "monday_item_id": g("np_monday_item_id", 40), "start_date": g("np_start_date", 10) or None,
        "cargo_description": g("np_cargo_description"), "notes": g("np_notes", 2000),
        "owners": [user.email] if user.email else [], "source": f"AI 邮件识别 #{ext.pk}",
        "milestones": list(prop["milestones"]) if form.get("np_milestones") else [],
        "timeline": [{"date": timezone.localdate().isoformat(), "text": f"由 AI 邮件识别 #{ext.pk} 创建（{user.get_full_name() or user.email}）"}],
        "loads": [],
    }
    for role in ("shipper", "consignee"):
        code = g(f"np_{role}_code", 60)
        if code:
            loc = {k: g(f"np_{role}_{k}", 160) for k in LOC_FIELDS}
            loc["code"] = code
            loc["kind"] = Location.Kind.SITE if role == "consignee" else Location.Kind.TERMINAL
            data["locations"].append(loc)
            proj[f"default_{role}"] = code
    n = int(form.get("np_load_count") or 0)
    for i in range(min(n, 200)):
        if not form.get(f"np_load_{i}_include"):
            continue
        carrier = g(f"np_load_{i}_carrier", 120)
        service = g(f"np_load_{i}_service")
        ld = {
            "container_number": re.sub(r"\s", "", g(f"np_load_{i}_container_number", 20)).upper(),
            "equipment": g(f"np_load_{i}_equipment") if g(f"np_load_{i}_equipment") in Shipment.Equipment.values else "",
            "service": service if service in Shipment.Service.values else Shipment.Service.OTR,
            "mbl": g(f"np_load_{i}_mbl", 40), "notes": g(f"np_load_{i}_notes", 1000), "customer_ref": g("np_customer_ref", 80),
            "cargo": [{"handling_qty": c["qty"], **{k: v for k, v in cargo_fields(c).items() if v is not None}} for c in _form_cargo(form, i)],
        }
        if carrier:
            data["carriers"].append({"name": carrier})
            ld["legs"] = [{"kind": Leg.Kind.DRAYAGE if ld["service"].startswith("DRAY") else Leg.Kind.OTR, "carrier": carrier}]
        proj["loads"].append(ld)
    eta = next((m["planned_date"] for m in prop["milestones"] if "ETA" in m["name"].upper() or "到港" in m["name"]), None)
    if eta:
        for ld in proj["loads"]:
            if ld["mbl"]:
                ld["mbl_eta"] = eta
    data["projects"].append(proj)
    if Project.objects.filter(customer__name=customer, name=name).exists():
        raise AiError(f"客户「{customer}」下已经有同名项目「{name}」，请改个名字")
    try:
        log_lines = import_data(data, user=user)
    except (ImportError_, ValueError) as e:
        raise AiError(f"创建失败：{e}") from e
    p = Project.objects.get(customer__name=customer, name=name)
    _set_np_state(ext, index, status="created", project_id=p.pk)
    ext.raw_result = {**ext.raw_result, "create_log": log_lines}
    _finish_if_done(ext)
    ext.reviewed_by, ext.reviewed_at = user, timezone.now()
    ext.save(update_fields=["raw_result", "status", "reviewed_by", "reviewed_at"])
    return p


def _form_cargo(form, i):
    """Cargo lines typed on the proposal form (any unit) -> clean dicts. Raises AiError on bad values."""
    g = lambda k, n=255: _s(form.get(k), n)  # noqa: E731
    out = []
    for j in range(min(int(form.get(f"np_load_{i}_cargo_count") or 0), 50)):
        k = f"np_load_{i}_cargo_{j}_"
        desc = g(k + "description")
        if not desc:
            continue
        try:
            qty = max(int(g(k + "qty") or 1), 1)
            line = {"description": desc, "qty": qty,
                    "handling_type": g(k + "type") if g(k + "type") in CargoLine.HandlingType.values else CargoLine.HandlingType.UNIT,
                    "weight_lbs": units.plain(units.parse_weight_lbs(g(k + "weight")))}
            for d in ("length", "width", "height"):
                line[f"{d}_ft"] = units.plain(units.parse_length_ft(g(k + d)))
        except ValueError as e:
            raise AiError(f"货物明细「{desc}」：{e}") from e
        out.append(line)
    return out


def _form_loads(form):
    g = lambda k, n=255: _s(form.get(k), n)  # noqa: E731
    out = []
    for i in range(min(int(form.get("np_load_count") or 0), 200)):
        if not form.get(f"np_load_{i}_include"):
            continue
        service = g(f"np_load_{i}_service")
        out.append({
            "container_number": re.sub(r"\s", "", g(f"np_load_{i}_container_number", 20)).upper(),
            "equipment": g(f"np_load_{i}_equipment") if g(f"np_load_{i}_equipment") in Shipment.Equipment.values else "",
            "service": service if service in Shipment.Service.values else Shipment.Service.OTR,
            "mbl": g(f"np_load_{i}_mbl", 40).upper(), "carrier": g(f"np_load_{i}_carrier", 120), "notes": g(f"np_load_{i}_notes", 1000),
            "cargo": _form_cargo(form, i),
        })
    return out


@transaction.atomic
def _add_to_project(ext, user, form, index, prop, project):
    """Add a later batch (new Loads, ticked milestones) to an existing project."""
    eta = next((m["planned_date"] for m in prop["milestones"] if "ETA" in m["name"].upper() or "到港" in m["name"]), None)
    added, skipped = [], []
    for ld in _form_loads(form):
        if ld["container_number"] and Shipment.objects.filter(container_number=ld["container_number"]).exists():
            skipped.append(ld["container_number"])
            continue
        s = Shipment(customer=project.customer, project=project, container_number=ld["container_number"],
                     equipment=ld["equipment"], service=ld["service"], monday_item_id=project.monday_item_id,
                     shipper=project.default_shipper, consignee=project.default_consignee, notes=ld["notes"],
                     source_ref=f"AI 邮件识别 #{ext.pk}")
        if ld["mbl"]:
            s.master_bl = MasterBL.objects.get_or_create(number=ld["mbl"])[0]
            if eta and not s.master_bl.eta:
                s.master_bl.eta = date.fromisoformat(eta)
                s.master_bl.save()
        s.save()
        for c in ld["cargo"]:
            CargoLine.objects.create(shipment=s, **cargo_fields(c))
        if ld["carrier"]:
            Leg.objects.create(shipment=s, sequence=1, kind=Leg.Kind.DRAYAGE if ld["service"].startswith("DRAY") else Leg.Kind.OTR,
                               origin=s.shipper, destination=s.consignee,
                               carrier=Carrier.objects.filter(name=ld["carrier"]).first() or Carrier.objects.create(name=ld["carrier"]))
        s.refresh_status()
        added.append(s.container_number or s.number)
    if form.get("np_milestones"):
        for m in prop["milestones"]:
            Milestone.objects.get_or_create(project=project, name=m["name"], planned_date=date.fromisoformat(m["planned_date"]))
    text = f"AI 邮件识别 #{ext.pk} 新增 {len(added)} 个 Load：{'、'.join(added) or '—'}"
    if skipped:
        text += f"（已存在未重复建：{'、'.join(skipped)}）"
    Note.objects.create(project=project, created_by=user, kind=Note.Kind.SYSTEM, text=text)
    project.refresh_status()
    _set_np_state(ext, index, status="created", project_id=project.pk)
    _finish_if_done(ext)
    ext.reviewed_by, ext.reviewed_at = user, timezone.now()
    ext.save(update_fields=["raw_result", "status", "reviewed_by", "reviewed_at"])
    return project
