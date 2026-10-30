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
    RFQ = "RFQ", "询报价 / 销售"


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    # One person can hold several roles (e.g. 操作 + 询报价). Stored as ",OP,RFQ," so a
    # database "contains ',OP,'" filter finds them; read with .role_set, write with set_roles().
    roles = models.CharField("角色", max_length=40, blank=True, default="")
    phone = models.CharField("电话", max_length=40, blank=True)

    history = HistoricalRecords()

    def __str__(self):
        return f"{self.user} ({self.roles_label or '未分配'})"

    @property
    def role_set(self):
        return {r for r in self.roles.split(",") if r in Role.values}

    def set_roles(self, roles):
        keep = [r for r in Role.values if r in set(roles)]  # stable order
        self.roles = f",{','.join(keep)}," if keep else ""

    @property
    def roles_label(self):
        return "、".join(Role(r).label for r in Role.values if r in self.role_set)

    @staticmethod
    def with_roles(*roles):
        """Q filter on users holding any of these roles."""
        q = models.Q(pk__in=[])
        for r in roles:
            q |= models.Q(profile__roles__contains=f",{r},")
        return q


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
        return reverse("master_detail", args=["customer", self.pk])


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
        return reverse("master_detail", args=["carrier", self.pk])


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
        return reverse("master_detail", args=["location", self.pk])

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
class Company(models.TextChoices):
    ATS = "ATS", "ATS"
    PTT = "PTT", "PTT"


