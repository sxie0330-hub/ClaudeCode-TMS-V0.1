"""Daily health report for the automatic check (GitHub Actions "Daily health data" → the daily Claude session).

GET /tasks/health-report/?hours=26 returns JSON: new problem reports, how AI reading went (what people rejected,
scans that found nothing), mailbox scan errors, and data gaps (planned pickups / deliveries passed with nothing recorded).
No e-mail text, no prices. Allowed for the deploy service account's Google ID token, or the task key Cloud Scheduler uses.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_GET

from .models import AiChange, AiExtraction, GmailConnection, Leg, ProblemReport, Project, Status


def _authorized(request):
    key = settings.TMS_TASK_KEY
    if key and constant_time_compare(request.headers.get("X-TMS-Task-Key", ""), key):
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    try:
        from google.auth.transport import requests as g_requests
        from google.oauth2 import id_token

        info = id_token.verify_oauth2_token(auth[7:], g_requests.Request(), audience=settings.TMS_HEALTH_AUDIENCE)
    except Exception:  # noqa: BLE001 — any bad, expired or foreign token is simply refused
        return False
    return info.get("email_verified") and info.get("email") in settings.TMS_HEALTH_REPORT_ACCOUNTS


def _who(u):
    return (u.get_full_name() or u.username) if u else ""


def report(hours=26, now=None):
    now = now or timezone.now()
    since = now - timedelta(hours=hours)
    today = timezone.localdate(now)
    reports = ProblemReport.objects.filter(created_at__gte=since).select_related("user")
    open_reports = ProblemReport.objects.exclude(status__in=[ProblemReport.State.DONE, ProblemReport.State.WONTFIX])
    ext = AiExtraction.objects.filter(created_at__gte=since)
    changes = AiChange.objects.filter(extraction__created_at__gte=since)
    open_loads = Leg.objects.filter(shipment__is_deleted=False, shipment__project__status__in=[
        Project.Status.PENDING, Project.Status.ACTIVE, Project.Status.DELIVERED]).exclude(
        shipment__status__in=[Status.CANCELLED, Status.CLOSED])
    late_pick = open_loads.filter(scheduled_pickup__lt=today - timedelta(days=1), actual_pickup__isnull=True)
    late_del = open_loads.filter(scheduled_delivery__lt=today - timedelta(days=1), actual_delivery__isnull=True)
    return {
        "generated_at": now.isoformat(), "window_hours": hours,
        "problem_reports": {
            "new": [{"id": r.pk, "at": r.created_at.isoformat(), "by": _who(r.user), "roles": r.roles, "kind": r.kind,
                     "text": r.text[:1500], "url": r.url, "lang": r.lang, "js_errors": r.js_errors[:1500], "status": r.status}
                    for r in reports],
            "open_total": open_reports.count(),
            "open_ids": list(open_reports.values_list("pk", flat=True)[:50]),
        },
        "ai_reading": {
            "extractions": list(ext.values("source", "status").annotate(n=Count("id")).order_by("source", "status")),
            "gmail_without_any_change": ext.filter(source=AiExtraction.Source.GMAIL, changes__isnull=True).distinct().count(),
            "change_outcomes": list(changes.values("status").annotate(n=Count("id")).order_by("status")),
            "rejected_by_field": list(changes.filter(status=AiChange.State.REJECTED).values("field")
                                      .annotate(n=Count("id")).order_by("-n")[:15]),
            "blocked_by_field": list(changes.filter(status=AiChange.State.BLOCKED).values("field", "result")
                                     .annotate(n=Count("id")).order_by("-n")[:15]),
        },
        "mailbox_scans": [{"who": _who(c.user), "auto": c.auto_scan, "hours": c.scan_hours(), "scope": c.scan_scope,
                           "last_scan_at": c.last_scan_at.isoformat() if c.last_scan_at else None,
                           "last_result": c.last_result, "last_error": c.last_error}
                          for c in GmailConnection.objects.filter(enabled=True).select_related("user")],
        "data_gaps": {
            "planned_pickup_passed_not_recorded": late_pick.count(),
            "planned_delivery_passed_not_recorded": late_del.count(),
            "by_project": list(late_pick.values("shipment__project__number").annotate(n=Count("id")).order_by("-n")[:15]),
        },
    }


@require_GET
def health_report(request):
    if not _authorized(request):
        return JsonResponse({"error": "forbidden"}, status=403)
    try:
        hours = max(1, min(int(request.GET.get("hours", 26)), 24 * 14))
    except ValueError:
        hours = 26
    return JsonResponse(report(hours), json_dumps_params={"ensure_ascii": False})
