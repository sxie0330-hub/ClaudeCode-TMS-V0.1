from django import forms
from django.contrib.auth import get_user_model

from . import units
from .models import (
    CargoLine,
    Carrier,
    Charge,
    Customer,
    Leg,
    Location,
    MasterBL,
    Milestone,
    Profile,
    Project,
    Shipment,
    Unit,
    YardStay,
)


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, **kwargs):
        super().__init__(format="%H:%M", **kwargs)


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


class BaseForm(forms.ModelForm):
    """Date/time pickers everywhere + protection against overwriting someone
    else's edit made after this form was opened."""

    _version = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            if isinstance(f, forms.DateTimeField):
                f.widget = DateTimeInput()
                f.input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]
            elif isinstance(f, forms.DateField):
                f.widget = DateInput()
            elif isinstance(f, forms.TimeField):
                f.widget = TimeInput()
            elif isinstance(f.widget, forms.Textarea):
                f.widget.attrs.setdefault("rows", 3)
        if self.instance.pk and getattr(self.instance, "updated_at", None):
            self.fields["_version"].initial = self.instance.updated_at.isoformat()

    def clean(self):
        data = super().clean()
        inst = self.instance
        if inst.pk and getattr(inst, "updated_at", None):
            current = type(inst).all_objects.filter(pk=inst.pk).values_list("updated_at", flat=True).first()
            sent = self.data.get(self.add_prefix("_version")) or ""
            if current and sent and current.isoformat() != sent:
                raise forms.ValidationError("这条记录在你打开之后已被其他人修改。请刷新页面查看最新内容后再改。")
        return data


class CustomerForm(BaseForm):
    class Meta:
        model = Customer
        fields = ["name", "code", "contact_name", "contact_phone", "contact_email", "notes"]


class CarrierForm(BaseForm):
    class Meta:
        model = Carrier
        fields = ["name", "code", "kind", "scac", "mc_number", "dot_number", "contact_name", "contact_phone", "contact_email", "dispatch_notes"]


class LocationForm(BaseForm):
    class Meta:
        model = Location
        fields = ["code", "name", "kind", "address", "city", "state", "postal_code", "gps", "contact_name", "contact_phone", "hours", "notes"]


