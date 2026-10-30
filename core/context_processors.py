from django.conf import settings

from .models import Charge, Role
from .permissions import can_edit_ops, can_quote, can_see_margin, is_management, roles_of, visible_sides


def role(request):
    user = request.user
    sides = visible_sides(user)
    return {
        "user_role": ",".join(sorted(roles_of(user))),
        "role_label": "、".join(Role(r).label for r in Role.values if r in roles_of(user)),
        "perm": {
            "edit_ops": can_edit_ops(user),
            "quote": can_quote(user),
            "mgmt": is_management(user),
            "see_ar": Charge.Side.AR in sides,
            "see_ap": Charge.Side.AP in sides,
            "see_margin": can_see_margin(user),
        },
        "company_name": settings.TMS_COMPANY_NAME,
        "en": request.COOKIES.get("tms_lang") == "en",
        **_notifications(user),
    }


def _notifications(user):
    """Unread count and recent items for the bell; unseen ones pop up once on this page."""
    if not user.is_authenticated:
        return {}
    from django.utils import timezone

    from .models import Notification

    qs = Notification.objects.filter(user=user)
    popups = list(qs.filter(shown_at__isnull=True, read_at__isnull=True)[:5])
    if popups:
        Notification.objects.filter(pk__in=[n.pk for n in popups]).update(shown_at=timezone.now())
    return {"notif_unread": qs.filter(read_at__isnull=True).count(), "notif_recent": list(qs[:8]), "notif_popups": popups}
