from django.conf import settings

from .models import Charge, Role
from .permissions import can_edit_ops, can_see_margin, is_management, role_of, visible_sides


def role(request):
    user = request.user
    sides = visible_sides(user)
    return {
        "user_role": role_of(user),
        "role_label": Role(role_of(user)).label if role_of(user) else "",
        "perm": {
            "edit_ops": can_edit_ops(user),
            "mgmt": is_management(user),
            "see_ar": Charge.Side.AR in sides,
            "see_ap": Charge.Side.AP in sides,
            "see_margin": can_see_margin(user),
        },
        "company_name": settings.TMS_COMPANY_NAME,
    }