class Project(SoftDeleteModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "待执行"
        ACTIVE = "ACTIVE", "执行中"
        DELIVERED = "DELIVERED", "已交付待POD"
        DONE = "DONE", "已完结"
        CANCELLED = "CANCELLED", "已取消"

    number = models.CharField("项目编号", max_length=20, unique=True, editable=False)
    company = models.CharField("公司", max_length=4, choices=Company.choices, default=Company.ATS)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="projects", verbose_name="客户")
    name = models.CharField("项目名称", max_length=160)
    owners = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="owned_projects", verbose_name="负责人")
    monday_item_id = models.CharField("Monday Item ID", max_length=40, blank=True, db_index=True)
    start_date = models.DateField("开始日期", null=True, blank=True)
    end_date = models.DateField("结束日期", null=True, blank=True)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.PENDING, editable=False, db_index=True)
    # Only 已完结 / 已取消 are set by hand; the rest follow the loads.
    manual_status = models.CharField(
        "手动状态", max_length=10, blank=True,
        choices=[("", "—（按 Load 自动）"), (Status.DONE.value, "已完结"), (Status.CANCELLED.value, "已取消")],
    )
    default_shipper = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="默认提货地"
    )
    default_consignee = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="默认收货地"
    )
    # Defaults used to pre-fill new loads and their BOL.
    cargo_description = models.CharField(
        "默认货物描述", max_length=255, blank=True, help_text="例如 Energy Storage System / Lithium ion batteries installed in cargo transport unit"
    )
    un_number = models.CharField("默认 UN 编号", max_length=10, blank=True, help_text="例如 UN3536；非危险品留空")
    hazard_class = models.CharField("默认危险品类别", max_length=10, blank=True, help_text="例如 9")
    nmfc = models.CharField("默认 NMFC", max_length=20, blank=True)
    freight_class = models.CharField("默认 Freight Class", max_length=10, blank=True)
    emergency_contact = models.CharField("24小时紧急联系人", max_length=255, blank=True, help_text="危险品必填，例如 CHEMTREC 703-527-3887")
    bol_instructions = models.TextField("BOL 司机须知", blank=True, help_text="印在该项目每张 BOL 的特别说明里")
    detention_free_hours = models.DecimalField("免费等待（小时）", max_digits=4, decimal_places=1, default=Decimal("2"))
    detention_rate = models.DecimalField("向客户收 Detention（$/小时）", max_digits=8, decimal_places=2, null=True, blank=True)
    terms = models.TextField("商务条款", blank=True, help_text="售价、附加费标准、报价来源等")
    source_ref = models.CharField("数据来源", max_length=255, blank=True, editable=False)
    notes = models.TextField("备注", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("project_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = f"PRJ-{Sequence.next('PRJ'):04d}"
        self.status = self.compute_status()
        super().save(*args, **kwargs)

    def compute_status(self):
        if self.manual_status:
            return self.manual_status
        if not self.pk:
            return self.Status.PENDING
        loads = [s for s in self.shipments.all() if s.status != Status.CANCELLED]
        if not loads:
            return self.Status.PENDING
        if all(s.status in LOCKING_STATUSES for s in loads):
            return self.Status.DELIVERED
        started = any(s.status in (Status.IN_TRANSIT, Status.IN_YARD, Status.DELIVERED, Status.CLOSED) for s in loads)
        return self.Status.ACTIVE if started else self.Status.PENDING

    def refresh_status(self):
        new = self.compute_status()
        if new != self.status:
            self.status = new
            Project.all_objects.filter(pk=self.pk).update(status=new)

    @property
    def is_open(self):
        return self.status not in (self.Status.DONE, self.Status.CANCELLED)


class Milestone(models.Model):
    """Custom project milestones. Standard ones (first pickup, last delivery,
    POD complete…) are derived from the loads and never stored."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    name = models.CharField("节点", max_length=80)
    planned_date = models.DateField("计划日期", null=True, blank=True)
    done_date = models.DateField("完成日期", null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["planned_date", "id"]

    def __str__(self):
        return self.name


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


class PodCheck(models.TextChoices):
    PENDING = "PENDING", "待检查"
    OK = "OK", "合规"
    NO_SIGN = "NO_SIGN", "签字缺失"
    DATE = "DATE", "日期问题"


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
        # Ocean containers
        C20GP = "20GP", "20' GP"
        C20HC = "20HC", "20' HC"
        C20OT = "20OT", "20' Open Top"
        C20FR = "20FR", "20' Flat Rack"
        C20RF = "20RF", "20' Reefer"
        C20TK = "20TK", "20' Tank"
        C20SOC = "20SOC", "20' SOC (BESS)"
        C40GP = "40GP", "40' GP"
        C40HC = "40HC", "40' HC"
        C40OT = "40OT", "40' Open Top"
        C40FR = "40FR", "40' Flat Rack"
        C40RF = "40RF", "40' Reefer"
        C40SOC = "40SOC", "40' SOC"
        C45HC = "45HC", "45' HC"
        C53CTR = "53CTR", "53' Domestic Container"
        # Trucks and trailers
        DRY_VAN = "53DV", "53' Dry Van"
        REEFER = "53RF", "53' Reefer"
        FLATBED = "FLAT", "Flatbed / Step Deck"
        CONESTOGA = "CONE", "Conestoga"
        DOUBLE_DROP = "DD", "Double Drop"
        RGN = "RGN", "RGN / Lowboy"
        MULTI_AXLE = "MULTI", "Multi-axle / Perimeter / Schnabel"
        HOTSHOT = "HOTSHOT", "Hot Shot"
        BOX = "BOX", "Box Truck / Straight Truck"
        POWER_ONLY = "PO", "Power Only"
        OTHER = "OTHER", "Other"

    class ManualStatus(models.TextChoices):
        NONE = "", "—"
        HOLD = Status.HOLD.value, "Hold"
        CANCELLED = Status.CANCELLED.value, "已取消"

    number = models.CharField("Load#", max_length=20, unique=True, editable=False)
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
    pod_received = models.DateField("POD 收到日期", null=True, blank=True)
    pod_check = models.CharField("POD 审核", max_length=10, choices=PodCheck.choices, blank=True)
    # Person-confirmed: the POD has been checked and filed where the customer wants it (OneDrive, portal…).
    pod_uploaded_at = models.DateTimeField("POD 已上传", null=True, blank=True, editable=False)
    pod_uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+", editable=False)
    pod_waived_reason = models.CharField("无需 POD 的原因", max_length=255, blank=True, editable=False,
                                         help_text="管理层设置；有值即视为无需 POD")
    empty_return_due = models.DateField("还空期限", null=True, blank=True, help_text="Per Diem / 车架免费期最后一天")
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
        if self.project_id:
            self.project.refresh_status()

    # -- status ------------------------------------------------------------
    @property
    def pod_state(self):
        """WAIVED / UPLOADED / RECEIVED (seen, not yet filed) / MISSING."""
        if self.pod_waived_reason:
            return "WAIVED"
        if self.pod_uploaded_at:
            return "UPLOADED"
        return "RECEIVED" if self.pod_received else "MISSING"

    @property
    def pod_done(self):
        return self.pod_state in ("WAIVED", "UPLOADED")

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
            if self.project_id:
                self.project.refresh_status()

    @property
    def lfd_warning(self):
        """LFD is close and the container hasn't left the port yet."""
        if not self.lfd or self.status not in (Status.PLANNING, Status.AWAITING, Status.ARRIVED):
            return False
        return self.lfd <= timezone.localdate() + timedelta(days=settings.TMS_LFD_WARNING_DAYS)

    @property
    def delivered_on(self):
        legs = list(self.legs.all())
        return legs[-1].actual_delivery if legs and self.status in LOCKING_STATUSES else None

    @property
    def picked_up_on(self):
        legs = list(self.legs.all())
        return legs[0].actual_pickup if legs else None

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
    length_ft = models.DecimalField("长 (ft)", max_digits=7, decimal_places=2, null=True, blank=True)
    width_ft = models.DecimalField("宽 (ft)", max_digits=7, decimal_places=2, null=True, blank=True)
    height_ft = models.DecimalField("高 (ft)", max_digits=7, decimal_places=2, null=True, blank=True)
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
        if self.length_ft and self.width_ft and self.height_ft:
            return " x ".join(_plain(v) for v in (self.length_ft, self.width_ft, self.height_ft))
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
    pickup_appt_confirmed = models.BooleanField("提货预约已确认", default=False)
    delivery_appt_confirmed = models.BooleanField("送货预约已确认", default=False)
    permits_required = models.BooleanField("需要许可证 / 护送", default=False)
    permits_confirmed = models.BooleanField("许可证 / 护送已确认", default=False)
    pickup_arrived_at = models.DateTimeField("到达提货点", null=True, blank=True)
    pickup_departed_at = models.DateTimeField("离开提货点", null=True, blank=True)
    delivery_arrived_at = models.DateTimeField("到达收货点", null=True, blank=True)
    delivery_departed_at = models.DateTimeField("离开收货点", null=True, blank=True)
    last_tracking_at = models.DateTimeField("最近跟踪更新", null=True, blank=True)
    actual_delivery = models.DateField("实际送达", null=True, blank=True)
    tracking_url = models.URLField("跟踪链接", max_length=500, blank=True)
    bol_number = models.CharField("BOL 号（留空自动生成）", max_length=40, blank=True)
    bol_instructions = models.TextField("本段 BOL 特别说明", blank=True)
    notes = models.TextField("备注", blank=True)
    ratecon_version = models.PositiveSmallIntegerField("运价确认单版本", default=0, editable=False)
    ratecon_at = models.DateTimeField("运价确认单生成时间", null=True, blank=True, editable=False)

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

    @staticmethod
    def _wait(arrived, departed, free_hours):
        if not arrived:
            return None
        end = departed or timezone.now()
        hours = Decimal(max((end - arrived).total_seconds(), 0)) / 3600
        return {"hours": hours.quantize(Decimal("0.1")), "billable": max(hours - Decimal(free_hours), 0).quantize(Decimal("0.1")),
                "running": departed is None}

    def pickup_wait(self, free_hours=2):
        if not self.pickup_departed_at and self.actual_pickup:
            return None  # left already, but nobody recorded when: unknown, not "still waiting"
        return self._wait(self.pickup_arrived_at, self.pickup_departed_at, free_hours)

    def delivery_wait(self, free_hours=2):
        if not self.delivery_departed_at and self.actual_delivery:
            return None
        return self._wait(self.delivery_arrived_at, self.delivery_departed_at, free_hours)


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


# --------------------------------------------------------------------------
# Per-user workspace: dashboard layout and to-do preferences
# --------------------------------------------------------------------------
class Dashboard(models.Model):
    """Widget layout: [{"id": "w1", "type": "todo", "size": "L", "config": {...}}, ...]"""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dashboard")
    layout = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)


class AlertSetting(models.Model):
    """A user's override of one built-in to-do rule (see core/alerts.py)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alert_settings")
    rule = models.CharField(max_length=40)
    enabled = models.BooleanField(default=True)
    threshold = models.IntegerField(null=True, blank=True)
    scope = models.CharField(max_length=4, default="mine")  # mine | all
    level = models.CharField(max_length=6, default="")

    class Meta:
        unique_together = [("user", "rule")]


class AlertSnooze(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="alert_snoozes")
    key = models.CharField(max_length=120, db_index=True)  # rule:object
    until = models.DateTimeField()

    class Meta:
        unique_together = [("user", "key")]


class CustomRule(models.Model):
    """"When a Load matches these conditions, remind me." Kept deliberately small."""

    class Field(models.TextChoices):
        CUSTOMER = "customer", "客户"
        CARRIER = "carrier", "承运商"
        PROJECT = "project", "项目"
        STATUS = "status", "状态"
        PICKUP_WITHIN = "pickup_within", "计划提货在 N 天内"
        DELIVERY_WITHIN = "delivery_within", "计划送达在 N 天内"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="custom_rules")
    name = models.CharField("提醒标题", max_length=120)
    field1 = models.CharField("条件一", max_length=20, choices=Field.choices)
    value1 = models.CharField("值", max_length=120)
    field2 = models.CharField("条件二（可选）", max_length=20, choices=Field.choices, blank=True)
    value2 = models.CharField("值", max_length=120, blank=True)
    level = models.CharField("级别", max_length=6, default="today")
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------
# AI email reading (see core/ai.py)
# --------------------------------------------------------------------------
class AiExtraction(models.Model):
    """One pasted email and what Gemini proposed from it."""

    class State(models.TextChoices):
        PENDING = "PENDING", "待确认"
        DONE = "DONE", "已处理"
        DISCARDED = "DISCARDED", "已放弃"

    class Source(models.TextChoices):
        PASTE = "PASTE", "手动粘贴"
        GMAIL = "GMAIL", "邮箱自动扫描"

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=6, choices=Source.choices, default=Source.PASTE)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    shipment = models.ForeignKey(Shipment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source_text = models.TextField("邮件内容")
    model_name = models.CharField(max_length=80, blank=True)
    summary = models.TextField(blank=True)
    unmatched = models.TextField(blank=True)
    raw_result = models.JSONField(default=dict, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=State.choices, default=State.PENDING, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"AI #{self.pk}"

    def get_absolute_url(self):
        return reverse("ai_review", args=[self.pk])


class AiChange(models.Model):
    """One proposed field change (or timeline note) for one Load."""

    class State(models.TextChoices):
        PENDING = "PENDING", "待确认"
        APPLIED = "APPLIED", "已写入"
        REJECTED = "REJECTED", "未采用"
        STALE = "STALE", "已被他人修改"
        BLOCKED = "BLOCKED", "无法写入"

    extraction = models.ForeignKey(AiExtraction, on_delete=models.CASCADE, related_name="changes")
    shipment = models.ForeignKey(Shipment, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    leg = models.ForeignKey(Leg, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    field = models.CharField(max_length=40)
    old_value = models.CharField(max_length=500, blank=True)
    new_value = models.TextField()
    evidence = models.TextField(blank=True)
    tz_assumed = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    result = models.CharField(max_length=200, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["shipment_id", "id"]

    def _spec(self):
        from .ai import FIELDS

        return FIELDS.get(self.field)

    @property
    def is_note(self):
        from .ai import NOTE_FIELD

        return self.field == NOTE_FIELD

    @property
    def label(self):
        spec = self._spec()
        return spec.label if spec else "时间线备注"

    @property
    def old_display(self):
        from .ai import display

        spec = self._spec()
        return display(spec.kind, self.old_value) if spec else ""

    @property
    def new_display(self):
        from .ai import display

        spec = self._spec()
        return display(spec.kind, self.new_value) if spec else self.new_value


# --------------------------------------------------------------------------
# Mailbox scanning (see core/mailscan.py)
# --------------------------------------------------------------------------
class Notification(models.Model):
    """In-app notice for one person: bell count, a pop-up on their next page, optional desktop alert."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    text = models.CharField(max_length=300)
    url = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    shown_at = models.DateTimeField(null=True, blank=True)  # popped up once
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self):
        return self.text


