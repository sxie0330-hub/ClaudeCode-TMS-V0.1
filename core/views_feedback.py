"""报告问题: anyone signed in can report from any page; management sees and answers the list."""
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import ProblemReport
from .notify import managers, notify
from .permissions import is_management, roles_of
from .views import _need


@require_POST
def feedback_new(request):
    text = request.POST.get("text", "").strip()
    if not text:
        return JsonResponse({"ok": False, "error": "请写一句话说明问题"}, status=400)
    kind = request.POST.get("kind", ProblemReport.Kind.BUG)
    if kind not in ProblemReport.Kind.values:
        kind = ProblemReport.Kind.BUG
    r = ProblemReport.objects.create(
        user=request.user, kind=kind, text=text[:5000],
        url=request.POST.get("url", "")[:500] or request.META.get("HTTP_REFERER", "")[:500],
        page_title=request.POST.get("title", "")[:200], roles=",".join(sorted(roles_of(request.user)))[:60],
        lang=request.COOKIES.get("tms_lang", "zh")[:5], user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        js_errors=request.POST.get("js_errors", "")[:4000],
    )
    who = request.user.get_full_name() or request.user.email
    notify(managers(), f"{who} 报告了问题 #{r.pk}（{r.get_kind_display()}）：{text[:80]}", "/feedback/", exclude=request.user)
    return JsonResponse({"ok": True, "id": r.pk})


def feedback_list(request):
    _need(is_management(request.user))
    status = request.GET.get("status", "open")
    qs = ProblemReport.objects.select_related("user")
    if status == "open":
        qs = qs.exclude(status__in=[ProblemReport.State.DONE, ProblemReport.State.WONTFIX])
    elif status:
        qs = qs.filter(status=status)
    return render(request, "core/feedback_list.html", {
        "reports": qs[:300], "status": status, "states": ProblemReport.State.choices,
    })


@require_POST
def feedback_update(request, pk):
    _need(is_management(request.user))
    r = get_object_or_404(ProblemReport, pk=pk)
    st = request.POST.get("status")
    if st in ProblemReport.State.values:
        r.status = st
    r.reply = request.POST.get("reply", r.reply)[:5000]
    pr = request.POST.get("pr_url", "").strip()
    r.pr_url = pr if pr.startswith("https://") else r.pr_url
    r.save()
    if r.user_id and st in (ProblemReport.State.DONE, ProblemReport.State.WONTFIX):
        notify(r.user, f"你报告的问题 #{r.pk} {r.get_status_display()}" + (f"：{r.reply[:80]}" if r.reply else ""), "/",
               exclude=request.user)
    messages.success(request, f"已更新 #{r.pk}")
    return redirect("feedback_list")
