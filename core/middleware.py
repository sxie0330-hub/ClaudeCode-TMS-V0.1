from django.conf import settings
from django.shortcuts import redirect, render

from .permissions import has_access

PUBLIC_PREFIXES = ("/accounts/", "/static/", "/healthz", "/dev-login/", "/admin/login/")


class RequireLoginMiddleware:
    """Everything requires a signed-in user who has been given a role."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if path.startswith(PUBLIC_PREFIXES):
            return self.get_response(request)
        if not request.user.is_authenticated:
            return redirect(f"{settings.LOGIN_URL}?next={path}")
        if not has_access(request.user) and not path.startswith("/logout"):
            return render(request, "core/no_access.html", status=403)
        return self.get_response(request)
