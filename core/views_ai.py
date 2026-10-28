"""Pages for AI email reading: paste → review → apply."""
from collections import OrderedDict

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import ai
from .models import AiChange, AiExtraction, Project, Shipment
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
    changes = list(ext.changes.select_related("shipment", "leg"))
    groups = OrderedDict()
    for c in changes:
        c.blocked = ai.blocked_reason(c, request.user) if c.status == AiChange.State.PENDING else ""
        groups.setdefault(c.shipment, []).append(c)
    return render(request, "core/ai_review.html", {
        "ext": ext, "groups": groups, "pending": any(c.status == AiChange.State.PENDING for c in changes),
    })


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
