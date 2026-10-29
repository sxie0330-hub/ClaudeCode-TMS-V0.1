from datetime import date, timedelta
from unittest import mock
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CargoLine, Carrier, Charge, Customer, Leg, Location, Profile, Project, Role, Shipment, Status, YardStay


def make_user(username, role):
    user = get_user_model().objects.create_user(username=username, email=f"{username}@advtransolution.com", password="pw-123456")
    Profile.objects.update_or_create(user=user, defaults={"role": role})
    return user


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mgmt = make_user("boss", Role.MANAGEMENT)
        cls.op = make_user("op", Role.OP)
        cls.ap = make_user("ap", Role.AP)
        cls.ar = make_user("ar", Role.AR)
        cls.nobody = make_user("new", "")
        cls.customer = Customer.objects.create(name="Trina")
        cls.yard = Location.objects.create(code="NJ Yard", kind=Location.Kind.YARD, address="250 Port St", city="Newark", state="NJ")
        cls.site = Location.objects.create(code="Holyoke", kind=Location.Kind.SITE, address="361 Whitney Ave", city="Holyoke", state="MA")
        cls.carrier = Carrier.objects.create(name="KD Trucking", code="KD")
        cls.project = Project.objects.create(customer=cls.customer, name="Holyoke", cargo_description="Energy Storage System",
                                             un_number="UN3536", hazard_class="9", default_consignee=cls.site)

    def setUp(self):
        self.s = Shipment.objects.create(customer=self.customer, project=self.project, container_number="cymu2523606", consignee=self.site)
        self.leg = Leg.objects.create(shipment=self.s, sequence=1, origin=self.yard, destination=self.site, carrier=self.carrier)
        self.ar_charge = Charge.objects.create(shipment=self.s, side="AR", charge_type="LINEHAUL", unit_price=Decimal("9100"))
        self.ap_charge = Charge.objects.create(shipment=self.s, side="AP", charge_type="LINEHAUL", unit_price=Decimal("6543"), carrier=self.carrier)

    def login(self, user):
        self.client.force_login(user)


class NumberingAndStatusTests(Base):
    def test_numbers_and_normalisation(self):
        self.assertRegex(self.s.number, r"^SHP-\d{4}-\d{4}$")
        self.assertEqual(self.s.container_number, "CYMU2523606")
        self.assertRegex(self.project.number, r"^PRJ-\d{4}$")

    def test_status_follows_dates_and_locks_on_delivery(self):
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, Status.PLANNING)
        YardStay.objects.create(shipment=self.s, yard=self.yard, in_date=date(2026, 1, 1))
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, Status.IN_YARD)
        self.leg.actual_pickup = date(2026, 1, 5)
        self.leg.save()
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, Status.IN_TRANSIT)
        self.assertFalse(self.s.is_locked)
        self.leg.actual_delivery = date(2026, 1, 6)
        self.leg.save()
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, Status.DELIVERED)
        self.assertTrue(self.s.is_locked)

    def test_leg_ending_at_yard_is_not_delivered(self):
        dray = Leg.objects.create(shipment=self.s, sequence=0, origin=self.site, destination=self.yard,
                                  actual_pickup=date(2026, 1, 1), actual_delivery=date(2026, 1, 2))
        self.leg.delete()  # only the drayage leg (ends at the yard) remains
        dray.save()
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, Status.IN_YARD)
        self.assertFalse(self.s.is_locked)

    def test_hold_overrides(self):
        self.s.manual_status = Status.HOLD
        self.s.save()
        self.assertEqual(self.s.status, Status.HOLD)

    def test_yard_days_by_month(self):
        y = YardStay(yard=self.yard, in_date=date(2026, 1, 30), out_date=date(2026, 3, 2), free_days=5)
        self.assertEqual(y.total_days(), 32)
        self.assertEqual(y.billable_days(), 27)
        self.assertEqual(y.days_by_month(), [("2026-01", 2), ("2026-02", 28), ("2026-03", 2)])

    def test_charge_amount(self):
        c = Charge.objects.create(shipment=self.s, side="AP", charge_type="DETENTION", quantity=Decimal("3"), unit_price=Decimal("85"))
        self.assertEqual(c.amount, Decimal("255"))


