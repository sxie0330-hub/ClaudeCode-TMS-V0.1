import re

from django.conf import settings
from django.shortcuts import redirect, render

from .permissions import has_access

INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00a0\s]+")

PUBLIC_PREFIXES = ("/accounts/", "/static/", "/healthz", "/health/", "/tasks/", "/dev-login/", "/admin/login/")


class RequireLoginMiddleware:
    """Everything requires a signed-in user who has been given a role."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        clean = INVISIBLE.sub("", path)
        if clean != path:  # links pasted from chat apps often carry zero-width spaces
            qs = request.META.get("QUERY_STRING", "")
            return redirect((clean or "/") + (f"?{qs}" if qs else ""))
        if path.startswith(PUBLIC_PREFIXES):
            return self.get_response(request)
        if not request.user.is_authenticated:
            return redirect(f"{settings.LOGIN_URL}?next={path}")
        if not has_access(request.user) and not path.startswith("/logout"):
            return render(request, "core/no_access.html", status=403)
        return self.get_response(request)
