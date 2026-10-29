"""In-app notifications: a bell with the unread count, a pop-up on the next page
(or within a minute while a page is open), and an optional desktop alert.

Nothing is e-mailed."""
from django.contrib.auth import get_user_model
from django.db.models import Q

from .models import Notification, Profile, Role


def notify(users, text, url="", exclude=None):
    """Send one notice to each user (a user, a list or a queryset). `exclude` is usually the person acting."""
    if users is None:
        return 0
    if not hasattr(users, "__iter__"):
        users = [users]
    rows = [Notification(user=u, text=text[:300], url=url[:300]) for u in users if u is not None and u != exclude]
    Notification.objects.bulk_create(rows)
    return len(rows)


def managers():
    User = get_user_model()
    return User.objects.filter(Profile.with_roles(Role.MANAGEMENT) | Q(is_superuser=True), is_active=True).distinct()
