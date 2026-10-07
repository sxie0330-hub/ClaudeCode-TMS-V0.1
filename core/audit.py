"""Timeline / audit trail built from django-simple-history records."""
from dataclasses import dataclass, field
from datetime import datetime

from django.db import models

from .models import CargoLine, Charge, Leg, MasterBL, Note, Project, Shipment, Unit, YardStay
from .permissions import visible_sides

# Models whose history can be shown and reverted, keyed by a short URL-safe name.
TRACKED = {
    "shipment": Shipment,
    "leg": Leg,
    "cargo": CargoLine,
    "unit": Unit,
    "yard": YardStay,
    "charge": Charge,
    "mbl": MasterBL,
    "project": Project,
}
MODEL_LABELS = {
    "shipment": "运单",
    "leg": "运输段",
    "cargo": "货物明细",
    "unit": "设备序列号",
    "yard": "堆场停留",
    "charge": "费用",
    "mbl": "提单",
    "project": "项目",
}
IGNORED_FIELDS = {"updated_at", "created_at", "status", "is_locked", "number", "id"}


@dataclass
class Change:
    field: str
    old: str
    new: str


@dataclass
class Entry:
    when: datetime
    who: str
    action: str  # 新建 / 修改 / 删除 / 恢复 / 备注 / 系统 / BOL
    subject: str
    changes: list = field(default_factory=list)
    text: str = ""
    model_key: str = ""
    history_id: int | None = None
    revertible: bool = False


def _display(model, field_name, value):
    if value in (None, ""):
        return "—"
    try:
        f = model._meta.get_field(field_name)
    except Exception:
        return str(value)
    if f.choices:
        return str(dict(f.flatchoices).get(value, value))
    if isinstance(f, models.ForeignKey):
        rel = f.related_model
        mgr = getattr(rel, "all_objects", rel._default_manager)
        obj = mgr.filter(pk=value).first()
        return str(obj) if obj else f"#{value}"
    if isinstance(f, models.BooleanField):
        return "是" if value else "否"
    return str(value)


def _label(model, field_name):
    try:
        return str(model._meta.get_field(field_name).verbose_name)
    except Exception:
        return field_name


def _subject(key, record):
    model = TRACKED[key]
    base = MODEL_LABELS[key]
    if key == "leg":
        return f"{base} #{record.sequence} {_display(model, 'kind', record.kind)}"
    if key == "charge":
        return f"{_display(model, 'side', record.side)} · {_display(model, 'charge_type', record.charge_type)}"
    if key == "unit":
        return f"{base} {record.serial_number}"
    if key == "cargo":
        return f"{base} {record.description[:30]}"
    if key == "yard":
        return f"{base} {_display(model, 'yard', record.yard_id)}"
    if key == "mbl":
        return f"{base} {record.number}"
    return base


def entries_for_records(key, records, can_revert=False):
    model = TRACKED[key]
    out = []
    for rec in records:
        who = rec.history_user.get_full_name() or rec.history_user.email or rec.history_user.username if rec.history_user else "系统"
        subject = _subject(key, rec)
        if rec.history_type == "+":
            out.append(Entry(rec.history_date, who, "新建", subject, model_key=key, history_id=rec.history_id))
            continue
        prev = rec.prev_record
        if prev is None:
            continue
        delta = rec.diff_against(prev)
        changes = [
            Change(_label(model, c.field), _display(model, c.field, c.old), _display(model, c.field, c.new))
            for c in delta.changes
            if c.field not in IGNORED_FIELDS
        ]
        if not changes:
            continue
        action = "修改"
        deleted_change = next((c for c in delta.changes if c.field == "is_deleted"), None)
        if deleted_change:
            action = "删除" if deleted_change.new else "恢复"
            changes = [c for c in changes if c.field != _label(model, "is_deleted")]
        out.append(
            Entry(
                rec.history_date,
                who,
                action,
                subject,
                changes=changes,
                model_key=key,
                history_id=rec.history_id,
                revertible=can_revert,
            )
        )
    return out


def _note_entries(notes):
    kinds = {Note.Kind.NOTE: "备注", Note.Kind.SYSTEM: "系统", Note.Kind.BOL: "BOL"}
    return [
        Entry(
            n.created_at,
            (n.created_by.get_full_name() or n.created_by.email or n.created_by.username) if n.created_by else "系统",
            kinds.get(n.kind, "备注"),
            "",
            text=n.text,
        )
        for n in notes
    ]


def shipment_timeline(shipment, user, can_revert=False):
    sides = visible_sides(user)
    hist = {
        "shipment": Shipment.history.filter(id=shipment.id),
        "leg": Leg.history.filter(shipment_id=shipment.id),
        "cargo": CargoLine.history.filter(shipment_id=shipment.id),
        "unit": Unit.history.filter(shipment_id=shipment.id),
        "yard": YardStay.history.filter(shipment_id=shipment.id),
        # Charges the user may not see never reach the timeline.
        "charge": Charge.history.filter(shipment_id=shipment.id, side__in=sides),
    }
    if shipment.master_bl_id:
        hist["mbl"] = MasterBL.history.filter(id=shipment.master_bl_id)
    entries = []
    for key, qs in hist.items():
        entries += entries_for_records(key, qs.select_related("history_user"), can_revert)
    entries += _note_entries(shipment.timeline_notes.select_related("created_by"))
    entries.sort(key=lambda e: e.when, reverse=True)
    return entries


def project_timeline(project, can_revert=False, limit=200):
    entries = entries_for_records("project", Project.history.filter(id=project.id).select_related("history_user"), can_revert)
    notes = Note.objects.filter(models.Q(project=project) | models.Q(shipment__project=project)).select_related(
        "created_by", "shipment"
    )[:limit]
    for n, e in zip(notes, _note_entries(notes)):
        if n.shipment_id:
            e.subject = n.shipment.number
        entries.append(e)
    entries.sort(key=lambda e: e.when, reverse=True)
    return entries[:limit]


def revert(key, history_id, user):
    """Undo one change: put the fields it touched back to their previous values."""
    model = TRACKED[key]
    rec = model.history.get(history_id=history_id)
    if key == "charge" and rec.side not in visible_sides(user):
        raise PermissionError
    prev = rec.prev_record
    if rec.history_type != "~" or prev is None:
        raise ValueError("只能回滚修改类记录")
    obj = model.all_objects.get(pk=rec.id)
    delta = rec.diff_against(prev)
    for c in delta.changes:
        if c.field in IGNORED_FIELDS:
            continue
        f = model._meta.get_field(c.field)
        setattr(obj, f.attname, getattr(c.old, "pk", c.old))
    obj._change_reason = f"回滚记录 #{history_id}"
    obj.save()
    return obj
