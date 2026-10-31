"""Who may see and do what.

  管理层 MGMT : everything, incl. both AR and AP, margin, unlock, revert, users
  操作 OP     : edit operations + master data; sees only the projects they own (and their Loads)
  应付 AP     : read operations, see/edit AP
  应收 AR     : read operations, see/edit AR — never sees AP
  询报价 RFQ  : inquiries and quotes (incl. RFQ-stage costs); read operations of the projects they own

A person may hold several roles; they get the union of what each allows.

Money visibility is enforced everywhere money can surface: charge tables,
timeline/audit entries, CSV export and search.
"""
from django.core.exceptions import PermissionDenied

from .models import Charge, Profile, Role


def roles_of(user):
    """Set of roles a user holds. A person can hold several (e.g. 操作 + 询报价)."""
    if not user.is_authenticated:
        return set()
    if user.is_superuser:
        return {Role.MANAGEMENT}
    profile = getattr(user, "profile", None)
    if profile is None:
        profile, _ = Profile.objects.get_or_create(user=user)
    return profile.role_set


def has_access(user):
    return bool(roles_of(user))


def is_management(user):
    return Role.MANAGEMENT in roles_of(user)


def can_edit_ops(user):
    return bool(roles_of(user) & {Role.MANAGEMENT, Role.OP})


def can_quote(user):
    """Inquiries and quotes: RFQ / sales, operations and management. RFQ-stage costs are visible to all three."""
    return bool(roles_of(user) & {Role.MANAGEMENT, Role.OP, Role.RFQ})


def can_manage_rates(user):
    """Carrier rate agreements: management, AP and operations (OP issue them to carriers, management decision 2026-10-30).
    OP still sees no other AP amounts."""
    return bool(roles_of(user) & {Role.MANAGEMENT, Role.OP, Role.AP})


SIDES = {
    Role.MANAGEMENT: {Charge.Side.AR, Charge.Side.AP},
    Role.OP: set(),  # operations see no AR / AP money (management decision 2026-10-30)
    Role.AP: {Charge.Side.AP},
    Role.AR: {Charge.Side.AR},
}


def visible_sides(user):
    """Charge sides (AR/AP) this user may see: the union over their roles."""
    out = set()
    for r in roles_of(user):
        out |= SIDES.get(r, set())
    return out


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


# ---------------------------------------------------------------------------
# Which projects (and their Loads) a person sees
# ---------------------------------------------------------------------------
SEE_ALL_PROJECTS = {Role.MANAGEMENT, Role.AP, Role.AR}


def sees_all_projects(user):
    """管理层 / 应付 / 应收 see every project; 操作 and 询报价 only the projects they own.
    A colleague covering for someone is added as an owner of that project."""
    return bool(roles_of(user) & SEE_ALL_PROJECTS)


def visible_projects(user, qs=None):
    from .models import Project

    qs = Project.objects.all() if qs is None else qs
    if sees_all_projects(user):
        return qs
    return qs.filter(pk__in=Project.all_objects.filter(owners=user).values("pk"))


def visible_loads(user, qs=None):
    """Loads of the projects this person sees, plus Loads that belong to no project."""
    from django.db.models import Q

    from .models import Project, Shipment

    qs = Shipment.objects.all() if qs is None else qs
    if sees_all_projects(user):
        return qs
    return qs.filter(Q(project_id__in=Project.all_objects.filter(owners=user).values("pk")) | Q(project__isnull=True))


def can_see_project(user, project):
    if project is None or sees_all_projects(user):
        return True
    return project.owners.filter(pk=user.pk).exists()