class AccessTests(Base):
    def test_anonymous_redirected_to_login(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    def test_user_without_role_blocked(self):
        self.login(self.nobody)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_ar_never_sees_ap(self):
        self.login(self.ar)
        page = self.client.get(self.s.get_absolute_url()).content.decode()
        self.assertIn("9,100.00", page)
        self.assertNotIn("6,543", page)
        self.assertNotIn("应付合计", page)
        csv = self.client.get(reverse("shipment_export") + "?status=").content.decode()
        self.assertIn("应收合计", csv)
        self.assertNotIn("应付合计", csv)
        self.assertNotIn("6543", csv)
        # Not via timeline either
        self.ap_charge.unit_price = Decimal("7000")
        self.ap_charge.save()
        page = self.client.get(self.s.get_absolute_url()).content.decode()
        self.assertNotIn("7000", page)
        # and not via direct URLs
        self.assertEqual(self.client.get(reverse("charge_edit", args=[self.ap_charge.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("charge_create", args=[self.s.pk, "AP"])).status_code, 403)

    def test_ap_and_op_never_see_ar(self):
        for user in (self.ap, self.op):
            self.login(user)
            page = self.client.get(self.s.get_absolute_url()).content.decode()
            self.assertIn("6,543.00", page)
            self.assertNotIn("9,100", page)
            self.assertNotIn("毛利", page)

    def test_management_sees_margin(self):
        self.login(self.mgmt)
        page = self.client.get(self.s.get_absolute_url()).content.decode()
        self.assertIn("$2,557.00", page)

    def test_finance_roles_cannot_edit_operations(self):
        for user in (self.ap, self.ar):
            self.login(user)
            self.assertEqual(self.client.get(reverse("shipment_edit", args=[self.s.pk])).status_code, 403)
            self.assertEqual(self.client.get(reverse("child_create", args=[self.s.pk, "leg"])).status_code, 403)

    def test_locked_shipment_only_editable_by_management(self):
        self.leg.actual_pickup = self.leg.actual_delivery = date(2026, 1, 6)
        self.leg.save()
        self.login(self.op)
        self.assertEqual(self.client.get(reverse("shipment_edit", args=[self.s.pk])).status_code, 403)
        # documents stay editable after delivery
        self.assertEqual(self.client.get(reverse("shipment_docs", args=[self.s.pk])).status_code, 200)
        self.login(self.mgmt)
        self.assertEqual(self.client.get(reverse("shipment_edit", args=[self.s.pk])).status_code, 200)


class EditingTests(Base):
    def form_data(self, **over):
        data = {
            "customer": self.customer.pk, "project": self.project.pk, "service": "OTR", "container_number": "CYMU2523606",
            "consignee": self.site.pk, "mbl_number": "zimushh31566854",
        }
        data.update(over)
        return data

    def test_edit_creates_history_and_mbl(self):
        self.login(self.op)
        page = self.client.get(reverse("shipment_edit", args=[self.s.pk])).content.decode()
        import re
        version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
        r = self.client.post(reverse("shipment_edit", args=[self.s.pk]), self.form_data(_version=version, seal_number="SEAL1"))
        self.assertEqual(r.status_code, 302)
        self.s.refresh_from_db()
        self.assertEqual(self.s.master_bl.number, "ZIMUSHH31566854")
        timeline = self.client.get(self.s.get_absolute_url()).content.decode()
        self.assertIn("SEAL1", timeline)

    def test_stale_form_rejected(self):
        self.login(self.op)
        r = self.client.post(reverse("shipment_edit", args=[self.s.pk]), self.form_data(_version="2000-01-01T00:00:00+00:00"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("已被其他人修改", r.content.decode())

    def test_revert(self):
        self.s.seal_number = "WRONG"
        self.s.save()
        rec = self.s.history.first()
        self.login(self.mgmt)
        self.client.post(reverse("history_revert", args=["shipment", rec.history_id]))
        self.s.refresh_from_db()
        self.assertEqual(self.s.seal_number, "")

    def test_only_management_reverts(self):
        self.s.seal_number = "X"
        self.s.save()
        self.login(self.op)
        r = self.client.post(reverse("history_revert", args=["shipment", self.s.history.first().history_id]))
        self.assertEqual(r.status_code, 403)

    def test_soft_delete_hides_but_keeps(self):
        self.login(self.op)
        self.client.post(reverse("child_delete", args=["leg", self.leg.pk]))
        self.assertFalse(Leg.objects.filter(pk=self.leg.pk).exists())
        self.assertTrue(Leg.all_objects.filter(pk=self.leg.pk).exists())

    def test_bulk_create_uses_project_defaults(self):
        self.login(self.op)
        r = self.client.post(reverse("project_bulk", args=[self.project.pk]), {
            "lines": "CYMU0000001, MBL1, SN-1\nCYMU0000002\tMBL1\nCYMU2523606", "service": "OTR", "equipment": "20SOC",
        })
        self.assertEqual(r.status_code, 302)
        new = Shipment.objects.filter(container_number__in=["CYMU0000001", "CYMU0000002"])
        self.assertEqual(new.count(), 2)
        self.assertEqual({s.master_bl.number for s in new}, {"MBL1"})
        line = CargoLine.objects.get(shipment__container_number="CYMU0000001")
        self.assertTrue(line.hazmat)
        self.assertEqual(line.un_number, "UN3536")
        # existing container in the project is skipped, not duplicated
        self.assertEqual(Shipment.objects.filter(container_number="CYMU2523606").count(), 1)


class BolTests(Base):
    def test_single_and_batch_pdf(self):
        CargoLine.objects.create(shipment=self.s, description="Lithium ion batteries installed in cargo transport unit",
                                 hazmat=True, un_number="UN3536", hazard_class="9", weight_lbs=Decimal("77161"))
        other = Shipment.objects.create(customer=self.customer, service="LTL")
        for i in range(8):
            CargoLine.objects.create(shipment=other, handling_qty=2, handling_type="PLT", description=f"Spare parts {i}", weight_lbs=500)
        self.login(self.ap)
        r = self.client.get(reverse("shipment_bol", args=[self.s.pk]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        r = self.client.post(reverse("bulk_bol"), {"ids": [self.s.pk, other.pk]})
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertTrue(self.s.timeline_notes.filter(kind="BOL").exists())

    def test_bol_is_english_only(self):
        from core.bol import bol_context, english
        self.assertEqual(english("ATS#5202 SEG Bayport TX → Tomball TX"), "ATS#5202 SEG Bayport TX -> Tomball TX")
        self.assertEqual(english("测试项目 Demo（20FR）"), "Demo(20FR)")
        self.assertEqual(english("Call before arrival\n到场前一小时电话通知\nNo forklift", drop_lines=True),
                         "Call before arrival\nNo forklift")
        self.assertEqual(english("纯中文"), "")
        self.s.project.bol_instructions = "司机须带 PPE\nPPE required on site"
        self.s.project.save()
        ctx = bol_context(self.s, self.leg)
        self.assertEqual(ctx["instructions"], "PPE required on site")
        self.assertEqual(ctx["company"]["name"], settings.TMS_COMPANY_NAME)

    def test_bol_header_follows_project_company(self):
        from core.bol import bol_context
        self.s.project.company = "PTT"
        self.s.project.save()
        with self.settings(TMS_DOC_COMPANIES={**settings.TMS_DOC_COMPANIES,
                                              "PTT": {"name": "PTT Test Co", "address": "1 Main St", "phone": "", "logo": "logo-ptt"}}):
            ctx = bol_context(self.s, self.leg)
            self.assertEqual(ctx["company"]["name"], "PTT Test Co")
            self.assertIsNone(ctx["company"]["logo_path"])
            self.login(self.ap)
            r = self.client.get(reverse("shipment_bol", args=[self.s.pk]))
            self.assertTrue(r.content.startswith(b"%PDF"))

    def test_bol_fills_exactly_one_page(self):
        import re as _re
        self.login(self.ap)
        r = self.client.get(reverse("shipment_bol", args=[self.s.pk]))
        self.assertEqual(len(_re.findall(rb"/Type /Page\b", r.content)), 1)

    def test_long_driver_instructions_move_to_attached_sheet(self):
        import re as _re
        self.login(self.ap)
        for lines, pages in [(30, 2), (200, 6)]:
            self.leg.bol_instructions = "\n".join(f"{i}. Report to staging area and wait for escort before moving." for i in range(lines))
            self.leg.save()
            r = self.client.get(reverse("shipment_bol", args=[self.s.pk]) + f"?leg={self.leg.pk}")
            self.assertEqual(len(_re.findall(rb"/Type /Page\b", r.content)), pages)

    def test_bol_pages_carry_bol_number(self):
        from core.bol import bol_context, render_bols
        for i in range(40):
            CargoLine.objects.create(shipment=self.s, description=f"Part {i}", weight_lbs=100)
        from reportlab import rl_config
        old, rl_config.pageCompression = rl_config.pageCompression, 0
        try:
            pdf = render_bols([bol_context(self.s, self.leg)])
        finally:
            rl_config.pageCompression = old
        self.assertIn(f"BOL {self.leg.effective_bol_number}".encode(), pdf)
        self.assertIn(b"Page 2", pdf)

    def test_cargo_dims_in_feet(self):
        line = CargoLine.objects.create(shipment=self.s, description="Transformer", weight_lbs=Decimal("185000"),
                                        length_ft=Decimal("28.5"), width_ft=Decimal("12"), height_ft=Decimal("13.75"))
        self.assertEqual(line.dims, "28.5 x 12 x 13.75")
        from core.bol import bol_context
        self.assertEqual(bol_context(self.s, self.leg)["lines"][0].dims, "28.5 x 12 x 13.75")

    def test_bol_number(self):
        self.assertEqual(self.leg.effective_bol_number, f"{self.s.number}-1")


class SearchTests(Base):
    def test_search_jumps_to_single_shipment(self):
        self.login(self.ar)
        r = self.client.get(reverse("search") + "?q=2523606")
        self.assertEqual(r.status_code, 302)

    def test_lfd_warning(self):
        self.s.lfd = date.today() + timedelta(days=1)
        self.s.save()
        self.assertTrue(self.s.lfd_warning)


@override_settings(TMS_ALLOWED_EMAIL_DOMAINS=["advtransolution.com"])
class TeamTests(Base):
    def test_manager_assigns_role(self):
        self.login(self.mgmt)
        self.client.post(reverse("team"), {"user": self.nobody.pk, "role": "AR"})
        self.nobody.profile.refresh_from_db()
        self.assertEqual(self.nobody.profile.role, "AR")

    def test_non_manager_cannot(self):
        self.login(self.op)
        self.assertEqual(self.client.get(reverse("team")).status_code, 403)


class ImportTests(Base):
    SAMPLE = {
        "customers": [{"name": "Hithium"}],
        "carriers": [{"name": "Harris Brokerage", "code": "Harris"}],
        "locations": [{"code": "KHE Carson", "kind": "YARD", "address": "851 E Watson Center Rd"},
                      {"code": "Nokesville", "kind": "SITE", "address": "10311 Reid Ln"}],
        "projects": [{
            "name": "PTT#4475 test", "customer": "Hithium", "company": "PTT", "owners": ["boss@advtransolution.com"],
            "default_shipper": "KHE Carson", "default_consignee": "Nokesville",
            "milestones": [{"name": "Test Run", "done_date": "2026-09-28"}],
            "loads": [
                {"container_number": "SHPU2204302", "pod_received": "2026-10-16",
                 "legs": [{"carrier": "Harris Brokerage", "carrier_load_number": "BLN30784", "actual_pickup": "2026-10-09",
                           "actual_delivery": "2026-10-13", "delivery_arrived_at": "2026-10-13T09:20:00-04:00"}],
                 "charges": [{"side": "AR", "unit_price": 28500}, {"side": "AP", "unit_price": 24000, "carrier": "Harris Brokerage"}]},
                {"container_number": "SHPU2204133",
                 "legs": [{"carrier": "Harris Brokerage", "actual_pickup": "2026-10-09"}]},
            ],
        }],
    }

    def test_import_is_idempotent_and_sets_status(self):
        from .importer import import_data

        import_data(self.SAMPLE)
        p = Project.objects.get(name="PTT#4475 test")
        self.assertEqual(p.company, "PTT")
        self.assertEqual(list(p.owners.all()), [self.mgmt])
        self.assertEqual(p.shipments.count(), 2)
        self.assertEqual(p.status, Project.Status.ACTIVE)  # one delivered, one in transit
        s = p.shipments.get(container_number="SHPU2204302")
        self.assertEqual(s.status, Status.DELIVERED)
        self.assertEqual(s.legs.get().carrier_load_number, "BLN30784")
        log = import_data(self.SAMPLE)
        self.assertIn("跳过", log[0])
        self.assertEqual(Project.objects.filter(name="PTT#4475 test").count(), 1)

    def test_import_converts_legacy_inch_dims(self):
        import copy
        from .importer import import_data

        data = copy.deepcopy(self.SAMPLE)
        data["projects"][0]["loads"][1]["cargo"] = [
            {"description": "BESS unit", "weight_lbs": 77000, "length_in": 240, "width_in": 96, "height_in": 114},
            {"description": "Skid", "length_ft": 8, "width_ft": 4, "height_ft": 3.5},
        ]
        import_data(data)
        lines = {c.description: c for c in CargoLine.objects.filter(shipment__container_number="SHPU2204133")}
        self.assertEqual(lines["BESS unit"].dims, "20 x 8 x 9.5")
        self.assertEqual(lines["Skid"].dims, "8 x 4 x 3.5")

    def test_dry_run_writes_nothing(self):
        from .importer import import_data

        import_data(self.SAMPLE, dry_run=True)
        self.assertFalse(Project.objects.filter(name="PTT#4475 test").exists())

    def test_upload_page(self):
        import json as _json

        from django.core.files.uploadedfile import SimpleUploadedFile
        self.login(self.op)
        self.assertEqual(self.client.get(reverse("import_upload")).status_code, 403)
        self.login(self.mgmt)
        f = SimpleUploadedFile("p.json", _json.dumps(self.SAMPLE).encode())
        r = self.client.post(reverse("import_upload"), {"file": f})
        self.assertContains(r, "导入项目")
        # wipe needs the typed confirmation
        f = SimpleUploadedFile("p.json", _json.dumps(self.SAMPLE).encode())
        r = self.client.post(reverse("import_upload"), {"file": f, "wipe": "1", "confirm": "no"})
        self.assertContains(r, "DELETE")
        self.assertTrue(Project.objects.filter(name="PTT#4475 test").exists())
        f = SimpleUploadedFile("p.json", _json.dumps(self.SAMPLE).encode())
        self.client.post(reverse("import_upload"), {"file": f, "wipe": "1", "confirm": "DELETE"})
        self.assertFalse(Shipment.objects.filter(container_number="CYMU2523606").exists())
        self.assertTrue(Project.objects.filter(name="PTT#4475 test").exists())


class ProjectStatusTests(Base):
    def test_status_follows_loads(self):
        p = Project.objects.create(customer=self.customer, name="Flow")
        self.assertEqual(p.status, Project.Status.PENDING)
        s = Shipment.objects.create(customer=self.customer, project=p, consignee=self.site)
        leg = Leg.objects.create(shipment=s, origin=self.yard, destination=self.site, actual_pickup=date(2026, 1, 1))
        p.refresh_from_db()
        self.assertEqual(p.status, Project.Status.ACTIVE)
        leg.actual_delivery = date(2026, 1, 3)
        leg.save()
        p.refresh_from_db()
        self.assertEqual(p.status, Project.Status.DELIVERED)
        p.manual_status = Project.Status.DONE
        p.save()
        self.assertEqual(p.status, Project.Status.DONE)

    def test_milestones_derived(self):
        from .views import _milestones

        Leg.objects.filter(pk=self.leg.pk).update(actual_pickup=date(2026, 1, 1), actual_delivery=date(2026, 1, 4))
        self.leg.refresh_from_db()
        self.leg.save()
        self.s.pod_received = date(2026, 1, 6)
        self.s.save()
        rows = {m["name"]: m for m in _milestones(self.project, list(self.project.shipments.prefetch_related("legs")))}
        self.assertTrue(rows["末车送达"]["done"])
        self.assertEqual(rows["POD 收齐"]["date"], date(2026, 1, 6))


class AlertTests(Base):
    def setUp(self):
        super().setUp()
        self.project.owners.add(self.op)
        self.s.lfd = date.today() + timedelta(days=1)
        self.s.save()

    def keys(self, user, **kw):
        from . import alerts
        return {a.rule for a in alerts.collect(user, **kw)}

    def test_lfd_only_for_owner_by_default(self):
        self.assertIn("lfd", self.keys(self.op))
        self.assertNotIn("lfd", self.keys(self.ap))  # not an owner, scope = mine

    def test_settings_scope_and_disable(self):
        from .models import AlertSetting
        AlertSetting.objects.create(user=self.ap, rule="lfd", scope="all")
        self.assertIn("lfd", self.keys(self.ap))
        AlertSetting.objects.filter(user=self.ap, rule="lfd").update(enabled=False)
        self.assertNotIn("lfd", self.keys(self.ap))

    def test_snooze(self):
        from . import alerts
        a = next(a for a in alerts.collect(self.op) if a.rule == "lfd")
        self.login(self.op)
        self.client.post(reverse("snooze"), {"key": a.key, "days": "3"})
        self.assertNotIn("lfd", self.keys(self.op))

    def test_money_rules_respect_roles(self):
        Charge.objects.create(shipment=self.s, side="AP", charge_type="DETENTION", unit_price=Decimal("300"), carrier=self.carrier)
        self.project.owners.add(self.ar, self.ap)
        self.assertIn("extra_charges", self.keys(self.ap))
        self.assertNotIn("extra_charges", self.keys(self.ar))
        self.assertNotIn("low_margin", self.keys(self.op))

    def test_detention_running(self):
        from django.utils import timezone
        self.leg.actual_pickup = date.today()
        self.leg.delivery_arrived_at = timezone.now() - timedelta(hours=3)
        self.leg.save()
        self.assertIn("detention", self.keys(self.op))

    def test_custom_rule(self):
        from .models import CustomRule
        CustomRule.objects.create(user=self.op, name="Trina 盯一下", field1="customer", value1="Trina")
        self.assertIn("custom", self.keys(self.op))

    def test_pages_render(self):
        self.login(self.op)
        for url in [reverse("todo"), reverse("alert_settings"), self.project.get_absolute_url()]:
            self.assertEqual(self.client.get(url).status_code, 200)


class DashboardTests(Base):
    def test_default_layout_and_role_filtered_widgets(self):
        from . import widgets
        self.login(self.ar)
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)
        kinds = {w["type"] for w in widgets.get_layout(self.ar).layout}
        self.assertIn("todo", kinds)
        self.assertNotIn("extra_charges", kinds)  # AP-only widget
        cleaned = widgets.clean_layout(self.ar, [{"type": "margin_month"}, {"type": "nope"}, {"type": "note", "size": "XL"}])
        self.assertEqual([w["type"] for w in cleaned], ["note"])
        self.assertEqual(cleaned[0]["size"], "S")

    def test_save_keeps_server_config(self):
        import json as _json
        from . import widgets
        self.login(self.mgmt)
        board = widgets.get_layout(self.mgmt)
        mp = next(w for w in board.layout if w["type"] == "my_projects")
        new = [{"id": mp["id"], "type": "my_projects", "size": "W", "config": {"columns": ["evil"]}}]
        r = self.client.post(reverse("dashboard_save"), _json.dumps(new), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        board.refresh_from_db()
        self.assertEqual(board.layout[0]["size"], "W")
        self.assertEqual(board.layout[0]["config"]["columns"], widgets.DEFAULT_PROJECT_COLUMNS)

    def test_pin_and_watch(self):
        from . import widgets
        self.login(self.op)
        self.client.post(reverse("pin_project", args=[self.project.pk]))
        self.client.post(reverse("watch_load", args=[self.s.pk]))
        kinds = [w["type"] for w in widgets.get_layout(self.op).layout]
        self.assertIn("project", kinds)
        self.assertIn("watched_loads", kinds)
        self.assertContains(self.client.get(reverse("home")), self.s.container_number)

    def test_widget_config(self):
        from . import widgets
        self.login(self.mgmt)
        board = widgets.get_layout(self.mgmt)
        mp = next(w for w in board.layout if w["type"] == "my_projects")
        self.client.post(reverse("widget_config", args=[mp["id"]]), {"columns": ["name", "margin"], "scope": "all", "statuses": ["PENDING"]})
        board.refresh_from_db()
        cfg = next(w for w in board.layout if w["id"] == mp["id"])["config"]
        self.assertEqual(cfg["columns"], ["name", "margin"])
        self.assertContains(self.client.get(reverse("home")), self.project.name)


class SuggestTests(Base):
    def test_suggest_and_prefix_filters(self):
        self.login(self.ar)
        data = self.client.get(reverse("search_suggest") + "?q=2523606").json()
        self.assertEqual(data["groups"][0]["label"], "Load")
        data = self.client.get(reverse("search_suggest"), {"q": "客户:Trina"}).json()
        self.assertTrue(any(g["label"] == "Load" for g in data["groups"]))
        data = self.client.get(reverse("search_suggest"), {"q": "客户:Nobody"}).json()
        self.assertFalse(any(g["label"] == "Load" for g in data["groups"]))

    def test_invoice_search_hides_other_side(self):
        self.ap_charge.invoice_number = "INV-AP-77"
        self.ap_charge.save()
        self.login(self.ar)
        data = self.client.get(reverse("search_suggest"), {"q": "INV-AP-77"}).json()
        self.assertFalse(any(g["label"] == "发票" for g in data["groups"]))


# --------------------------------------------------------------------------
# AI email reading — Gemini is replaced by a fake that returns fixed JSON
# --------------------------------------------------------------------------
def fake_gemini(result):
    calls = []

    def caller(email_text, context, today):
        calls.append((email_text, context, today))
        return result, {"totalTokenCount": 123}

    caller.calls = calls
    return caller


@override_settings(TMS_AI_ENABLED=True)
class AiEmailTests(Base):
    EMAIL = "CYMU2523606 picked up 1/5, arrived site 1/6 09:20 ET, unloaded. Driver Mike."

    def result(self, **extra):
        load = {"container_number": "CYMU2523606", "carrier_load_number": "BLN30784", "note": "Driver Mike unloaded fine",
                "updates": [
                    {"field": "actual_pickup", "value": "2026-01-05", "evidence": "picked up 1/5"},
                    {"field": "delivery_arrived_at", "value": "2026-01-06T09:20:00-05:00", "evidence": "arrived site 1/6 09:20 ET"},
                    {"field": "driver_name", "value": "Mike", "evidence": "Driver Mike"},
                ]}
        load.update(extra)
        return {"summary": "Picked up and arrived", "loads": [load], "unmatched": ""}

    def run_extract(self, result, user=None, **scope):
        from . import ai

        return ai.extract(self.EMAIL, user or self.op, caller=fake_gemini(result), **scope)

    def test_extract_matches_load_and_stores_pending_changes(self):
        ext = self.run_extract(self.result(), project=self.project)
        changes = {c.field: c for c in ext.changes.all()}
        self.assertEqual(set(changes), {"actual_pickup", "delivery_arrived_at", "driver_name", "note"})
        self.assertEqual(changes["actual_pickup"].shipment, self.s)
        self.assertEqual(changes["actual_pickup"].leg, self.leg)
        self.assertEqual(changes["delivery_arrived_at"].new_value, "2026-01-06T14:20Z")  # stored in UTC
        self.assertFalse(changes["delivery_arrived_at"].tz_assumed)
        # Nothing written yet.
        self.leg.refresh_from_db()
        self.assertIsNone(self.leg.actual_pickup)

    def test_apply_writes_with_history_and_status(self):
        from . import ai

        ext = self.run_extract(self.result(), project=self.project)
        ids = [c.pk for c in ext.changes.all()]
        applied, skipped = ai.apply_changes(ext, self.op, ids)
        self.assertEqual((len(applied), len(skipped)), (4, 0))
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.actual_pickup, date(2026, 1, 5))
        self.assertEqual(self.leg.driver_name, "Mike")
        self.assertEqual(self.leg.history.first().history_change_reason, f"AI 邮件识别 #{ext.pk}")
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, Status.IN_TRANSIT)
        texts = list(self.s.timeline_notes.values_list("text", flat=True))
        self.assertTrue(any("Driver Mike unloaded fine" in t for t in texts))
        self.assertTrue(any(t.startswith(f"通过 AI 邮件识别 #{ext.pk} 更新") for t in texts))

    def test_unticked_changes_are_rejected(self):
        from . import ai
        from .models import AiChange

        ext = self.run_extract(self.result(), project=self.project)
        keep = ext.changes.get(field="driver_name")
        ai.apply_changes(ext, self.op, [keep.pk])
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.driver_name, "Mike")
        self.assertIsNone(self.leg.actual_pickup)
        self.assertEqual(ext.changes.filter(status=AiChange.State.REJECTED).count(), 3)

    def test_stale_value_is_not_overwritten(self):
        from . import ai
        from .models import AiChange

        ext = self.run_extract(self.result(), project=self.project)
        self.leg.driver_name = "Someone else"
        self.leg.save()
        ai.apply_changes(ext, self.op, [c.pk for c in ext.changes.all()])
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.driver_name, "Someone else")
        self.assertEqual(ext.changes.get(field="driver_name").status, AiChange.State.STALE)
        self.assertEqual(self.leg.actual_pickup, date(2026, 1, 5))  # others still applied

    def test_locked_load_blocks_ops_fields_but_not_pod(self):
        from . import ai
        from .models import AiChange

        self.leg.actual_pickup, self.leg.actual_delivery = date(2026, 1, 5), date(2026, 1, 6)
        self.leg.save()
        self.s.refresh_from_db()
        self.assertTrue(self.s.is_locked)
        res = {"summary": "", "unmatched": "", "loads": [{"container_number": "CYMU2523606", "carrier_load_number": "", "note": "",
                "updates": [{"field": "pod_received", "value": "2026-01-09", "evidence": "POD attached"},
                            {"field": "driver_name", "value": "Mike", "evidence": "Driver Mike"}]}]}
        ext = self.run_extract(res, project=self.project)
        ai.apply_changes(ext, self.op, [c.pk for c in ext.changes.all()])
        self.s.refresh_from_db()
        self.assertEqual(self.s.pod_received, date(2026, 1, 9))
        self.assertEqual(ext.changes.get(field="driver_name").status, AiChange.State.BLOCKED)
        # Management may write to a locked Load.
        ext2 = self.run_extract(res, user=self.mgmt, project=self.project)
        ai.apply_changes(ext2, self.mgmt, [c.pk for c in ext2.changes.all()])
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.driver_name, "Mike")

    def test_unchanged_values_unknown_loads_and_bad_values(self):
        self.leg.driver_name = "Mike"
        self.leg.save()
        res = self.result()
        res["loads"][0]["updates"].append({"field": "actual_delivery", "value": "next week", "evidence": "x"})
        res["loads"].append({"container_number": "ZZZU0000000", "carrier_load_number": "", "note": "",
                             "updates": [{"field": "actual_pickup", "value": "2026-01-01", "evidence": "x"}]})
        ext = self.run_extract(res, project=self.project)
        fields = set(ext.changes.values_list("field", flat=True))
        self.assertNotIn("driver_name", fields)  # already correct
        self.assertNotIn("actual_delivery", fields)  # unparseable
        self.assertIn("找不到 ZZZU0000000", ext.unmatched)
        self.assertIn("无法识别", ext.unmatched)

    def test_match_by_carrier_load_number_and_naive_time(self):
        self.leg.carrier_load_number = "BLN30784"
        self.leg.save()
        res = self.result(container_number="")
        res["loads"][0]["updates"] = [{"field": "delivery_arrived_at", "value": "2026-01-06T09:20", "evidence": "09:20"}]
        ext = self.run_extract(res)  # global scope
        c = ext.changes.get(field="delivery_arrived_at")
        self.assertEqual(c.shipment, self.s)
        self.assertTrue(c.tz_assumed)
        self.assertEqual(c.new_value, "2026-01-06T17:20Z")  # 09:20 Pacific

    def test_money_and_unknown_fields_are_ignored(self):
        res = self.result()
        res["loads"][0]["updates"] = [{"field": "unit_price", "value": "9999", "evidence": "rate"}]
        ext = self.run_extract(res, project=self.project)
        self.assertEqual(list(ext.changes.exclude(field="note")), [])

    def test_permissions_and_limits(self):
        from . import ai

        for user in (self.ar, self.ap):
            with self.assertRaises(ai.AiError):
                self.run_extract(self.result(), user=user)
        with self.assertRaises(ai.AiError):
            ai.extract("", self.op, caller=fake_gemini(self.result()))
        with self.assertRaises(ai.AiError):
            ai.extract("x" * (ai.MAX_EMAIL_CHARS + 1), self.op, caller=fake_gemini(self.result()))
        with self.settings(TMS_AI_DAILY_LIMIT=1):
            self.run_extract(self.result())
            with self.assertRaises(ai.AiError):
                self.run_extract(self.result())
        self.login(self.ar)
        self.assertEqual(self.client.get(reverse("ai_new")).status_code, 403)

    def test_pages(self):
        from unittest import mock

        from . import ai

        self.login(self.op)
        r = self.client.get(reverse("ai_new") + f"?load={self.s.pk}")
        self.assertContains(r, "开始识别")
        with mock.patch.object(ai, "call_gemini", fake_gemini(self.result())):
            r = self.client.post(reverse("ai_new"), {"text": self.EMAIL, "load": self.s.pk})
        self.assertEqual(r.status_code, 302)
        ext_url = r["Location"]
        r = self.client.get(ext_url)
        self.assertContains(r, "实际提货")
        self.assertContains(r, "2026-01-06 06:20 PST")  # 09:20 EST shown in Pacific
        pk = int(ext_url.rstrip("/").split("/")[-1])
        ids = list(map(str, __import__("core.models", fromlist=["AiChange"]).AiChange.objects.filter(extraction_id=pk).values_list("pk", flat=True)))
        r = self.client.post(reverse("ai_apply", args=[pk]), {"action": "apply", "change": ids})
        self.assertEqual(r.status_code, 302)
        self.leg.refresh_from_db()
        self.assertEqual(self.leg.actual_pickup, date(2026, 1, 5))
        self.assertContains(self.client.get(reverse("ai_list")), f"#{pk}")
        # Discard on an already-processed extraction does nothing.
        r = self.client.post(reverse("ai_apply", args=[pk]), {"action": "discard"}, follow=True)
        self.assertContains(r, "已经处理过了")

    def test_disabled_shows_setup_note(self):
        self.login(self.op)
        with self.settings(TMS_AI_ENABLED=False):
            r = self.client.get(reverse("ai_new"))
        self.assertContains(r, "还没有开启")

    def test_health_alias(self):
        self.assertEqual(self.client.get("/health/").status_code, 200)


class AuditDisplayTests(TestCase):
    def test_datetimes_shown_in_local_time(self):
        from datetime import datetime, timezone as dt_tz

        from .audit import _display

        v = datetime(2026, 10, 28, 11, 45, tzinfo=dt_tz.utc)
        self.assertEqual(_display(Leg, "delivery_arrived_at", v), "2026-10-28 04:45 PDT")


NEW_PROJECT_RESULT = {
    "summary": "新项目 #4560 启动", "loads": [], "unmatched": "",
    "new_project": {
        "is_new": True, "name": "PTT#4560 环世 → Moreno Valley", "company": "PTT", "customer": "环世 Worldwide Logistics USA",
        "customer_ref": "", "monday_item_id": "P-04560", "start_date": "2026-10-28", "cargo_description": "电池柜",
        "notes": "DO 未放（客户待提供）",
        "shipper": {"code": "Port of LA", "name": "Port of Los Angeles", "address": "", "city": "Los Angeles", "state": "CA", "postal_code": ""},
        "consignee": {"code": "MVUSD Bus Charging", "name": "Moreno Valley Unified School District - Bus Charging Station",
                      "address": "13911 Perris Blvd", "city": "Moreno Valley", "state": "CA", "postal_code": "92553"},
        "milestones": [{"name": "船到港 ETA", "planned_date": "2026-11-08"}, {"name": "bad", "planned_date": "soon"}],
        "loads": [
            {"container_number": "", "equipment": "20GP", "service": "DRAY", "mbl": "", "carrier": "B E Jordan Trucking", "notes": "电池柜，DO 待放"},
            {"container_number": "TEXU 0009861", "equipment": "20FR", "service": "DRAY", "mbl": "shz8660350", "carrier": "", "notes": "HBL WLC61002517"},
            {"container_number": "CYMU2523606", "equipment": "XX", "service": "??", "mbl": "", "carrier": "", "notes": "已存在"},
        ],
    },
}


@override_settings(TMS_AI_ENABLED=True)
class AiNewProjectTests(Base):
    def extract(self):
        from . import ai

        return ai.extract("项目开始 #4560 …", self.op, caller=fake_gemini(NEW_PROJECT_RESULT))

    def form_from_proposal(self, prop):
        form = {"np_index": str(prop.get("index", 0)), "np_name": prop["name"], "np_customer": prop["customer"], "np_company": prop["company"],
                "np_monday_item_id": prop["monday_item_id"], "np_start_date": prop["start_date"],
                "np_cargo_description": prop["cargo_description"], "np_notes": prop["notes"], "np_milestones": "1",
                "np_load_count": str(len(prop["loads"]))}
        for role in ("shipper", "consignee"):
            for k in ("code", "name", "address", "city", "state", "postal_code"):
                form[f"np_{role}_{k}"] = prop[role][k]
        for i, ld in enumerate(prop["loads"]):
            for k in ("container_number", "equipment", "service", "mbl", "carrier", "notes"):
                form[f"np_load_{i}_{k}"] = ld[k]
            if not ld["exists"]:
                form[f"np_load_{i}_include"] = "1"
        return form

    def test_proposal_is_cleaned(self):
        from . import ai

        prop = ai.proposal(self.extract())
        self.assertEqual(prop["company"], "PTT")
        self.assertFalse(prop["customer_exists"])
        self.assertEqual(prop["loads"][1]["container_number"], "TEXU0009861")
        self.assertEqual(prop["loads"][1]["mbl"], "SHZ8660350")
        self.assertTrue(prop["loads"][2]["exists"])  # CYMU2523606 is already in TMS
        self.assertEqual(prop["loads"][2]["equipment"], "OTHER")  # unknown type kept as text in notes
        self.assertTrue(prop["loads"][2]["notes"].startswith("柜型：XX"))
        self.assertEqual(prop["milestones"], [{"name": "船到港 ETA", "planned_date": "2026-11-08"}])

    def test_review_page_and_create(self):
        from . import ai
        from .models import AiExtraction, Location, Milestone

        ext = self.extract()
        self.login(self.op)
        r = self.client.get(ext.get_absolute_url())
        self.assertContains(r, "核对后新建")
        self.assertContains(r, "系统里已有这个柜号")
        r = self.client.post(reverse("ai_create_project", args=[ext.pk]), self.form_from_proposal(ai.proposal(ext)))
        p = Project.objects.get(name="PTT#4560 环世 → Moreno Valley")
        self.assertRedirects(r, p.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(p.company, "PTT")
        self.assertEqual(p.customer.name, "环世 Worldwide Logistics USA")
        self.assertEqual(list(p.owners.all()), [self.op])
        self.assertEqual(p.default_consignee.address, "13911 Perris Blvd")
        self.assertEqual(Location.objects.get(code="MVUSD Bus Charging").kind, Location.Kind.SITE)
        loads = list(p.shipments.order_by("id"))
        self.assertEqual(len(loads), 2)  # the existing container was not re-created
        self.assertEqual(loads[1].container_number, "TEXU0009861")
        self.assertEqual(loads[1].equipment, "20FR")
        self.assertEqual(loads[1].master_bl.number, "SHZ8660350")
        self.assertEqual(loads[0].legs.get().carrier.name, "B E Jordan Trucking")
        self.assertEqual(loads[0].legs.get().kind, Leg.Kind.DRAYAGE)
        self.assertTrue(Milestone.objects.filter(project=p, name="船到港 ETA", planned_date=date(2026, 11, 8)).exists())
        ext.refresh_from_db()
        self.assertEqual(ext.status, AiExtraction.State.DONE)
        self.assertEqual(ext.raw_result["np_state"]["0"], {"status": "created", "project_id": p.pk})
        self.assertContains(self.client.get(ext.get_absolute_url()), "已根据这次识别新建项目")
        # A second submit does nothing.
        r = self.client.post(reverse("ai_create_project", args=[ext.pk]), self.form_from_proposal(ai.proposal(ext)), follow=True)
        self.assertContains(r, "已经处理过了")
        self.assertEqual(Project.objects.filter(name=p.name).count(), 1)

    def test_create_requires_name_and_rejects_duplicates(self):
        from . import ai

        ext = self.extract()
        form = self.form_from_proposal(ai.proposal(ext))
        with self.assertRaises(ai.AiError):
            ai.create_project(ext, self.op, {**form, "np_name": ""})
        with self.assertRaises(ai.AiError):
            ai.create_project(ext, self.op, {**form, "np_name": self.project.name, "np_customer": self.customer.name})
        with self.assertRaises(ai.AiError):
            ai.create_project(ext, self.ar, form)

    def test_own_company_is_never_the_customer(self):
        import copy

        from . import ai

        self.assertEqual(ai.own_company("PTT"), "PTT")
        self.assertEqual(ai.own_company("ptt specialized"), "PTT")
        self.assertEqual(ai.own_company("Advantage Transport Solution Inc"), "ATS")
        self.assertEqual(ai.own_company("PTTEP Co"), "")
        self.assertEqual(ai.own_company("环世 Worldwide Logistics USA"), "")
        res = copy.deepcopy(NEW_PROJECT_RESULT)
        res["new_project"].update(customer="PTT", company="")
        ext = ai.extract("x", self.op, caller=fake_gemini(res))
        prop = ai.proposal(ext)
        self.assertEqual((prop["customer"], prop["company"], prop["customer_was_own"]), ("", "PTT", True))
        self.login(self.op)
        self.assertContains(self.client.get(ext.get_absolute_url()), "AI 把我方公司当成了客户")
        form = self.form_from_proposal(prop)
        with self.assertRaises(ai.AiError):
            ai.create_project(ext, self.op, {**form, "np_customer": "PTT"})

    def test_several_projects_in_one_text(self):
        import copy

        from . import ai
        from .models import AiExtraction

        p1 = copy.deepcopy(NEW_PROJECT_RESULT["new_project"])
        p2 = copy.deepcopy(p1)
        p2.update(name="ATS#4600 Trina → Holyoke", company="ATS", customer="Trina", monday_item_id="P-04600")
        p2["loads"] = [{"container_number": "MSKU1234567", "equipment": "40HC", "service": "DRAY", "mbl": "", "carrier": "", "notes": ""}]
        p3 = copy.deepcopy(p2)
        p3.update(name="ATS#4601 Trina → Holyoke", monday_item_id="P-04601")
        p3["loads"][0]["container_number"] = "MSKU7654321"
        res = {"summary": "三个新项目", "loads": [], "unmatched": "", "new_projects": [p1, p2, p3]}
        ext = ai.extract("三个项目……", self.op, caller=fake_gemini(res))
        props = ai.proposals(ext)
        self.assertEqual([p["index"] for p in props], [0, 1, 2])
        self.login(self.op)
        r = self.client.get(ext.get_absolute_url())
        self.assertContains(r, "新项目 1/3")
        self.assertContains(r, "新项目 3/3")
        # Create the second, skip the third, the first is still open.
        r = self.client.post(reverse("ai_create_project", args=[ext.pk]), self.form_from_proposal(props[1]))
        self.assertRedirects(r, ext.get_absolute_url(), fetch_redirect_response=False)  # more waiting
        self.assertTrue(Project.objects.filter(name="ATS#4600 Trina → Holyoke", customer__name="Trina").exists())
        self.client.post(reverse("ai_create_project", args=[ext.pk]), {"np_index": "2", "action": "skip"})
        ext.refresh_from_db()
        self.assertEqual(ext.status, AiExtraction.State.PENDING)
        self.assertEqual([p["index"] for p in ai.open_proposals(ext)], [0])
        r = self.client.get(ext.get_absolute_url())
        self.assertNotContains(r, "新项目 2/3")
        self.assertContains(r, "ATS#4600 Trina → Holyoke")
        # Creating the same one twice is refused.
        with self.assertRaises(ai.AiError):
            ai.create_project(ext, self.op, self.form_from_proposal(props[1]))
        # Last one: the extraction is finished and we land on the new project.
        r = self.client.post(reverse("ai_create_project", args=[ext.pk]), self.form_from_proposal(props[0]))
        p = Project.objects.get(name=p1["name"])
        self.assertRedirects(r, p.get_absolute_url(), fetch_redirect_response=False)
        ext.refresh_from_db()
        self.assertEqual(ext.status, AiExtraction.State.DONE)
        self.assertFalse(Project.objects.filter(name="ATS#4601 Trina → Holyoke").exists())

    def test_later_batch_goes_to_existing_project(self):
        import copy

        from . import ai
        from .models import AiExtraction, MasterBL

        self.project.monday_item_id = "P-04560"
        self.project.save()
        batch = copy.deepcopy(NEW_PROJECT_RESULT["new_project"])
        batch.update(existing_project="", monday_item_id="#4560", name="PTT#4560 第二批")
        batch["loads"] = [
            {"container_number": "TGHU1111111", "equipment": "40HC", "service": "DRAY", "mbl": "shz999", "carrier": "B E Jordan Trucking", "notes": "第二批"},
            {"container_number": "CYMU2523606", "equipment": "40HC", "service": "DRAY", "mbl": "", "carrier": "", "notes": "已存在"},
        ]
        res = {"summary": "#4560 第二批", "loads": [], "unmatched": "", "new_projects": [batch]}
        ext = ai.extract("第二批……", self.op, caller=fake_gemini(res))
        prop = ai.proposal(ext)
        self.assertEqual(prop["target"], self.project.pk)  # matched by project code 4560
        self.login(self.op)
        r = self.client.get(ext.get_absolute_url())
        self.assertContains(r, "已有项目的新批次")
        form = {**self.form_from_proposal(prop), "np_target": str(self.project.pk), "np_load_1_include": "1"}
        before = Project.objects.count()
        r = self.client.post(reverse("ai_create_project", args=[ext.pk]), form)
        self.assertRedirects(r, self.project.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(Project.objects.count(), before)  # no duplicate project
        new = Shipment.objects.get(container_number="TGHU1111111")
        self.assertEqual((new.project, new.customer, new.consignee), (self.project, self.customer, self.site))
        self.assertEqual(new.master_bl.number, "SHZ999")
        self.assertEqual(new.master_bl.eta, date(2026, 11, 8))
        self.assertEqual(new.legs.get().carrier.name, "B E Jordan Trucking")
        self.assertEqual(Shipment.objects.filter(container_number="CYMU2523606").count(), 1)  # not duplicated
        self.assertTrue(self.project.timeline_notes.filter(text__contains="新增 1 个 Load").exists())
        ext.refresh_from_db()
        self.assertEqual(ext.status, AiExtraction.State.DONE)

    def test_scope_project_and_explicit_number(self):
        import copy

        from . import ai

        batch = copy.deepcopy(NEW_PROJECT_RESULT["new_project"])
        batch.update(monday_item_id="", name="whatever", existing_project="")
        res = {"summary": "", "loads": [], "unmatched": "", "new_projects": [batch]}
        ext = ai.extract("x", self.op, project=self.project, caller=fake_gemini(res))
        self.assertEqual(ai.proposal(ext)["target"], self.project.pk)  # entered from the project page
        batch["existing_project"] = self.project.number
        ext2 = ai.extract("x", self.op, caller=fake_gemini({**res, "new_projects": [batch]}))
        self.assertEqual(ai.proposal(ext2)["target"], self.project.pk)
        batch["existing_project"] = ""
        ext3 = ai.extract("x", self.op, caller=fake_gemini({**res, "new_projects": [batch]}))
        self.assertIsNone(ai.proposal(ext3)["target"])

    def test_works_with_empty_system(self):
        Shipment.all_objects.all().delete()
        ext = self.extract()
        self.assertIsNotNone(ext.pk)


@override_settings(TMS_AI_ENABLED=True)
class AiSheetUploadTests(Base):
    def test_xlsx_and_csv_become_text(self):
        import io
        from datetime import datetime as dt
        from unittest import mock

        from django.core.files.uploadedfile import SimpleUploadedFile
        from openpyxl import Workbook

        from . import ai

        wb = Workbook()
        ws = wb.active
        ws.title = "柜号"
        ws.append(["Container", "MBL", "ETA"])
        ws.append(["TEXU0009861", "SHZ8660350", dt(2026, 11, 8)])
        buf = io.BytesIO()
        wb.save(buf)
        text = ai.sheet_to_text(SimpleUploadedFile("list.xlsx", buf.getvalue()))
        self.assertIn("## 工作表：柜号", text)
        self.assertIn("TEXU0009861\tSHZ8660350\t2026-11-08", text)
        self.assertIn("柜号,MBL", ai.sheet_to_text(SimpleUploadedFile("a.csv", "柜号,MBL\nX,Y".encode("gb18030"))))
        with self.assertRaises(ai.AiError):
            ai.sheet_to_text(SimpleUploadedFile("a.pdf", b"%PDF"))

        captured = {}

        def caller(email_text, context, today):
            captured["text"] = email_text
            return {"summary": "", "loads": [], "unmatched": "", "new_project": {"is_new": False}}, {}

        self.login(self.op)
        with mock.patch.object(ai, "call_gemini", caller):
            r = self.client.post(reverse("ai_new"), {"text": "", "sheet": SimpleUploadedFile("list.xlsx", buf.getvalue())})
        self.assertEqual(r.status_code, 302)
        self.assertIn("附带表格：list.xlsx", captured["text"])


class EtaAndMilestoneAlertTests(Base):
    def setUp(self):
        super().setUp()
        from .models import MasterBL

        self.project.owners.add(self.op)
        today = date.today()
        self.mbl = MasterBL.objects.create(number="SHZ8660350", eta=today + timedelta(days=2))
        self.s.master_bl = self.mbl
        self.s.save()

    def keys(self, user=None):
        from . import alerts

        return {a.key: a for a in alerts.collect(user or self.op)}

    def test_eta_soon_and_overdue(self):
        self.s.refresh_from_db()
        self.assertEqual(self.s.status, Status.AWAITING)
        a = self.keys()[f"eta:{self.s.pk}"]
        self.assertIn("2 天后到港", a.title)
        self.assertIn("LFD", a.detail)
        self.mbl.eta = date.today() - timedelta(days=1)
        self.mbl.save()
        a = self.keys()[f"eta:{self.s.pk}"]
        self.assertEqual(a.level, "urgent")
        self.assertIn("已过 ETA", a.title)
        self.mbl.eta = date.today() + timedelta(days=10)
        self.mbl.save()
        self.assertNotIn(f"eta:{self.s.pk}", self.keys())

    def test_arrived_stops_eta_alert(self):
        self.mbl.ata = date.today()
        self.mbl.save()
        self.s.refresh_status()
        self.assertNotIn(f"eta:{self.s.pk}", self.keys())

    def test_milestone_due_and_done(self):
        from .models import Milestone

        m = Milestone.objects.create(project=self.project, name="船到港 ETA", planned_date=date.today() + timedelta(days=1))
        self.assertIn(f"milestone:{m.pk}", self.keys())
        m.done_date = date.today()
        m.save()
        self.assertNotIn(f"milestone:{m.pk}", self.keys())


@override_settings(TMS_AI_ENABLED=True)
class AiMblFieldTests(Base):
    def test_ata_updates_mbl_and_status(self):
        from . import ai
        from .models import MasterBL

        self.s.master_bl = MasterBL.objects.create(number="SHZ1", eta=date(2026, 11, 8))
        self.s.save()
        res = {"summary": "", "unmatched": "", "new_project": {"is_new": False}, "loads": [
            {"container_number": "CYMU2523606", "carrier_load_number": "", "note": "",
             "updates": [{"field": "ata", "value": "2026-11-09", "evidence": "vessel discharged 11/9"},
                         {"field": "eta", "value": "2026-11-09", "evidence": "ETA revised"}]}]}
        ext = ai.extract("x", self.op, project=self.project, caller=fake_gemini(res))
        self.assertEqual(set(ext.changes.values_list("field", flat=True)), {"ata", "eta"})
        ai.apply_changes(ext, self.op, list(ext.changes.values_list("pk", flat=True)))
        self.s.refresh_from_db()
        self.assertEqual(self.s.master_bl.ata, date(2026, 11, 9))
        self.assertEqual(self.s.status, Status.ARRIVED)

    def test_no_mbl_is_reported(self):
        from . import ai

        res = {"summary": "", "unmatched": "", "new_project": {"is_new": False}, "loads": [
            {"container_number": "CYMU2523606", "carrier_load_number": "", "note": "",
             "updates": [{"field": "eta", "value": "2026-11-09", "evidence": "ETA"}]}]}
        ext = ai.extract("x", self.op, project=self.project, caller=fake_gemini(res))
        self.assertFalse(ext.changes.exists())
        self.assertIn("还没有MBL", ext.unmatched)

    def test_new_project_eta_goes_onto_mbl(self):
        from . import ai

        ext = ai.extract("x", self.op, caller=fake_gemini(NEW_PROJECT_RESULT))
        form = AiNewProjectTests.form_from_proposal(None, ai.proposal(ext))
        p = ai.create_project(ext, self.op, form)
        s = p.shipments.get(container_number="TEXU0009861")
        self.assertEqual(s.master_bl.eta, date(2026, 11, 8))
        self.assertEqual(s.status, Status.AWAITING)



@override_settings(TMS_AI_ENABLED=True, TMS_TASK_KEY="k-1")
class MailScanTests(Base):
    def setUp(self):
        super().setUp()
        from . import mailscan
        from .models import GmailConnection, MasterBL

        self.project.owners.add(self.op)
        self.s.master_bl = MasterBL.objects.create(number="SHZ8660350")
        self.s.save()
        self.leg.carrier_load_number = "BLN30784"
        self.leg.save()
        self.conn = GmailConnection.objects.create(user=self.op, email=self.op.email, refresh_token=mailscan.encrypt("r-tok"))
        self.inbox = {
            "m1": {"id": "m1", "subject": "CYMU2523606 delivered", "from": "a@carrier.com", "date": "Wed, 28 Oct 2026 10:00:00 -0700",
                   "text": "CYMU2523606 delivered 10/28, POD attached"},
            "m2": {"id": "m2", "subject": "Re: BLN30784", "from": "b@carrier.com", "date": "Wed, 28 Oct 2026 11:00:00 -0700",
                   "text": "Driver Mike on BLN30784"},
            "m3": {"id": "m3", "subject": "lunch", "from": "c@x.com", "date": "", "text": "nothing relevant ABCDEF1"},
        }
        self.calls = []

    def fake_get(self, token, path, params=None):
        if path == "/messages":
            return {"messages": [{"id": k} for k in self.inbox]}
        raise AssertionError(path)

    def run_scan(self, result=None):
        from unittest import mock

        from . import mailscan

        res = result or {"summary": "已送达", "unmatched": "", "new_project": {"is_new": False}, "loads": [
            {"container_number": "CYMU2523606", "carrier_load_number": "", "note": "",
             "updates": [{"field": "actual_delivery", "value": "2026-10-28", "evidence": "delivered 10/28"}]}]}

        def caller(text, context, today):
            self.calls.append(text)
            return res, {}

        with mock.patch.object(mailscan, "_access_token", return_value="a-tok"), \
             mock.patch.object(mailscan, "_get", side_effect=self.fake_get), \
             mock.patch.object(mailscan, "read_message", side_effect=lambda tok, mid: self.inbox[mid]):
            return mailscan.scan_one(self.conn, caller=caller)

    def test_identifiers_and_query(self):
        from datetime import datetime, timezone as dt_tz

        from . import mailscan

        ids = mailscan.identifiers(mailscan.open_loads())
        self.assertEqual(set(ids), {"CYMU2523606", "SHZ8660350", "BLN30784"})
        q = list(mailscan._queries(ids, datetime(2026, 10, 28, tzinfo=dt_tz.utc)))
        self.assertEqual(len(q), 1)
        self.assertIn('"CYMU2523606" OR', q[0])
        self.assertTrue(q[0].startswith("after:"))

    def test_scan_groups_by_project_and_marks_messages(self):
        from .models import AiChange, AiExtraction, ScannedMessage

        res = self.run_scan()
        self.assertIn("2 封相关邮件", res["result"])
        self.assertEqual(len(self.calls), 1)  # both relevant e-mails in ONE AI call for the project
        self.assertIn("CYMU2523606 delivered", self.calls[0])
        self.assertIn("Driver Mike", self.calls[0])
        ext = AiExtraction.objects.get(source=AiExtraction.Source.GMAIL)
        self.assertEqual((ext.project, ext.created_by, ext.status), (self.project, self.op, AiExtraction.State.PENDING))
        self.assertEqual(AiChange.objects.filter(extraction=ext).count(), 1)
        self.assertEqual(set(ScannedMessage.objects.values_list("message_id", flat=True)), {"m1", "m2", "m3"})
        self.conn.refresh_from_db()
        self.assertIsNotNone(self.conn.last_scan_at)
        # Nothing is written before a person confirms.
        self.leg.refresh_from_db()
        self.assertIsNone(self.leg.actual_delivery)
        # Shows up in the owner's to-do list.
        from . import alerts

        self.assertIn(f"ai_pending:{ext.pk}", {a.key for a in alerts.collect(self.op)})
        # A second run reads nothing again.
        self.calls.clear()
        self.run_scan()
        self.assertEqual(self.calls, [])

    def test_no_news_is_not_a_todo(self):
        from .models import AiExtraction

        self.leg.actual_delivery = date(2026, 10, 28)  # already correct
        self.leg.save()
        self.run_scan({"summary": "", "unmatched": "", "new_project": {"is_new": False}, "loads": [
            {"container_number": "CYMU2523606", "carrier_load_number": "", "note": "",
             "updates": [{"field": "actual_delivery", "value": "2026-10-28", "evidence": "x"}]}]})
        ext = AiExtraction.objects.get(source=AiExtraction.Source.GMAIL)
        self.assertEqual(ext.status, AiExtraction.State.DONE)

    def test_ai_failure_keeps_window_and_retries(self):
        from unittest import mock

        from . import ai
        from .models import ScannedMessage

        with mock.patch.object(ai, "extract", side_effect=ai.AiError("Gemini 返回错误 500")):
            res = self.run_scan()
        self.assertIn("Gemini 返回错误 500", res["result"])
        self.conn.refresh_from_db()
        self.assertIsNone(self.conn.last_scan_at)  # same window next time
        self.assertEqual(set(ScannedMessage.objects.values_list("message_id", flat=True)), {"m3"})

    def test_access_errors_are_recorded(self):
        from unittest import mock

        from . import mailscan

        with mock.patch.object(mailscan, "_access_token", side_effect=mailscan.ScanError("邮箱授权已失效，请重新连接邮箱")):
            res = mailscan.scan_one(self.conn)
        self.assertIn("授权已失效", res["error"])
        self.conn.refresh_from_db()
        self.assertIn("授权已失效", self.conn.last_error)

    def test_connect_only_own_mailbox(self):
        from unittest import mock

        from django.core import signing
        from django.test import RequestFactory

        from . import mailscan
        from .models import GmailConnection

        self.conn.delete()
        req = RequestFactory().get("/mail/callback/")
        state = signing.dumps({"u": self.op.pk}, salt="gmail-connect")
        tok = mock.Mock(status_code=200, json=lambda: {"access_token": "a", "refresh_token": "r2", "scope": mailscan.SCOPE})
        with self.settings(SOCIALACCOUNT_PROVIDERS={"google": {"APP": {"client_id": "cid", "secret": "sec"}}}), \
             mock.patch.object(mailscan.requests, "post", return_value=tok):
            with mock.patch.object(mailscan, "_get", return_value={"emailAddress": "someone@else.com"}):
                with self.assertRaises(mailscan.ScanError):
                    mailscan.finish_connect(req, self.op, "code", state)
            with mock.patch.object(mailscan, "_get", return_value={"emailAddress": self.op.email}):
                conn = mailscan.finish_connect(req, self.op, "code", state)
            with self.assertRaises(mailscan.ScanError):
                mailscan.finish_connect(req, self.mgmt, "code", state)  # state belongs to another user
        self.assertEqual(GmailConnection.objects.get().pk, conn.pk)
        self.assertNotEqual(conn.refresh_token, "r2")  # stored encrypted
        self.assertEqual(mailscan.decrypt(conn.refresh_token), "r2")

    def test_pages_and_task_endpoint(self):
        from unittest import mock

        from . import mailscan

        self.login(self.op)
        r = self.client.get(reverse("mail_settings"))
        self.assertContains(r, self.op.email)
        self.assertContains(r, "现在扫描一次")
        self.login(self.ar)
        self.assertEqual(self.client.get(reverse("mail_settings")).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(reverse("task_scan_email")).status_code, 403)
        with mock.patch.object(mailscan, "scan_all", return_value=[{"mailbox": "x", "result": "ok"}]) as m:
            r = self.client.post(reverse("task_scan_email"), HTTP_X_TMS_TASK_KEY="k-1")
        self.assertEqual(r.status_code, 200)
        m.assert_called_once()
        with mock.patch.object(mailscan.requests, "post"):
            self.login(self.op)
            self.client.post(reverse("mail_disconnect"))
        from .models import GmailConnection

        self.assertFalse(GmailConnection.objects.exists())


@override_settings(TMS_AI_ENABLED=True)
class AiTemplateButtonTests(Base):
    def test_template_button(self):
        self.login(self.op)
        r = self.client.get(reverse("ai_new"))
        self.assertContains(r, "插入新建项目模板")
        self.assertContains(r, "客户（委托方，不要写 PTT/ATS）")
        self.assertContains(r, "<title>AI 识别邮件 · TMS</title>", html=False)


@override_settings(TMS_AI_ENABLED=True)
class AiFillInTests(Base):
    def res(self, loads=(), project_updates=()):
        return {"summary": "补全", "unmatched": "", "new_projects": [], "loads": list(loads), "project_updates": list(project_updates)}

    def test_placeholder_load_gets_container_mbl_carrier_and_type(self):
        from . import ai

        ph = Shipment.objects.create(customer=self.customer, project=self.project, consignee=self.site)  # no container, no leg
        item = {"load_number": ph.number, "container_number": "", "carrier_load_number": "", "note": "",
                "updates": [{"field": "container_number", "value": "TGHU 1234567", "evidence": "cntr TGHU1234567"},
                            {"field": "mbl_number", "value": "shz777", "evidence": "MBL SHZ777"},
                            {"field": "equipment", "value": "40' HC", "evidence": "40HC"},
                            {"field": "carrier", "value": "B E Jordan Trucking", "evidence": "BE Jordan will pull"}]}
        ext = ai.extract("x", self.op, project=self.project, caller=fake_gemini(self.res([item])))
        self.assertEqual(ext.changes.count(), 4)
        applied, skipped = ai.apply_changes(ext, self.op, list(ext.changes.values_list("pk", flat=True)))
        self.assertEqual((len(applied), len(skipped)), (4, 0))
        ph.refresh_from_db()
        self.assertEqual((ph.container_number, ph.equipment, ph.master_bl.number), ("TGHU1234567", "40HC", "SHZ777"))
        self.assertEqual(ph.legs.get().carrier.name, "B E Jordan Trucking")

    def test_duplicate_container_is_blocked(self):
        from . import ai
        from .models import AiChange

        ph = Shipment.objects.create(customer=self.customer, project=self.project)
        item = {"load_number": ph.number, "container_number": "", "carrier_load_number": "", "note": "",
                "updates": [{"field": "container_number", "value": "CYMU2523606", "evidence": "x"}]}
        ext = ai.extract("x", self.op, project=self.project, caller=fake_gemini(self.res([item])))
        ai.apply_changes(ext, self.op, list(ext.changes.values_list("pk", flat=True)))
        self.assertEqual(ext.changes.get().status, AiChange.State.BLOCKED)

    def test_project_level_fill_in(self):
        from . import ai

        site = Location.objects.create(code="Shakopee", city="Shakopee", state="MN")
        self.project.default_consignee = site
        self.project.monday_item_id = "P-04915"
        self.project.save()
        ups = [{"project": "P-04915", "field": "consignee_address", "value": "1000 Blue Lake Rd", "evidence": "deliver to 1000 Blue Lake Rd"},
               {"project": "#4915", "field": "consignee_postal_code", "value": "55379", "evidence": "55379"},
               {"project": "P-04915", "field": "start_date", "value": "2026-11-20", "evidence": "start 11/20"},
               {"project": "P-09999", "field": "start_date", "value": "2026-11-20", "evidence": "?"}]
        ext = ai.extract("x", self.op, caller=fake_gemini(self.res(project_updates=ups)))
        self.assertEqual(ext.changes.filter(project=self.project).count(), 3)
        self.assertIn("找不到项目 P-09999", ext.unmatched)
        self.login(self.op)
        self.assertContains(self.client.get(ext.get_absolute_url()), "项目信息补全")
        ai.apply_changes(ext, self.op, list(ext.changes.values_list("pk", flat=True)))
        site.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual((site.address, site.postal_code), ("1000 Blue Lake Rd", "55379"))
        self.assertEqual(self.project.start_date, date(2026, 11, 20))
        self.assertTrue(self.project.timeline_notes.filter(text__contains="补全").exists())

    def test_project_scope_without_loads_is_allowed(self):
        from . import ai

        empty = Project.objects.create(customer=self.customer, name="Empty")
        ext = ai.extract("x", self.op, project=empty, caller=fake_gemini(self.res()))
        self.assertIsNotNone(ext.pk)

    def test_nothing_found_is_closed_and_stuck_ones_can_be_closed(self):
        from . import ai
        from .models import AiExtraction

        ext = ai.extract("x", self.op, caller=fake_gemini(self.res()))
        self.assertEqual(ext.status, AiExtraction.State.DONE)
        stuck = AiExtraction.objects.create(created_by=self.op, source_text="old", raw_result={"new_project": {"is_new": False}})
        self.login(self.op)
        r = self.client.get(stuck.get_absolute_url())
        self.assertContains(r, "关闭这次识别")
        self.client.post(reverse("ai_apply", args=[stuck.pk]), {"action": "discard"})
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, AiExtraction.State.DISCARDED)

    def test_clean_street_and_suggestion_lists(self):
        from . import ai

        self.assertEqual(ai.clean_street("43851 Division ST Lancaster CA 93535", "LANCASTER", "CA", "93535"), "43851 Division ST")
        self.assertEqual(ai.clean_street("504 Wally Kay Way", "MOAPA", "NV", "89166"), "504 Wally Kay Way")
        ext = ai.extract("x", self.op, caller=fake_gemini(NEW_PROJECT_RESULT))
        self.login(self.op)
        r = self.client.get(ext.get_absolute_url())
        self.assertContains(r, 'list="loc-codes"')
        self.assertContains(r, '<datalist id="customer-names">')
        self.assertContains(r, 'id="loc-data"')


class MailScanProjectTermTests(Base):
    def test_project_code_and_customer_word(self):
        from datetime import datetime, timezone as dt_tz

        from . import mailscan

        self.project.monday_item_id = "P-04915"
        self.project.save()
        terms = mailscan.project_terms(mailscan.open_projects())
        self.assertEqual(terms[self.project.pk], ("4915", "TRINA"))
        q = list(mailscan._queries({}, datetime(2026, 10, 28, tzinfo=dt_tz.utc), terms))[0]
        self.assertIn('("4915" "TRINA")', q)


@override_settings(TMS_AI_ENABLED=True)
class MailFindTests(Base):
    def setUp(self):
        super().setUp()
        import base64 as b64
        import io

        from openpyxl import Workbook

        from . import mailscan
        from .models import GmailConnection

        self.conn = GmailConnection.objects.create(user=self.op, email=self.op.email, refresh_token=mailscan.encrypt("r"))
        wb = Workbook()
        wb.active.append(["Container", "MBL"])
        wb.active.append(["TGHU7777777", "SHZ555"])
        buf = io.BytesIO()
        wb.save(buf)
        self.xlsx = b64.urlsafe_b64encode(buf.getvalue()).decode()
        body = b64.urlsafe_b64encode("项目开始 #4915，柜号见附件".encode()).decode()
        self.msg = {"payload": {
            "headers": [{"name": "Subject", "value": "项目开始 #4915 Sungrow"}, {"name": "From", "value": "Jack <jack@ptt.com>"},
                        {"name": "Date", "value": "Mon, 20 Oct 2026 10:00:00 -0700"}],
            "parts": [{"mimeType": "text/plain", "body": {"data": body}},
                      {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "filename": "柜号.xlsx",
                       "body": {"attachmentId": "att1", "size": 5000}},
                      {"mimeType": "application/pdf", "filename": "BOL.pdf", "body": {"attachmentId": "att2", "size": 9000}},
                      {"mimeType": "application/zip", "filename": "photos.zip", "body": {"attachmentId": "att3", "size": 9000}}]}}
        self.pdf = b"%PDF-1.4 fake POD signed 10/21 by J. Smith"
        self.queries = []

    def fake_get(self, token, path, params=None):
        if path == "/messages":
            self.queries.append(params["q"])
            return {"messages": [{"id": "m9"}]}
        if path == "/messages/m9":
            return self.msg
        if path == "/messages/m9/attachments/att1":
            return {"data": self.xlsx}
        if path == "/messages/m9/attachments/att2":
            import base64 as b64
            return {"data": b64.urlsafe_b64encode(self.pdf).decode()}
        raise AssertionError(path)

    def test_query_building(self):
        from . import mailscan

        self.assertEqual(mailscan.gmail_query('项目开始 "#4915"'), 'subject:"项目开始  #4915 "')
        self.assertEqual(mailscan.gmail_query("from:a@b.com subject:4915"), "from:a@b.com subject:4915")
        with self.assertRaises(mailscan.ScanError):
            mailscan.gmail_query("  ")

    def test_find_reads_attachments_and_extracts(self):
        from unittest import mock

        from . import mailscan
        from .models import AiExtraction, ScannedMessage

        seen = {}

        def caller(text, context, today, attachments=None):
            seen["text"], seen["docs"] = text, attachments
            return {"summary": "新项目 #4915", "loads": [], "unmatched": "", "project_updates": [], "new_projects": [NEW_PROJECT_RESULT["new_project"]]}, {}

        with mock.patch.object(mailscan, "_access_token", return_value="t"), mock.patch.object(mailscan, "_get", side_effect=self.fake_get):
            ext, n = mailscan.find_and_extract(self.conn, "项目开始 #4915", caller=caller)
        self.assertEqual(n, 1)
        self.assertIn('subject:"项目开始 #4915"', self.queries[0])
        self.assertIn("--- 附件：柜号.xlsx ---", seen["text"])
        self.assertIn("TGHU7777777\tSHZ555", seen["text"])
        self.assertIn("未读取的附件：photos.zip", seen["text"])
        self.assertIn("AI 直接阅读：BOL.pdf", seen["text"])
        self.assertEqual(seen["docs"], [{"name": "BOL.pdf", "mime": "application/pdf", "data": self.pdf}])
        self.assertEqual(ext.raw_result["read_attachments"], ["BOL.pdf"])
        self.assertEqual(ext.source, AiExtraction.Source.GMAIL)
        self.assertEqual(len(ext.raw_result["new_projects"]), 1)
        self.assertTrue(ScannedMessage.objects.filter(message_id="m9", extraction=ext).exists())

    def test_find_view(self):
        from unittest import mock

        from . import mailscan

        self.login(self.op)
        self.assertContains(self.client.get(reverse("mail_settings")), "按邮件主题识别")
        with mock.patch.object(mailscan, "find_and_extract", side_effect=mailscan.ScanError("邮箱里没找到主题包含「x」的邮件")):
            r = self.client.post(reverse("mail_find"), {"q": "x"}, follow=True)
        self.assertContains(r, "没找到")


class UnitTests(TestCase):
    def test_length_parsing(self):
        from .units import parse_length_ft as L

        for text, ft in [("40", "40.00"), ("13'6\"", "13.50"), ("13' 6", "13.50"), ("13'-6\"", "13.50"), ("13 ft 6 in", "13.50"),
                         ("162 in", "13.50"), ("162\"", "13.50"), ("12.19 m", "39.99"), ("410 cm", "13.45"), ("4100mm", "13.45"),
                         ("8.5 ft", "8.50"), ("1,200 in", "100.00")]:
            self.assertEqual(L(text), Decimal(ft), text)
        self.assertIsNone(L(""))
        for bad in ("abc", "13 furlongs", "13'14\""):
            with self.assertRaises(ValueError):
                L(bad)

    def test_weight_parsing(self):
        from .units import parse_weight_lbs as W

        for text, lbs in [("44000", "44000.0"), ("44,000 lbs", "44000.0"), ("35,000 kg", "77161.8"), ("20 t", "44092.5"),
                          ("20 MT", "44092.5"), ("10 tons", "22046.2"), ("10 short tons", "22046.2"), ("20吨", "44092.5"), ("1000 公斤", "2204.6")]:
            self.assertEqual(W(text), Decimal(lbs), text)
        with self.assertRaises(ValueError):
            W("20 stone-ish")


class CargoFormUnitsTests(Base):
    def test_cargo_form_accepts_any_unit(self):
        self.login(self.op)
        r = self.client.post(reverse("child_create", args=[self.s.pk, "cargo"]), {
            "handling_qty": "1", "handling_type": "UNIT", "description": "Transformer", "weight_lbs": "35,000 kg",
            "length_ft": "12.19 m", "width_ft": "102 in", "height_ft": "13'6\""})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        line = CargoLine.objects.get(shipment=self.s, description="Transformer")
        self.assertEqual((line.weight_lbs, line.length_ft, line.width_ft, line.height_ft),
                         (Decimal("77161.8"), Decimal("39.99"), Decimal("8.50"), Decimal("13.50")))
        r = self.client.get(reverse("child_edit", args=["cargo", line.pk]))
        self.assertContains(r, 'value="13.5"')

    def test_cargo_form_rejects_unknown_unit(self):
        self.login(self.op)
        r = self.client.post(reverse("child_create", args=[self.s.pk, "cargo"]), {
            "handling_qty": "1", "handling_type": "UNIT", "description": "X", "length_ft": "3 cubits"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "不认识的长度单位")


@override_settings(TMS_AI_ENABLED=True)
class AiCargoTests(Base):
    CARGO = [{"description": "Transformer", "qty": "1", "handling_type": "UNIT", "weight": "35,000 kg", "weight_is_each": "false",
              "length": "12.19 m", "width": "2.44 m", "height": "2.9 m"},
             {"description": "Radiator", "qty": "2", "handling_type": "CRATE", "weight": "4,000 lbs", "weight_is_each": "true",
              "length": "10'", "width": "6'6\"", "height": "", }]

    def test_new_project_cargo_converted_and_created(self):
        import copy
        from . import ai

        res = copy.deepcopy(NEW_PROJECT_RESULT)
        res["new_project"]["loads"][1]["cargo"] = self.CARGO
        ext = ai.extract("项目开始", self.op, caller=fake_gemini(res))
        prop = ai.proposal(ext)
        cargo = prop["loads"][1]["cargo"]
        self.assertEqual((cargo[0]["weight_lbs"], cargo[0]["length_ft"], cargo[0]["height_ft"]), ("77161.8", "39.99", "9.51"))
        self.assertEqual(cargo[1]["weight_lbs"], "8000")  # 2 × 4,000 lbs each
        self.assertEqual(cargo[0]["original"], "35,000 kg；12.19 m x 2.44 m x 2.9 m")
        self.login(self.op)
        self.assertContains(self.client.get(ext.get_absolute_url()), "原文：35,000 kg")
        form = AiNewProjectTests.form_from_proposal(None, prop)
        for j, c in enumerate(cargo):
            for k, v in (("description", c["description"]), ("qty", c["qty"]), ("type", c["handling_type"]),
                         ("weight", c["weight_lbs"]), ("length", c["length_ft"]), ("width", c["width_ft"]), ("height", c["height_ft"])):
                form[f"np_load_1_cargo_{j}_{k}"] = v
        form["np_load_1_cargo_count"] = "2"
        form["np_load_1_cargo_1_height"] = "5 ft 3 in"  # edited on the form, any unit
        self.client.post(reverse("ai_create_project", args=[ext.pk]), form)
        s = Shipment.objects.get(container_number="TEXU0009861")
        lines = {c.description: c for c in s.cargo_lines.all()}
        self.assertEqual(lines["Transformer"].weight_lbs, Decimal("77161.8"))
        self.assertEqual(lines["Transformer"].length_ft, Decimal("39.99"))
        self.assertEqual((lines["Radiator"].handling_qty, lines["Radiator"].handling_type, lines["Radiator"].height_ft),
                         (2, "CRATE", Decimal("5.25")))

    def test_existing_load_gets_cargo_after_confirm(self):
        from . import ai

        res = {"summary": "dims", "unmatched": "", "loads": [
            {"container_number": "CYMU2523606", "carrier_load_number": "", "note": "", "updates": [], "cargo": self.CARGO[:1]}]}
        ext = ai.extract("dims", self.op, project=self.project, caller=fake_gemini(res))
        c = ext.changes.get(field="cargo_line")
        self.assertIn("39.99 x 8.01 x 9.51 ft", c.new_display)
        self.assertIn("77,161.8 lbs", c.new_display)
        ai.apply_changes(ext, self.op, [c.pk])
        line = self.s.cargo_lines.get()
        self.assertEqual((line.description, line.width_ft), ("Transformer", Decimal("8.01")))
        # A second email does not add duplicates on top of existing lines.
        ext2 = ai.extract("dims", self.op, project=self.project, caller=fake_gemini(res))
        self.assertFalse(ext2.changes.filter(field="cargo_line").exists())
        self.assertIn("已有货物明细", ext2.unmatched)


@override_settings(TMS_AI_ENABLED=True)
class AiDocumentTests(Base):
    def test_upload_pdf_and_photo_on_paste_page(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from . import ai
        from .models import AiExtraction

        seen = {}

        def caller(text, context, today, attachments=None):
            seen["text"], seen["docs"] = text, attachments
            return {"summary": "POD", "unmatched": "", "loads": [{"container_number": "CYMU2523606", "carrier_load_number": "",
                    "note": "签收人 J. Smith", "updates": [{"field": "pod_received", "value": "2026-10-21",
                                                         "evidence": "附件 POD.pdf：Received 10/21"}]}]}, {}

        self.login(self.op)
        with mock.patch.object(ai, "call_gemini", side_effect=caller):
            r = self.client.post(reverse("ai_new") + f"?project={self.project.pk}", {
                "text": "", "project": self.project.pk,
                "files": [SimpleUploadedFile("POD.pdf", b"%PDF-1.4 x", content_type="application/pdf"),
                          SimpleUploadedFile("site.jpg", b"\xff\xd8jpeg", content_type="image/jpeg")]})
        ext = AiExtraction.objects.latest("pk")
        self.assertRedirects(r, ext.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual([d["name"] for d in seen["docs"]], ["POD.pdf", "site.jpg"])
        self.assertEqual(seen["docs"][1]["mime"], "image/jpeg")
        self.assertIn("只有附件", seen["text"])
        self.assertEqual(ext.changes.get(field="pod_received").new_value, "2026-10-21")
        page = self.client.get(ext.get_absolute_url())
        self.assertContains(page, "AI 读取的附件")
        self.assertContains(page, "POD.pdf、site.jpg")

    def test_too_many_or_too_big_docs_are_listed_not_sent(self):
        from . import ai

        big = {"name": "huge.pdf", "mime": "application/pdf", "data": b"x" * (ai.MAX_DOC_BYTES + 1)}
        small = [{"name": f"p{i}.pdf", "mime": "application/pdf", "data": b"x"} for i in range(ai.MAX_DOCS + 1)]
        kept, skipped = ai.limit_docs([big] + small)
        self.assertEqual(len(kept), ai.MAX_DOCS)
        self.assertEqual(skipped, ["huge.pdf", f"p{ai.MAX_DOCS}.pdf"])
        seen = {}

        def caller(text, context, today, attachments=None):
            seen["n"] = len(attachments)
            return {"summary": "", "loads": [], "unmatched": ""}, {}

        ext = ai.extract("", self.op, caller=caller, attachments=[big] + small)
        self.assertEqual(seen["n"], ai.MAX_DOCS)
        self.assertIn("huge.pdf", ext.unmatched)

    def test_gemini_request_carries_inline_documents(self):
        from . import ai

        sent = {}

        class Resp:
            status_code = 200

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": '{"summary": "", "loads": [], "unmatched": ""}'}]}}]}

        creds = mock.Mock(token="tok")
        with mock.patch("google.auth.default", return_value=(creds, "atsstms")), \
             mock.patch("requests.post", side_effect=lambda url, json, headers, timeout: sent.update(body=json) or Resp()):
            ai.call_gemini("email", "ctx", date(2026, 10, 29), attachments=[{"name": "POD.pdf", "mime": "application/pdf", "data": b"%PDF"}])
        parts = sent["body"]["contents"][0]["parts"]
        self.assertEqual(parts[1], {"text": "=== 附件：POD.pdf ==="})
        self.assertEqual(parts[2], {"inlineData": {"mimeType": "application/pdf", "data": "JVBERg=="}})
