from decimal import Decimal

from django.db import migrations, models

DIMS = ("length", "width", "height")


def inches_to_feet(apps, schema_editor):
    for name in ("CargoLine", "HistoricalCargoLine"):
        model = apps.get_model("core", name)
        for row in model.objects.exclude(length_ft=None, width_ft=None, height_ft=None).iterator():
            for d in DIMS:
                v = getattr(row, f"{d}_ft")
                if v is not None:
                    setattr(row, f"{d}_ft", (Decimal(v) / 12).quantize(Decimal("0.01")))
            row.save(update_fields=[f"{d}_ft" for d in DIMS])


def feet_to_inches(apps, schema_editor):
    for name in ("CargoLine", "HistoricalCargoLine"):
        model = apps.get_model("core", name)
        for row in model.objects.exclude(length_ft=None, width_ft=None, height_ft=None).iterator():
            for d in DIMS:
                v = getattr(row, f"{d}_ft")
                if v is not None:
                    setattr(row, f"{d}_ft", (Decimal(v) * 12).quantize(Decimal("0.1")))
            row.save(update_fields=[f"{d}_ft" for d in DIMS])


def _fields(model):
    ops = []
    for d, label in (("length", "长"), ("width", "宽"), ("height", "高")):
        ops.append(migrations.RenameField(model_name=model, old_name=f"{d}_in", new_name=f"{d}_ft"))
        ops.append(migrations.AlterField(
            model_name=model, name=f"{d}_ft",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=7, null=True, verbose_name=f"{label} (ft)"),
        ))
    return ops


class Migration(migrations.Migration):
    """Cargo dimensions are kept in feet (weights stay in lbs)."""

    dependencies = [("core", "0008_ai_fill_in")]

    operations = _fields("cargoline") + _fields("historicalcargoline") + [
        migrations.RunPython(inches_to_feet, feet_to_inches),
    ]
