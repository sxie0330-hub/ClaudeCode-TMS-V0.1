"""Weights for the 9-axle and 4+4 8-axle combinations, from the material the team supplied (2026-10-30)."""
from decimal import Decimal

from django.db import migrations

NINE = ("\n\n2026-10-30 按公司提供的资料更新（9 Axle Trailer Dimensions）：整车空车重 65,000 lbs（这里拆成车头 21,000 + 挂车组 44,000）、"
        "下层板 25'、板高 18\"–24\"（公司车组图为 2'2\"，取较高的 26\" 偏保守）、整车长约 103'、货重一般 100k–110k，许可上限约 105,000 lbs。"
        "额定载重按 105,000 lbs 货重填。")
EIGHT = ("\n\n2026-10-30 按公司提供的资料更新：8 轴重载车组空车约 45,000–60,000+ lbs，取中间约 52,000（四轴车头 22,000 + 挂车 30,000）；"
         "四轴车头空车 18,000–22,000 lbs，取 22,000 偏保守。资料来源是网络汇总（引用 ATS Inc、DeBary Truck Sales 等），请以车队磅单为准。"
         "板高和可装长度仍缺，装车后高度暂不能算。")


def update(apps, schema_editor):
    TrailerType = apps.get_model("core", "TrailerType")
    t = TrailerType.objects.filter(key="rgn_9axle_team").first()
    if t and t.trailer_tare_lbs is None:
        t.trailer_tare_lbs, t.tractor_tare_lbs = 44000, 21000
        t.deck_length_ft, t.deck_height_in = Decimal("25"), Decimal("26")
        t.trailer_length_ft = Decimal("83")  # overall ~103' = trailer group + ~20' tractor
        t.rated_capacity_lbs, t.active = 105000, True
        t.notes = (t.notes or "").replace("资料不全（缺自重），补齐后再打开“参与自动建议”。\n", "") + NINE
        t.save()
    e = TrailerType.objects.filter(key="axle8_4x4").first()
    if e and e.trailer_tare_lbs is None:
        e.trailer_tare_lbs, e.tractor_tare_lbs = 30000, 22000
        e.notes = (e.notes or "") + EIGHT
        e.save()


class Migration(migrations.Migration):
    dependencies = [("core", "0018_pod_upload")]
    operations = [migrations.RunPython(update, migrations.RunPython.noop)]
