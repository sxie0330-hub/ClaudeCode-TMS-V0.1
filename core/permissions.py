"""Who may see and do what.

  管理层 MGMT : everything, incl. both AR and AP, margin, unlock, revert, users
  操作 OP     : edit operations + master data, see/edit AP
  应付 AP     : read operations, see/edit AP
  应收 AR     : read operations, see/edit AR — never sees AP

Money visibility is enforced everywhere money can surface: charge tables,
timeline/audit entries, CSV export and search.
"""
from django.core.exceptions import PermissionDenied

from .models import Charge, Profile, Role


def role_of(user):
    if not user.is_authenticated:
        return ""
    if user.is_superuser:
        return Role.MANAGEMENT
    profile = getattr(user, "profile", None)
    if profile is None:
        profile, _ = Profile.objects.get_or_create(user=user)
    return profile.role


def has_access(user):
    return bool(role_of(user))


def is_management(user):
    return role_of(user) == Role.MANAGEMENT


def can_edit_ops(user):
    return role_of(user) in (Role.MANAGEMENT, Role.OP)


def visible_sides(user):
    """Charge sides (AR/AP) this user may see."""
    return {
        Role.MANAGEMENT: {Charge.Side.AR, Charge.Side.AP},
        Role.OP: {Charge.Side.AP},
        Role.AP: {Charge.Side.AP},
        Role.AR: {Charge.Side.AR},
    }.get(role_of(user), set())


def can_see_side(user, side):
    return side in visible_sides(user)


def can_see_margin(user):
    return is_management(user)


def require(check, user, *args):
    if not check(user, *args):
        raise PermissionDenied


def can_edit_shipment_ops(user, shipment):
    """Ops fields of a delivered/closed shipment are locked for everyone but management."""
    if not can_edit_ops(user):
        return False
    return not shipment.is_locked or is_management(user)