class ProjectForm(BaseForm):
    class Meta:
        model = Project
        fields = [
            "company", "customer", "name", "owners", "monday_item_id", "start_date", "end_date", "manual_status",
            "default_shipper", "default_consignee",
            "cargo_description", "un_number", "hazard_class", "nmfc", "freight_class",
            "emergency_contact", "bol_instructions", "detention_free_hours", "detention_rate", "terms", "notes",
        ]
        widgets = {"owners": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owners"].queryset = get_user_model().objects.filter(is_active=True).order_by("first_name", "email")
        self.fields["owners"].label_from_instance = lambda u: u.get_full_name() or u.email or u.username


class ShipmentForm(BaseForm):
    mbl_number = forms.CharField(label="MBL/HBL/Booking#", max_length=40, required=False, help_text="同一 MBL 的柜子会自动关联到同一提单")

    class Meta:
        model = Shipment
        fields = [
            "customer", "project", "service", "monday_item_id", "customer_ref",
            "container_number", "equipment", "seal_number", "lfd",
            "shipper", "consignee", "manual_status", "notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.master_bl_id:
            self.fields["mbl_number"].initial = self.instance.master_bl.number
        self.order_fields(["customer", "project", "service", "monday_item_id", "customer_ref", "mbl_number"])

    def clean(self):
        data = super().clean()
        project = data.get("project")
        if project and data.get("customer") and project.customer_id != data["customer"].pk:
            self.add_error("project", "该项目属于另一个客户")
        return data

    def save(self, commit=True):
        mbl = (self.cleaned_data.get("mbl_number") or "").strip().upper()
        if mbl:
            self.instance.master_bl, _ = MasterBL.objects.get_or_create(number=mbl)
        else:
            self.instance.master_bl = None
        return super().save(commit)


class ShipmentDocsForm(BaseForm):
    """Things that legitimately change after delivery; never locked."""

    class Meta:
        model = Shipment
        fields = ["pod_received", "pod_check", "pod_link", "photos_link", "inspection_link", "other_docs_link",
                  "empty_return_due", "empty_return_date", "empty_return_location"]


class MasterBLForm(BaseForm):
    class Meta:
        model = MasterBL
        fields = ["number", "ocean_carrier", "vessel_voyage", "port_of_discharge", "terminal", "etd", "eta", "ata", "notes"]


class LegForm(BaseForm):
    class Meta:
        model = Leg
        fields = [
            "sequence", "kind", "origin", "destination", "carrier", "carrier_load_number", "equipment",
            "driver_name", "driver_phone", "truck_trailer",
            "scheduled_pickup", "pickup_appt", "pickup_appt_confirmed", "actual_pickup",
            "scheduled_delivery", "delivery_appt", "delivery_appt_confirmed", "actual_delivery",
            "permits_required", "permits_confirmed",
            "pickup_arrived_at", "pickup_departed_at", "delivery_arrived_at", "delivery_departed_at",
            "tracking_url", "last_tracking_at", "bol_number", "bol_instructions", "notes",
        ]

    def clean(self):
        data = super().clean()
        pu, dl = data.get("actual_pickup"), data.get("actual_delivery")
        if dl and not pu:
            self.add_error("actual_pickup", "已填实际送达，请同时填实际提货日期")
        if pu and dl and dl < pu:
            self.add_error("actual_delivery", "送达日期早于提货日期")
        return data


class MilestoneForm(forms.ModelForm):
    class Meta:
        model = Milestone
        fields = ["name", "planned_date", "done_date"]
        widgets = {"planned_date": DateInput(), "done_date": DateInput()}


class MeasureField(forms.Field):
    """Text box that accepts any common unit and stores feet or lbs."""

    def __init__(self, parser, help_text, placeholder, **kwargs):
        self.parser = parser
        kwargs.setdefault("required", False)
        super().__init__(help_text=help_text, widget=forms.TextInput(attrs={"placeholder": placeholder, "inputmode": "decimal"}), **kwargs)

    def prepare_value(self, value):
        return units.plain(value) if value not in (None, "") and not isinstance(value, str) else value

    def to_python(self, value):
        try:
            return self.parser(value)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e


class CargoLineForm(BaseForm):
    weight_lbs = MeasureField(units.parse_weight_lbs, units.WEIGHT_HELP, "如 44000 或 20,000 kg", label="该行总重 (lbs)")
    length_ft = MeasureField(units.parse_length_ft, units.LENGTH_HELP, "如 40 或 13'6\"", label="长 (ft)")
    width_ft = MeasureField(units.parse_length_ft, units.LENGTH_HELP, "如 8.5 或 102 in", label="宽 (ft)")
    height_ft = MeasureField(units.parse_length_ft, units.LENGTH_HELP, "如 9.5 或 2.9 m", label="高 (ft)")

    class Meta:
        model = CargoLine
        fields = [
            "handling_qty", "handling_type", "pieces", "description", "weight_lbs",
            "length_ft", "width_ft", "height_ft",
            "hazmat", "un_number", "hazard_class", "packing_group", "nmfc", "freight_class",
        ]

    def clean(self):
        data = super().clean()
        if data.get("hazmat") and not data.get("un_number"):
            self.add_error("un_number", "危险品必须填 UN 编号")
        return data


class UnitForm(BaseForm):
    class Meta:
        model = Unit
        fields = ["serial_number", "product", "config", "tier", "new_serial_number", "notes"]


class YardStayForm(BaseForm):
    class Meta:
        model = YardStay
        fields = ["yard", "in_date", "out_date", "free_days", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["yard"].queryset = Location.objects.filter(kind__in=[Location.Kind.YARD, Location.Kind.WAREHOUSE])

    def clean(self):
        data = super().clean()
        if data.get("in_date") and data.get("out_date") and data["out_date"] < data["in_date"]:
            self.add_error("out_date", "出场日期早于进场日期")
        return data


class ChargeForm(BaseForm):
    class Meta:
        model = Charge
        fields = ["charge_type", "description", "quantity", "unit_price", "carrier", "invoice_number", "invoice_date", "state", "notes"]

    def __init__(self, *args, side=None, **kwargs):
        super().__init__(*args, **kwargs)
        side = side or self.instance.side
        if side == Charge.Side.AR:
            del self.fields["carrier"]


class NoteForm(forms.Form):
    text = forms.CharField(label="添加备注", widget=forms.Textarea(attrs={"rows": 2, "placeholder": "记录沟通、异常、决定…"}))


class BulkShipmentForm(forms.Form):
    lines = forms.CharField(
        label="柜号列表",
        widget=forms.Textarea(attrs={"rows": 10, "placeholder": "每行一个柜子：柜号, MBL（可选）, 序列号（可选）\nCYMU2523606, ZIMUSHH31566854, TSMG-250613000201"}),
        help_text="可直接从表格复制粘贴（Tab、逗号或空格分隔）。",
    )
    service = forms.ChoiceField(label="服务", choices=Shipment.Service.choices, initial=Shipment.Service.DRAY_STORAGE_OTR)
    equipment = forms.ChoiceField(label="柜型 / 设备", choices=[("", "—")] + list(Shipment.Equipment.choices), required=False)

    def parsed(self):
        import re

        rows = []
        for raw in self.cleaned_data["lines"].splitlines():
            parts = [p.strip() for p in re.split(r"[\t,;]+|\s{2,}| ", raw.strip()) if p.strip()]
            if parts:
                rows.append((parts + ["", "", ""])[:3])
        return rows


class ProfileRoleForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["role"]


User = get_user_model()
