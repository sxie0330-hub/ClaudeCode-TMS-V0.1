import re

from django.conf import settings
from django.shortcuts import redirect, render

from .permissions import has_access

INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00a0\s]+")

PUBLIC_PREFIXES = ("/lang/", "/accounts/", "/static/", "/healthz", "/health/", "/tasks/", "/dev-login/", "/admin/login/")


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


class ProjectAccessMiddleware:
    """OP / 询报价 only reach the projects they own and those projects' Loads (see permissions.sees_all_projects).
    Pages addressed by a project, Load, rate agreement, charge or Load row are checked here, in one place;
    a project someone does not own answers 404, as if it did not exist."""

    PROJECT = {"pin_project", "project_detail", "project_edit", "project_close", "project_note", "project_bulk",
               "milestone_create", "milestone_edit", "rate_agreement_new"}
    LOAD = {"watch_load", "shipment_detail", "shipment_edit", "shipment_docs", "shipment_delete", "shipment_unlock",
            "shipment_lock", "shipment_close", "shipment_pod", "shipment_note", "mbl_edit", "shipment_bol", "child_create",
            "charge_create", "permit_create", "permit_prefill"}
    RATE = {"rate_agreement", "rate_agreement_edit", "rate_agreement_pdf"}
    CHARGE = {"charge_edit", "charge_delete"}
    ROW = {"child_edit", "child_delete"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from django.http import Http404

        from .permissions import can_see_project, sees_all_projects

        user = getattr(request, "user", None)
        match = getattr(request, "resolver_match", None)
        if not user or not user.is_authenticated or not match or sees_all_projects(user):
            return None
        project = self._project(match.url_name, view_kwargs)
        if project is not False and not can_see_project(user, project):
            raise Http404
        return None

    def _project(self, name, kw):
        """The project behind this page; None for a Load without project; False when the page is not about one."""
        from .models import Charge, Project, RateAgreement, Shipment

        def load_project(pk):
            s = Shipment.all_objects.filter(pk=pk).select_related("project").first()
            return s.project if s else None

        if name in self.PROJECT:
            return Project.all_objects.filter(pk=kw.get("pk")).first()
        if name in self.LOAD:
            return load_project(kw.get("pk"))
        if name in self.RATE:
            a = RateAgreement.objects.filter(pk=kw.get("pk")).select_related("project").first()
            return a.project if a else None
        if name in self.CHARGE:
            c = Charge.objects.filter(pk=kw.get("cid")).first()
            return load_project(c.shipment_id) if c else None
        if name in self.ROW:
            from .views import CHILDREN

            model = CHILDREN.get(kw.get("kind"), (None,))[0]
            obj = model.objects.filter(pk=kw.get("cid")).first() if model else None
            return load_project(obj.shipment.pk) if obj else None
        return False
