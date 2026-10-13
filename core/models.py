"""Data model.

Hierarchy: Customer -> Project (optional) -> Shipment (one container, or one
truckload of non-container freight) -> legs / cargo lines / units / yard stays
/ charges / notes.

Every model keeps full change history (django-simple-history) and is soft
deleted, so nothing typed into the system is ever silently lost.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from simple_history.models import HistoricalRecords


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------
class ActiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class SoftDeleteModel(models.Model):
    is_deleted = models.BooleanField("已删除", default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ActiveManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def soft_delete(self):
        self.is_deleted = True
        self.save(update_fields=["is_deleted", "updated_at"])


class Sequence(models.Model):
    """Gap-tolerant counters for human-readable numbers (SHP-2610-0001)."""

    key = models.CharField(max_length=32, unique=True)
    value = models.PositiveIntegerField(default=0)

    @classmethod
    def next(cls, key):
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(key=key)
            seq.value += 1
            seq.save(update_fields=["value"])
            return seq.value


# --------------------------------------------------------------------------
# Users / roles
# --------------------------------------------------------------------------
class Role(models.TextChoices):
    MANAGEMENT = "MGMT", "管理层"
    OP = "OP", "操作 OP"
    AP = "AP", "应付 AP"
    AR = "AR", "应收 AR"


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField("角色", max_length=4, choices=Role.choices, blank=True)
    phone = models.CharField("电话", max_length=40, blank=True)

    history = HistoricalRecords()

    def __str__(self):
        return f"{self.user} ({self.get_role_display() or '未分配'})"


# --------------------------------------------------------------------------
# Master data
# --------------------------------------------------------------------------
class Customer(SoftDeleteModel):
    name = models.CharField("客户名称", max_length=120, unique=True)
    code = models.CharField("简称", max_length=30, blank=True)
    contact_name = models.CharField("联系人", max_length=120, blank=True)
    contact_phone = models.CharField("电话", max_length=60, blank=True)
    contact_email = models.EmailField("邮箱", blank=True)
    notes = models.TextField("备注", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("customer_detail", args=[self.pk])


class Carrier(SoftDeleteModel):
    class Kind(models.TextChoices):
        DRAYAGE = "DRAYAGE", "拖柜 Drayage"
        OTR = "OTR", "公路 OTR/FTL"
        HEAVY = "HEAVY", "重载/超限 Heavy Haul"
        LTL = "LTL", "零担 LTL"
        BROKER = "BROKER", "经纪 Broker"
        OCEAN = "OCEAN", "船公司 SSL"
        WAREHOUSE = "WHS", "仓库/堆场"
        OTHER = "OTHER", "其他"

    name = models.CharField("承运商名称", max_length=120, unique=True)
    code = models.CharField("代号", max_length=30, blank=True)
    kind = models.CharField("类型", max_length=10, choices=Kind.choices, default=Kind.OTR)
    scac = models.CharField("SCAC", max_length=4, blank=True)
    mc_number = models.CharField("MC#", max_length=20, blank=True)
    dot_number = models.CharField("DOT#", max_length=20, blank=True)
    contact_name = models.CharField("联系人", max_length=120, blank=True)
    contact_phone = models.CharField("电话", max_length=60, blank=True)
    contact_email = models.EmailField("邮箱", blank=True)
    dispatch_notes = models.TextField("调度备注", blank=True, help_text="设备、能力、常跑线路等操作信息")

    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.code or self.name

    def get_absolute_url(self):
        return reverse("carrier_detail", args=[self.pk])


class Location(SoftDeleteModel):
    class Kind(models.TextChoices):
        TERMINAL = "TERMINAL", "码头 Terminal"
        YARD = "YARD", "堆场 Yard"
        WAREHOUSE = "WHS", "仓库 Warehouse"
        SITE = "SITE", "现场 Job Site"
        SHIPPER = "SHIPPER", "发货地 Shipper"
        OTHER = "OTHER", "其他"

    code = models.CharField("代号", max_length=60, unique=True, help_text="例如 PTT#2988、NEE-Kola、NJ Yard")
    name = models.CharField("名称", max_length=160, blank=True, help_text="公司/场地名称，印在 BOL 上")
    kind = models.CharField("类型", max_length=10, choices=Kind.choices, default=Kind.OTHER)
    address = models.CharField("地址", max_length=255, blank=True)
    city = models.CharField("城市", max_length=80, blank=True)
    state = models.CharField("州", max_length=40, blank=True)
    postal_code = models.CharField("邮编", max_length=20, blank=True)
    gps = models.CharField("GPS", max_length=60, blank=True, help_text="无门牌地址的现场填经纬度")
    contact_name = models.CharField("现场联系人", max_length=120, blank=True)
    contact_phone = models.CharField("联系电话", max_length=60, blank=True)
    hours = models.CharField("收货时间", max_length=120, blank=True)
    notes = models.TextField("注意事项", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code

    def get_absolute_url(self):
        return reverse("location_detail", args=[self.pk])

    @property
    def full_address(self):
        tail = " ".join(p for p in [self.state, self.postal_code] if p)
        parts = [p for p in [self.address, self.city, tail] if p]
        text = ", ".join(parts)
        if self.gps:
            text = f"{text} (GPS {self.gps})" if text else f"GPS {self.gps}"
        return text


# --------------------------------------------------------------------------
# Projects and shipments
# --------------------------------------------------------------------------
class Project(SoftDeleteModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "进行中"
        DONE = "DONE", "已完成"
        ON_HOLD = "HOLD", "暂停"

    number = models.CharField("项目编号", max_length=20, unique=True, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="projects", verbose_name="客户")
    name = models.CharField("项目名称", max_length=160)
    monday_item_id = models.CharField("Monday Item ID", max_length=40, blank=True, db_index=True)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.ACTIVE)
    default_shipper = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="默认提货地"
    )
    default_consignee = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="默认收货地"
    )
    # Defaults used to pre-fill new shipments and their BOL.
    cargo_description = models.CharField(
        "默认货物描述", max_length=255, blank=True, help_text="例如 Energy Storage System / Lithium ion batteries installed in cargo transport unit"
    )
    un_number = models.CharField("默认 UN 编号", max_length=10, blank=True, help_text="例如 UN3536；非危险品留空")
    hazard_class = models.CharField("默认危险品类别", max_length=10, blank=True, help_text="例如 9")
    nmfc = models.CharField("默认 NMFC", max_length=20, blank=True)
    freight_class = models.CharField("默认 Freight Class", max_length=10, blank=True)
    emergency_contact = models.CharField("24小时紧急联系人", max_length=255, blank=True, help_text="危险品必填，例如 CHEMTREC 703-527-3887")
    bol_instructions = models.TextField("BOL 司机须知", blank=True, help_text="印在该项目每张 BOL 的特别说明里")
    notes = models.TextField("备注", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} {self.name}"

    def get_absolute_url(self):
        return reverse("project_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = f"PRJ-{Sequence.next('PRJ'):04d}"
        super().save(*args, **kwargs)


class MasterBL(SoftDeleteModel):
    """Ocean bill of lading; several containers usually share one."""

    number = models.CharField("MBL/HBL/Booking#", max_length=40, unique=True)
    ocean_carrier = models.CharField("船公司", max_length=60, blank=True)
    vessel_voyage = models.CharField("船名航次", max_length=80, blank=True)
    port_of_discharge = models.CharField("卸货港", max_length=60, blank=True)
    terminal = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="码头"
    )
    etd = models.DateField("ETD", null=True, blank=True)
    eta = models.DateField("ETA", null=True, blank=True)
    ata = models.DateField("实际到港 ATA", null=True, blank=True)
    notes = models.TextField("备注", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-eta", "number"]
        verbose_name = "提单"

    def __str__(self):
        return self.number

    def save(self, *args, **kwargs):
        self.number = self.number.strip().upper()
        super().save(*args, **kwargs)
        for shipment in self.shipments.all():
            shipment.refresh_status()


class Status(models.TextChoices):
    PLANNING = "PLANNING", "待安排"
    AWAITING = "AWAITING", "待到港"
    ARRIVED = "ARRIVED", "已到港"
    IN_TRANSIT = "TRANSIT", "运输中"
    IN_YARD = "YARD", "在堆场"
    DELIVERED = "DELIVERED", "已送达"
    CLOSED = "CLOSED", "已结案"
    HOLD = "HOLD", "Hold"
    CANCELLED = "CANCELLED", "已取消"


# Reaching one of these locks the operational fields of a shipment.
LOCKING_STATUSES = (Status.DELIVERED, Status.CLOSED)


class Shipment(SoftDeleteModel):
    """One container, or one truckload of non-container freight."""

    class Service(models.TextChoices):
        DRAYAGE = "DRAY", "Drayage"
        DRAY_TRANSLOAD = "DRAY_TL", "Drayage + Transload"
        DRAY_STORAGE_OTR = "DRAY_ST_OTR", "Drayage + Storage + OTR"
        DRAY_TL_ST_OTR = "DRAY_TL_ST_OTR", "Drayage + Transload + Storage + OTR"
        OTR = "OTR", "OTR / FTL"
        HEAVY = "HEAVY", "Heavy Haul / OOG"
        LTL = "LTL", "LTL"
        TRANSLOAD = "TL", "Transload"
        STORAGE = "ST", "Storage"
        OTHER = "OTHER", "Other"

    class Equipment(models.TextChoices):
        C20GP = "20GP", "20' GP"
        C20HC = "20HC", "20' HC"
        C20SOC = "20SOC", "20' SOC (BESS)"
        C40GP = "40GP", "40' GP"
        C40HC = "40HC", "40' HC"
        C40FR = "40FR", "40' Flat Rack"
        C40OT = "40OT", "40' Open Top"
        C45HC = "45HC", "45' HC"
        DRY_VAN = "53DV", "53' Dry Van"
        FLATBED = "FLAT", "Flatbed / Step Deck"
        RGN = "RGN", "RGN / Lowboy"
        MULTI_AXLE = "MULTI", "Multi-axle / Perimeter / Schnabel"
        OTHER = "OTHER", "Other"

    class ManualStatus(models.TextChoices):
        NONE = "", "—"
        HOLD = Status.HOLD.value, "Hold"
        CANCELLED = Status.CANCELLED.value, "已取消"

    number = models.CharField("运单号", max_length=20, unique=True, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="shipments", verbose_name="客户")
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="shipments", verbose_name="项目"
    )
    monday_item_id = models.CharField("Monday Item ID", max_length=40, blank=True, db_index=True)
    customer_ref = models.CharField("客户参考号 / PO", max_length=80, blank=True, db_index=True)
    service = models.CharField("服务", max_length=20, choices=Service.choices, default=Service.DRAY_STORAGE_OTR)

    master_bl = models.ForeignKey(
        MasterBL, null=True, blank=True, on_delete=models.SET_NULL, related_name="shipments", verbose_name="提单 MBL"
    )
    container_number = models.CharField("柜号 / 拖车号", max_length=20, blank=True, db_index=True)
    equipment = models.CharField("柜型 / 设备", max_length=10, choices=Equipment.choices, blank=True)
    seal_number = models.CharField("封条号", max_length=40, blank=True)
    lfd = models.DateField("LFD", null=True, blank=True)
    empty_return_date = models.DateField("还空日期", null=True, blank=True)
    empty_return_location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="还空地点"
    )

    shipper = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="起运地"
    )
    consignee = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="最终收货地"
    )

    pod_link = models.URLField("POD 链接", max_length=500, blank=True)
    photos_link = models.URLField("照片链接", max_length=500, blank=True)
    inspection_link = models.URLField("检验报告链接", max_length=500, blank=True)
    other_docs_link = models.URLField("其他文件链接", max_length=500, blank=True)

    manual_status = models.CharField("Hold/取消", max_length=10, choices=ManualStatus.choices, blank=True)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.PLANNING, editable=False, db_index=True)
    is_locked = models.BooleanField("已锁定", default=False, editable=False)
    closed_at = models.DateTimeField("结案时间", null=True, blank=True, editable=False)
    source_ref = models.CharField("数据来源", max_length=255, blank=True, editable=False)
    notes = models.TextField("备注", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.number

    def get_absolute_url(self):
        return reverse("shipment_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.number:
            stamp = timezone.localdate().strftime("%y%m")
            self.number = f"SHP-{stamp}-{Sequence.next('SHP-' + stamp):04d}"
        self.container_number = self.container_number.strip().upper()
        old_status = self.status
        if self.pk:
            old_status = Shipment.all_objects.filter(pk=self.pk).values_list("status", flat=True).first() or old_status
        self.status = self.compute_status()
        if self.status in LOCKING_STATUSES and old_status not in LOCKING_STATUSES:
            self.is_locked = True
        super().save(*args, **kwargs)

    # -- status ------------------------------------------------------------
    def compute_status(self):
        if self.manual_status:
            return self.manual_status
        if self.closed_at:
            return Status.CLOSED
        if not self.pk:
            return Status.ARRIVED if self.master_bl and self.master_bl.ata else (
                Status.AWAITING if self.master_bl else Status.PLANNING
            )
        legs = list(self.legs.all())
        if legs:
            last = legs[-1]
            # Delivered = the last leg reached the final consignee. A leg that
            # ends at a yard just means the next leg hasn't been entered yet.
            if last.actual_delivery and (not self.consignee_id or last.destination_id in (None, self.consignee_id)):
                return Status.DELIVERED
            if any(leg.actual_pickup and not leg.actual_delivery for leg in legs):
                return Status.IN_TRANSIT
        if self.yard_stays.filter(in_date__isnull=False, out_date__isnull=True).exists():
            return Status.IN_YARD
        if legs and any(leg.actual_delivery for leg in legs):
            # Finished a leg but the next one hasn't started yet.
            return Status.IN_YARD
        if self.master_bl:
            return Status.ARRIVED if self.master_bl.ata else Status.AWAITING
        return Status.PLANNING

    def refresh_status(self):
        old = self.status
        new = self.compute_status()
        fields = {}
        if new != old:
            fields["status"] = new
            if new in LOCKING_STATUSES and old not in LOCKING_STATUSES:
                fields["is_locked"] = True
        if fields:
            for k, v in fields.items():
                setattr(self, k, v)
            # Bypass history for status bookkeeping; the change that caused it
            # is already recorded on the child row.
            Shipment.all_objects.filter(pk=self.pk).update(**fields)

    @property
    def lfd_warning(self):
        """LFD is close and the container hasn't left the port yet."""
        if not self.lfd or self.status not in (Status.PLANNING, Status.AWAITING, Status.ARRIVED):
            return False
        return self.lfd <= timezone.localdate() + timedelta(days=settings.TMS_LFD_WARNING_DAYS)

    @property
    def current_leg(self):
        legs = list(self.legs.all())
        for leg in legs:
            if not leg.actual_delivery:
                return leg
        return legs[-1] if legs else None


