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


class ImportUploadTests(Base):
    def test_only_management(self):
        self.login(self.op)
        self.assertEqual(self.client.get(reverse("import_trina")).status_code, 403)
        self.login(self.mgmt)
        self.assertEqual(self.client.get(reverse("import_trina")).status_code, 200)

    def test_rejects_non_xlsx(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.login(self.mgmt)
        r = self.client.post(reverse("import_trina"), {"file": SimpleUploadedFile("x.csv", b"a,b")})
        self.assertEqual(r.status_code, 302)
