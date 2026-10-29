"""Team 9-axle RGN (tri-axle tractor + 2-axle jeep + 2-axle RGN + 2-axle booster): 65-ton class rating and permit note."""
from django.db import migrations

NOTE = ("\n\n额定载重 130,000 lbs = 65 美吨（short ton，约 59 公吨）。厂家吨位是美吨，不是公吨。"
        "依据：Trail King / XL Specialized 的 2+2+2（jeep + RGN + booster）挂牌多为 60–65 吨级（TK120HDG 2+2+2 60 吨；XL130HDG 2+2+2 65 吨）；"
        "80 吨级一般要三轴组（如 Cozad 80-Ton 9-Axle 为 3+3+3、Brandt / Trail King 2+3+2 为 65 吨）。以自有车铭牌为准。"
        "\n这套组合空车也超出法定尺寸 / 轴数，上路一定要办 permit。")


def update(apps, schema_editor):
    TrailerType = apps.get_model("core", "TrailerType")
    t = TrailerType.objects.filter(key="rgn_9axle_team").first()
    if t and not t.rated_capacity_lbs:
        t.rated_capacity_lbs = 130000
        t.name = "RGN 共 9 轴 65 吨级（三轴车头 + 2 轴 jeep + 2 轴 RGN + 2 轴 booster，Trail King / Cozad）"[:80]
        t.notes = (t.notes or "") + NOTE
        t.save()


class Migration(migrations.Migration):
    dependencies = [("core", "0014_more_trailers")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
