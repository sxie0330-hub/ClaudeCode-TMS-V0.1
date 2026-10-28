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
        form = {"np_name": prop["name"], "np_customer": prop["customer"], "np_company": prop["company"],
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
        self.assertContains(r, "这封邮件是新项目")
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
        self.assertEqual(ext.raw_result["created_project_id"], p.pk)
        self.assertContains(self.client.get(ext.get_absolute_url()), "已根据这封邮件新建项目")
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