class GmailConnection(models.Model):
    """A person's read-only Gmail access, granted by themselves."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="gmail")
    email = models.EmailField()
    refresh_token = models.TextField(help_text="加密保存")
    enabled = models.BooleanField(default=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_scan_at = models.DateTimeField(null=True, blank=True)
    last_result = models.CharField(max_length=300, blank=True)
    last_error = models.CharField(max_length=500, blank=True)

    def __str__(self):
        return self.email


class ScannedMessage(models.Model):
    """A Gmail message already sent to AI, so it is never read twice."""

    connection = models.ForeignKey(GmailConnection, on_delete=models.CASCADE, related_name="scanned")
    message_id = models.CharField(max_length=64)
    subject = models.CharField(max_length=300, blank=True)
    scanned_at = models.DateTimeField(auto_now_add=True)
    extraction = models.ForeignKey(AiExtraction, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        unique_together = [("connection", "message_id")]


# --------------------------------------------------------------------------
# Inquiries and quotes (询报价): Lead → scope → carrier RFQ → customer quote → project
# --------------------------------------------------------------------------
class Lead(SoftDeleteModel):
    class Status(models.TextChoices):
        NEW = "NEW", "新询价"
        SCOPING = "SCOPING", "梳理需求"
        SOURCING = "SOURCING", "找车询价中"
        QUOTED = "QUOTED", "已报价待回复"
        WON = "WON", "成单"
        LOST = "LOST", "未成单"
        CANCELLED = "CANCELLED", "取消"

    class Source(models.TextChoices):
        EMAIL = "EMAIL", "邮件"
        WECHAT = "WECHAT", "微信"
        PHONE = "PHONE", "电话"
        OTHER = "OTHER", "其他"

    OPEN = (Status.NEW, Status.SCOPING, Status.SOURCING, Status.QUOTED)

    number = models.CharField("询价编号", max_length=20, unique=True, editable=False)
    title = models.CharField("询价名称", max_length=200)
    company = models.CharField("公司", max_length=4, choices=Company.choices, default=Company.ATS)
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads",
                                 verbose_name="客户（已有）")
    prospect_name = models.CharField("客户名称（新客户）", max_length=120, blank=True, help_text="系统里还没有的客户；成单时自动建客户")
    contact_name = models.CharField("联系人", max_length=120, blank=True)
    contact_email = models.CharField("联系人邮箱", max_length=200, blank=True)
    contact_phone = models.CharField("联系人电话 / 微信", max_length=80, blank=True)
    source = models.CharField("来源", max_length=8, choices=Source.choices, default=Source.EMAIL)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.NEW, db_index=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="leads", verbose_name="负责人（询报价）")
    quote_due = models.DateField("报价截止", null=True, blank=True)
    customer_ref = models.CharField("客户参考号 / 项目号", max_length=80, blank=True)
    summary = models.TextField("需求概要", blank=True)
    questions = models.TextField("待向客户确认", blank=True, help_text="缺的信息，一行一条")
    notes = models.TextField("内部备注", blank=True)
    lost_reason = models.CharField("未成单原因", max_length=255, blank=True)
    source_text = models.TextField("询价原文", blank=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="leads", verbose_name="成单项目")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.number} {self.title}"

    def save(self, *args, **kwargs):
        if not self.number:
            stamp = timezone.localdate().strftime("%y%m")
            self.number = f"RFQ-{stamp}-{Sequence.next('RFQ-' + stamp):04d}"
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("lead_detail", args=[self.pk])

    @property
    def customer_label(self):
        return self.customer.name if self.customer_id else self.prospect_name

    @property
    def is_open(self):
        return self.status in self.OPEN


class AiUsage(models.Model):
    """Tokens used by one Gemini call, for the AI usage page (management only)."""

    class Feature(models.TextChoices):
        OPS_PASTE = "OPS_PASTE", "运营 · 手动 AI 识别"
        OPS_GMAIL = "OPS_GMAIL", "运营 · 邮箱扫描 / 按主题识别"
        LEAD = "LEAD", "询报价 · AI 整理询价"
        REPLY = "REPLY", "询报价 · 承运商回复整理"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    feature = models.CharField(max_length=10, choices=Feature.choices)
    model_name = models.CharField(max_length=80, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0, help_text="AI 写出的结果")
    thinking_tokens = models.PositiveIntegerField(default=0, help_text="模型思考，按输出计费")

    class Meta:
        ordering = ["-created_at"]


class TrailerType(models.Model):
    """Equipment library for the OOG screen: deck height, deck length and combination tare weight.
    Defaults come from manufacturer spec sheets; the team edits them to match the fleets they use."""

    key = models.SlugField(unique=True)
    name = models.CharField("车型", max_length=80)
    deck_height_in = models.DecimalField("装货面离地高 (in)", max_digits=6, decimal_places=1, null=True, blank=True,
                                         help_text="低平板 / 阶梯板填凹槽或下层板的高度")
    deck_length_ft = models.DecimalField("可装货长度 (ft)", max_digits=6, decimal_places=1, null=True, blank=True,
                                         help_text="阶梯板 / 低平板填下层板或凹槽长度")
    deck_width_in = models.DecimalField("车板宽 (in)", max_digits=6, decimal_places=1, default=Decimal("102"))
    trailer_tare_lbs = models.PositiveIntegerField("挂车自重 (lbs)", null=True, blank=True, help_text="多轴车含 jeep / booster")
    tractor_tare_lbs = models.PositiveIntegerField("车头自重 (lbs)", default=17000)
    trailer_length_ft = models.DecimalField("挂车总长 (ft)", max_digits=6, decimal_places=1, null=True, blank=True)
    max_extended_length_ft = models.DecimalField("最大伸长 (ft)", max_digits=6, decimal_places=1, null=True, blank=True)
    rated_capacity_lbs = models.PositiveIntegerField("额定载重 (lbs)", null=True, blank=True)
    combo_axles = models.PositiveSmallIntegerField("车组总轴数", null=True, blank=True)
    enclosed = models.BooleanField("厢式（只能装进厢内）", default=False)
    sort = models.PositiveSmallIntegerField("推荐顺序", default=50, help_text="小的优先：越普通、越便宜的车排越前")
    active = models.BooleanField("参与自动建议", default=True)
    notes = models.TextField("说明 / 来源", blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["sort", "name"]

    def __str__(self):
        return self.name

    @property
    def combo_tare_lbs(self):
        if self.trailer_tare_lbs is None or self.tractor_tare_lbs is None:
            return None
        return self.trailer_tare_lbs + self.tractor_tare_lbs

    @property
    def complete(self):
        """Has what the OOG screen needs."""
        return self.deck_height_in is not None and self.deck_length_ft is not None and self.combo_tare_lbs is not None


class LeadLane(SoftDeleteModel):
    """One move in the scope of work: where from, where to, what equipment, how many trucks."""

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="lanes")
    sequence = models.PositiveSmallIntegerField("顺序", default=1)
    origin = models.CharField("起点", max_length=255, help_text="名称 + 地址，或港口 / 城市, 州")
    origin_zip = models.CharField("起点邮编", max_length=12, blank=True)
    destination = models.CharField("终点", max_length=255)
    destination_zip = models.CharField("终点邮编", max_length=12, blank=True)
    service = models.CharField("服务", max_length=20, choices=Shipment.Service.choices, default=Shipment.Service.OTR)
    equipment = models.CharField("车型 / 柜型", max_length=80, blank=True, help_text="如 Flatbed、Step Deck、RGN 9-axle、40HC")
    trucks = models.PositiveIntegerField("车次 / 柜数", default=1)
    ready_date = models.DateField("可提货日", null=True, blank=True)
    deliver_by = models.DateField("最晚送达", null=True, blank=True)
    notes = models.TextField("要求 / 备注", blank=True, help_text="许可证、护送、吊装、预约、保险等")
    origin_state = models.CharField("起点州", max_length=2, blank=True, help_text="两位州代码，如 GA")
    destination_state = models.CharField("终点州", max_length=2, blank=True, help_text="如 TX")
    via_states = models.CharField("途经州", max_length=80, blank=True, help_text="按路线顺序填，如 AL,MS,LA；不填只按起点和终点州判断")
    trailer = models.ForeignKey("TrailerType", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                verbose_name="指定车型（空 = 自动建议）")

    history = HistoricalRecords()

    class Meta:
        ordering = ["lead", "sequence", "id"]

    def __str__(self):
        return f"第 {self.sequence} 段 {self.origin[:18]} → {self.destination[:18]}"


class LeadCargo(SoftDeleteModel):
    """Cargo in the scope, in ft / lbs (see core/units.py)."""

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="cargo")
    lane = models.ForeignKey(LeadLane, null=True, blank=True, on_delete=models.SET_NULL, related_name="cargo", verbose_name="属于哪一段")
    description = models.CharField("品名", max_length=255)
    qty = models.PositiveIntegerField("件数", default=1)
    handling_type = models.CharField("单位", max_length=6, choices=CargoLine.HandlingType.choices, default=CargoLine.HandlingType.UNIT)
    weight_lbs = models.DecimalField("每件重量 (lbs)", max_digits=10, decimal_places=1, null=True, blank=True)
    length_ft = models.DecimalField("长 (ft)", max_digits=7, decimal_places=2, null=True, blank=True)
    width_ft = models.DecimalField("宽 (ft)", max_digits=7, decimal_places=2, null=True, blank=True)
    height_ft = models.DecimalField("高 (ft)", max_digits=7, decimal_places=2, null=True, blank=True)
    hazmat = models.BooleanField("危险品", default=False)
    divisible = models.BooleanField("可拆分（divisible）", default=False,
                                    help_text="能拆开或减重分装的货物不能办超重 / 超尺寸许可，要拆分或多车运")
    notes = models.CharField("备注", max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["lead", "id"]

    def __str__(self):
        return f"{self.qty} × {self.description}"

    @property
    def total_weight(self):
        return (self.weight_lbs or 0) * self.qty if self.weight_lbs else None

    @property
    def dims_text(self):
        from .quotes import ft_in

        vals = (self.length_ft, self.width_ft, self.height_ft)
        if not any(vals):
            return ""
        return " × ".join(ft_in(v) or "?" for v in vals)


class CarrierQuote(SoftDeleteModel):
    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "已询价待回复"
        QUOTED = "QUOTED", "已回价"
        DECLINED = "DECLINED", "不接 / 无车"
        SELECTED = "SELECTED", "已选用"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="carrier_quotes")
    lane = models.ForeignKey(LeadLane, null=True, blank=True, on_delete=models.SET_NULL, related_name="carrier_quotes",
                             verbose_name="哪一段（空 = 整个项目）")
    carrier = models.ForeignKey(Carrier, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotes", verbose_name="承运商（已有）")
    carrier_name = models.CharField("承运商名称（新）", max_length=120, blank=True)
    contact_email = models.CharField("询价邮箱", max_length=200, blank=True)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.REQUESTED)
    equipment = models.CharField("车型", max_length=80, blank=True)
    per_truck = models.BooleanField("按车次计价", default=True, help_text="勾选：下面的价格是每车；不勾：是这一段/整个项目的总价")
    valid_until = models.DateField("报价有效期", null=True, blank=True)
    transit = models.CharField("时效", max_length=80, blank=True)
    conditions = models.TextField("条件 / 不含项", blank=True)
    reply_text = models.TextField("承运商回复原文", blank=True)
    requested_at = models.DateTimeField("询价时间", null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["lead", "lane", "id"]

    def __str__(self):
        return self.carrier_label

    @property
    def carrier_label(self):
        return self.carrier.name if self.carrier_id else (self.carrier_name or "（未填承运商）")

    @property
    def total(self):
        return sum((l.amount for l in self.lines.all()), Decimal(0))


class CarrierQuoteLine(models.Model):
    quote = models.ForeignKey(CarrierQuote, on_delete=models.CASCADE, related_name="lines")
    charge_type = models.CharField("费用类型", max_length=12, choices=Charge.Type.choices, default=Charge.Type.LINEHAUL)
    description = models.CharField("说明", max_length=255, blank=True)
    quantity = models.DecimalField("数量", max_digits=10, decimal_places=2, default=Decimal("1"))
    unit_price = models.DecimalField("单价 (USD)", max_digits=12, decimal_places=2, default=Decimal("0"))
    is_estimate = models.BooleanField("视情况发生", default=False, help_text="如 Detention、Layover：报价里写了单价，但不一定发生")

    class Meta:
        ordering = ["id"]

    @property
    def amount(self):
        return (self.quantity or 0) * (self.unit_price or 0) if not self.is_estimate else Decimal(0)


class CustomerQuote(SoftDeleteModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "草稿"
        SENT = "SENT", "已发客户"
        ACCEPTED = "ACCEPTED", "客户接受"
        REJECTED = "REJECTED", "客户拒绝"
        SUPERSEDED = "SUPERSEDED", "已被新版本取代"

    class Style(models.TextChoices):
        SIMPLE = "SIMPLE", "简式：一张价格表"
        FORMAL = "FORMAL", "正式：范围、分段价格、假设与条款、签字确认"

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="customer_quotes")
    version = models.PositiveSmallIntegerField("版本", default=1)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.DRAFT)
    style = models.CharField("PDF 样式", max_length=6, choices=Style.choices, default=Style.SIMPLE)
    valid_until = models.DateField("报价有效期", null=True, blank=True)
    intro = models.TextField("开头说明", blank=True, help_text="英文，印在报价单和邮件开头")
    scope_text = models.TextField("服务范围 Scope of Work", blank=True, help_text="英文，正式报价单用；留空则按需求自动生成")
    assumptions = models.TextField("前提假设 / 不含项", blank=True, help_text="英文，一行一条")
    terms = models.TextField("付款与条款", blank=True, help_text="英文，一行一条")
    show_pdf_totals = models.BooleanField("显示合计", default=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["lead", "-version"]

    def __str__(self):
        return f"{self.lead.number} Quote v{self.version}"

    @property
    def number(self):
        return f"{self.lead.number}-Q{self.version}"

    @property
    def lines_list(self):
        return list(self.lines.all())

    @property
    def total(self):
        return sum((l.amount for l in self.lines.all()), Decimal(0))

    @property
    def cost_total(self):
        return sum((l.cost_amount for l in self.lines.all()), Decimal(0))


class CustomerQuoteLine(models.Model):
    class Unit(models.TextChoices):
        LOAD = "LOAD", "per load"
        TRUCK = "TRUCK", "per truck"
        CNTR = "CNTR", "per container"
        DAY = "DAY", "per day"
        HOUR = "HOUR", "per hour"
        MILE = "MILE", "per mile"
        LUMP = "LUMP", "lump sum"
        EACH = "EACH", "each"

    quote = models.ForeignKey(CustomerQuote, on_delete=models.CASCADE, related_name="lines")
    section = models.CharField("分段标题", max_length=120, blank=True, help_text="大项目可按阶段 / 线路分段，如 Phase 1 – Port Drayage")
    lane = models.ForeignKey(LeadLane, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", verbose_name="对应哪一段")
    charge_type = models.CharField("费用类型", max_length=12, choices=Charge.Type.choices, default=Charge.Type.LINEHAUL)
    description = models.CharField("说明（英文，客户可见）", max_length=255)
    quantity = models.DecimalField("数量", max_digits=10, decimal_places=2, default=Decimal("1"))
    unit = models.CharField("计价单位", max_length=5, choices=Unit.choices, default=Unit.LOAD)
    unit_price = models.DecimalField("售价单价 (USD)", max_digits=12, decimal_places=2, default=Decimal("0"))
    unit_cost = models.DecimalField("成本单价 (USD)", max_digits=12, decimal_places=2, default=Decimal("0"))
    is_optional = models.BooleanField("视情况发生", default=False, help_text="如 Detention、Storage：列出单价，不计入合计")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    @property
    def amount(self):
        return Decimal(0) if self.is_optional else (self.quantity or 0) * (self.unit_price or 0)

    @property
    def cost_amount(self):
        return Decimal(0) if self.is_optional else (self.quantity or 0) * (self.unit_cost or 0)
