"""8-axle 4+4 combination (4-axle tractor + 4-axle trailer) for the heavy-BESS rule. No published spec was found:
deck height, deck length and tare are left for the team to fill in."""
from django.db import migrations


def add(apps, schema_editor):
    TrailerType = apps.get_model("core", "TrailerType")
    if not TrailerType.objects.filter(key="axle8_4x4").exists():
        TrailerType.objects.create(
            key="axle8_4x4", name="4+4 共 8 轴（四轴车头 + 四轴挂车）", combo_axles=8, sort=58, active=False,
            tractor_tare_lbs=17000,
            notes=("公司规则：20' BESS ≥ 90,000 lbs、路线不经过 CA 时用这套（每轴重量要符合沿途各州规定，不符合就改用 9 轴）。\n"
                   "资料不全：请填板高、可装长度、挂车自重，并把车头自重改成四轴车头的实际磅单重量（17,000 只是默认值）。"
                   "这套组合空车也要 permit。"))


class Migration(migrations.Migration):
    dependencies = [("core", "0015_team_rgn_capacity")]
    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