def _plain(d):
    """Decimal('96.0') → '96', Decimal('238.5') → '238.5'."""
    d = Decimal(d)
    return f"{d.quantize(Decimal(1)):f}" if d == d.to_integral() else f"{d.normalize():f}"


class CargoLine(SoftDeleteModel):
    """What is on the truck. Drives the BOL commodity table."""

    class HandlingType(models.TextChoices):
        CONTAINER = "CNTR", "Container"
        UNIT = "UNIT", "Unit"
        PALLET = "PLT", "Pallet"
        CRATE = "CRATE", "Crate"
        SKID = "SKID", "Skid"
        PIECE = "PCS", "Piece"
        COIL = "COIL", "Coil"
        OTHER = "OTHER", "Other"

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="cargo_lines")
    handling_qty = models.PositiveIntegerField("数量", default=1)
    handling_type = models.CharField("单位", max_length=6, choices=HandlingType.choices, default=HandlingType.CONTAINER)
    pieces = models.PositiveIntegerField("件数", null=True, blank=True)
    description = models.CharField("货物描述", max_length=255)
    weight_lbs = models.DecimalField("该行总重 (lbs)", max_digits=10, decimal_places=1, null=True, blank=True)
    length_in = models.DecimalField("长 (in)", max_digits=7, decimal_places=1, null=True, blank=True)
    width_in = models.DecimalField("宽 (in)", max_digits=7, decimal_places=1, null=True, blank=True)
    height_in = models.DecimalField("高 (in)", max_digits=7, decimal_places=1, null=True, blank=True)
    hazmat = models.BooleanField("危险品 HM", default=False)
    un_number = models.CharField("UN 编号", max_length=10, blank=True)
    hazard_class = models.CharField("类别", max_length=10, blank=True)
    packing_group = models.CharField("包装组 PG", max_length=5, blank=True)
    nmfc = models.CharField("NMFC", max_length=20, blank=True)
    freight_class = models.CharField("Class", max_length=10, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.description

    @property
    def dims(self):
        if self.length_in and self.width_in and self.height_in:
            return " x ".join(_plain(v) for v in (self.length_in, self.width_in, self.height_in))
        return ""

    @property
    def bol_description(self):
        if self.hazmat and self.un_number:
            parts = [self.un_number, self.description]
            if self.hazard_class:
                parts.append(self.hazard_class)
            if self.packing_group:
                parts.append(f"PG {self.packing_group}")
            return ", ".join(parts)
        return self.description


class Unit(SoftDeleteModel):
    """Serialized equipment inside a shipment (e.g. BESS cabinets)."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="units")
    serial_number = models.CharField("序列号", max_length=60, db_index=True)
    product = models.CharField("产品", max_length=80, blank=True)
    config = models.CharField("型号 / Config", max_length=40, blank=True)
    tier = models.CharField("Tier", max_length=20, blank=True)
    new_serial_number = models.CharField("换标后序列号", max_length=60, blank=True, db_index=True)
    notes = models.CharField("备注", max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.serial_number


class Leg(SoftDeleteModel):
    """One move: port→yard, yard→site, site→site…"""

    class Kind(models.TextChoices):
        DRAYAGE = "DRAY", "拖柜 Drayage"
        TRANSFER = "TRANSFER", "堆场转运"
        OTR = "OTR", "公路 OTR"
        HEAVY = "HEAVY", "重载 Heavy Haul"
        LTL = "LTL", "零担 LTL"
        OTHER = "OTHER", "其他"

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="legs")
    sequence = models.PositiveSmallIntegerField("顺序", default=1)
    kind = models.CharField("类型", max_length=10, choices=Kind.choices, default=Kind.OTR)
    origin = models.ForeignKey(Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="起点")
    destination = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="终点"
    )
    carrier = models.ForeignKey(Carrier, null=True, blank=True, on_delete=models.SET_NULL, related_name="legs", verbose_name="承运商")
    carrier_load_number = models.CharField("承运商 Load# / PRO#", max_length=60, blank=True)
    equipment = models.CharField("车辆设备", max_length=60, blank=True, help_text="例如 9-axle RGN、53' Flatbed")
    driver_name = models.CharField("司机", max_length=80, blank=True)
    driver_phone = models.CharField("司机电话", max_length=40, blank=True)
    truck_trailer = models.CharField("车牌 / 拖车号", max_length=60, blank=True)
    scheduled_pickup = models.DateField("计划提货", null=True, blank=True)
    pickup_appt = models.TimeField("提货预约时间", null=True, blank=True)
    actual_pickup = models.DateField("实际提货", null=True, blank=True)
    scheduled_delivery = models.DateField("计划送达", null=True, blank=True)
    delivery_appt = models.TimeField("送达预约时间", null=True, blank=True)
    actual_delivery = models.DateField("实际送达", null=True, blank=True)
    tracking_url = models.URLField("跟踪链接", max_length=500, blank=True)
    bol_number = models.CharField("BOL 号（留空自动生成）", max_length=40, blank=True)
    bol_instructions = models.TextField("本段 BOL 特别说明", blank=True)
    notes = models.TextField("备注", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["sequence", "id"]

    def __str__(self):
        return f"{self.shipment.number} #{self.sequence} {self.get_kind_display()}"

    def get_absolute_url(self):
        return reverse("shipment_detail", args=[self.shipment_id]) + "#leg"

    @property
    def effective_bol_number(self):
        return self.bol_number or f"{self.shipment.number}-{self.sequence}"


class YardStay(SoftDeleteModel):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="yard_stays")
    yard = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+", verbose_name="堆场")
    in_date = models.DateField("进场日期", null=True, blank=True)
    out_date = models.DateField("出场日期", null=True, blank=True)
    free_days = models.PositiveSmallIntegerField("免费天数", default=0)
    notes = models.CharField("备注", max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["in_date", "id"]

    def __str__(self):
        return f"{self.yard} {self.in_date}–{self.out_date or ''}"

    def total_days(self, as_of=None):
        """Calendar days in the yard, counting both in and out day."""
        if not self.in_date:
            return 0
        end = self.out_date or as_of or timezone.localdate()
        return max((end - self.in_date).days + 1, 0)

    def billable_days(self, as_of=None):
        return max(self.total_days(as_of) - self.free_days, 0)

    def days_by_month(self, as_of=None):
        """[(YYYY-MM, days)] — for monthly storage billing."""
        if not self.in_date:
            return []
        end = self.out_date or as_of or timezone.localdate()
        out, cur = [], self.in_date
        while cur <= end:
            nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
            stop = min(end, nxt - timedelta(days=1))
            out.append((cur.strftime("%Y-%m"), (stop - cur).days + 1))
            cur = nxt
        return out


class Charge(SoftDeleteModel):
    class Side(models.TextChoices):
        AR = "AR", "应收 AR"
        AP = "AP", "应付 AP"

    class Type(models.TextChoices):
        DRAYAGE = "DRAYAGE", "Drayage"
        TRANSLOAD = "TRANSLOAD", "Transload"
        LINEHAUL = "LINEHAUL", "Linehaul / Final Delivery"
        FUEL = "FUEL", "Fuel Surcharge"
        STORAGE = "STORAGE", "Storage"
        CHASSIS = "CHASSIS", "Chassis"
        PREPULL = "PREPULL", "Prepull"
        DETENTION = "DETENTION", "Detention"
        LAYOVER = "LAYOVER", "Layover"
        TONU = "TONU", "TONU / Dry Run"
        PERMIT = "PERMIT", "Permit"
        ESCORT = "ESCORT", "Escort / Pilot Car"
        PIERPASS = "PIERPASS", "Pierpass / Toll"
        DEMURRAGE = "DEMURRAGE", "Demurrage"
        PERDIEM = "PERDIEM", "Per Diem"
        OVERWEIGHT = "OVERWEIGHT", "Overweight"
        CRANE = "CRANE", "Crane / Rigging"
        TARP = "TARP", "Tarp"
        PAY_ON_BEHALF = "POB", "Pay on Behalf"
        OTHER = "OTHER", "Other"

    class State(models.TextChoices):
        OPEN = "OPEN", "待处理"
        INVOICED = "INVOICED", "已开票/已收票"
        PAID = "PAID", "已收/已付"
        DISPUTED = "DISPUTED", "争议中"
        VOID = "VOID", "作废"

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="charges")
    side = models.CharField("应收/应付", max_length=2, choices=Side.choices)
    charge_type = models.CharField("费用类型", max_length=12, choices=Type.choices)
    description = models.CharField("说明", max_length=255, blank=True)
    quantity = models.DecimalField("数量", max_digits=10, decimal_places=2, default=Decimal("1"))
    unit_price = models.DecimalField("单价", max_digits=12, decimal_places=2, default=Decimal("0"))
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2, default=Decimal("0"), editable=False)
    carrier = models.ForeignKey(
        Carrier, null=True, blank=True, on_delete=models.SET_NULL, related_name="charges", verbose_name="付款对象（承运商）"
    )
    invoice_number = models.CharField("发票号", max_length=60, blank=True, db_index=True)
    invoice_date = models.DateField("发票日期", null=True, blank=True)
    state = models.CharField("状态", max_length=10, choices=State.choices, default=State.OPEN)
    notes = models.CharField("备注", max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["side", "id"]

    def __str__(self):
        return f"{self.get_side_display()} {self.get_charge_type_display()} {self.amount}"

    def save(self, *args, **kwargs):
        self.amount = (self.quantity or 0) * (self.unit_price or 0)
        super().save(*args, **kwargs)


class Note(models.Model):
    """Free-text timeline entries and system events (BOL printed, unlocked…)."""

    class Kind(models.TextChoices):
        NOTE = "NOTE", "备注"
        SYSTEM = "SYSTEM", "系统"
        BOL = "BOL", "BOL"

    shipment = models.ForeignKey(Shipment, null=True, blank=True, on_delete=models.CASCADE, related_name="timeline_notes")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="timeline_notes")
    kind = models.CharField(max_length=6, choices=Kind.choices, default=Kind.NOTE)
    text = models.TextField("内容")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.text[:60]
