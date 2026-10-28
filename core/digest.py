"""Daily e-mail with each person's 今日待办 (to-do list).

Cloud Scheduler calls /tasks/daily-digest/ every working morning; the view
checks a shared key and calls send_all(). Each person gets only what the
web page would show them (same rules, settings, scope, money visibility and
snoozes). Nobody gets an empty e-mail.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from . import alerts as alert_engine
from .permissions import has_access

log = logging.getLogger(__name__)


def site_url():
    if settings.TMS_SITE_URL:
        return settings.TMS_SITE_URL
    hosts = [h for h in settings.ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1", "*")]
    return f"https://{hosts[0]}" if hosts else ""


def build(user):
    """(subject, text, html) for this user, or None if there is nothing to do."""
    items = [a for a in alert_engine.collect(user) if not a.snoozed]
    if not items:
        return None
    base = site_url()
    for a in items:
        a.abs_url = (base + a.url) if a.url.startswith("/") else a.url
    levels = [(k, label, [a for a in items if a.level == k]) for k, label in alert_engine.LEVELS]
    c = alert_engine.counts(items)
    parts = [f"{c[k]} {label}" for k, label in alert_engine.LEVELS if c.get(k)]
    subject = f"TMS 今日待办：{' · '.join(parts)}（{timezone.localdate():%m-%d}）"
    ctx = {"user": user, "levels": levels, "todo_url": base + "/todo/", "settings_url": base + "/todo/settings/",
           "total": len(items), "today": timezone.localdate()}
    return subject, render_to_string("core/email/digest.txt", ctx), render_to_string("core/email/digest.html", ctx)


def send_to(user, connection=None):
    built = build(user)
    if not built:
        return False
    subject, text, html = built
    msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [user.email], connection=connection)
    msg.attach_alternative(html, "text/html")
    msg.send()
    return True


def recipients():
    User = get_user_model()
    for u in User.objects.filter(is_active=True).exclude(email="").select_related("profile").order_by("id"):
        profile = getattr(u, "profile", None)
        if has_access(u) and (profile is None or profile.email_digest):
            yield u


def send_all():
    """Returns {"sent": n, "empty": n, "failed": [emails]}."""
    from django.core.mail import get_connection

    result = {"sent": 0, "empty": 0, "failed": []}
    with get_connection() as conn:
        for u in recipients():
            try:
                if send_to(u, connection=conn):
                    result["sent"] += 1
                else:
                    result["empty"] += 1
            except Exception:  # noqa: BLE001 — one bad address must not stop the others
                log.exception("digest failed for %s", u.email)
                result["failed"].append(u.email)
    return result
