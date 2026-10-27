from datetime import date, timedelta
from decimal import Decimal

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
