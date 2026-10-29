"""Connect a mailbox for the daily scan, see its status, run it now."""
from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import ai, mailscan
from .models import AiExtraction, GmailConnection
from .permissions import can_edit_ops
from .views import _need


def mail_settings(request):
    _need(can_edit_ops(request.user))
    conn = GmailConnection.objects.filter(user=request.user).first()
    recent = AiExtraction.objects.filter(source=AiExtraction.Source.GMAIL, created_by=request.user).select_related("project")[:20]
    return render(request, "core/mail.html", {
        "conn": conn, "recent": recent, "ai_enabled": ai.enabled(),
        "scanned": conn.scanned.count() if conn else 0,
    })


def mail_connect(request):
    _need(can_edit_ops(request.user))
    try:
        return redirect(mailscan.auth_url(request, request.user))
    except mailscan.ScanError as e:
        messages.error(request, str(e))
        return redirect("mail_settings")


def mail_callback(request):
    _need(can_edit_ops(request.user))
    if request.GET.get("error"):
        messages.warning(request, "没有完成授权，邮箱没有连接")
        return redirect("mail_settings")
    try:
        conn = mailscan.finish_connect(request, request.user, request.GET.get("code", ""), request.GET.get("state", ""))
    except (mailscan.ScanError, Exception) as e:  # noqa: BLE001 — show any failure to the person
        messages.error(request, f"连接失败：{e}")
        return redirect("mail_settings")
    messages.success(request, f"已连接 {conn.email}。每天傍晚自动扫描一次，也可以点“现在扫描一次”。")
    return redirect("mail_settings")


@require_POST
def mail_disconnect(request):
    conn = GmailConnection.objects.filter(user=request.user).first()
    if conn:
        mailscan.disconnect(conn)
        messages.info(request, "已断开邮箱，TMS 不会再读取你的邮件")
    return redirect("mail_settings")


@require_POST
def mail_scan_now(request):
    _need(can_edit_ops(request.user))
    conn = GmailConnection.objects.filter(user=request.user).first()
    if not conn:
        return redirect("mail_settings")
    if not ai.enabled():
        messages.error(request, "AI 识别没有开启")
        return redirect("mail_settings")
    res = mailscan.scan_one(conn)
    if res.get("error"):
        messages.error(request, res["error"])
    else:
        messages.success(request, res["result"])
    return redirect("mail_settings")


@csrf_exempt
@require_POST
def task_scan_email(request):
    """Called once a day by Cloud Scheduler with header X-TMS-Task-Key."""
    key = settings.TMS_TASK_KEY
    if not key or not constant_time_compare(request.headers.get("X-TMS-Task-Key", ""), key):
        return JsonResponse({"error": "forbidden"}, status=403)
    if not ai.enabled():
        return JsonResponse({"error": "AI not enabled"}, status=503)
    return JsonResponse({"mailboxes": mailscan.scan_all()})
