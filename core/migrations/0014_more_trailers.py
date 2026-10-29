"""Tri-axle trailers for heavy 20' BESS and similar loads, plus the 9-axle RGN layout the team uses.
Dimensions from the team's equipment sheets (JKHackl equipment list; RGN layout drawing); tare weights from
manufacturer / dealer listings. All editable in the trailer library."""
from decimal import Decimal

from django.db import migrations

JK = "尺寸来源：公司提供的 JKHackl equipment list（Multi-axle Single Drop：下层 40'、上层 11'、3 轴；Multi-axle Double Drop：凹槽 30'、装货高 2'）。"
STEP_TARE = ("挂车自重来源（48' 三轴钢制阶梯板，经销商 / 厂家标注）：Transcraft 14,769 lbs ±3%（contractorshotline.com）、"
             "BWS 14,875 lbs ±2%（mascus）、Demco DD48-3 12,840 lbs（ballweg.biz）。取 14,800。板高 40\" 为行业常见值，请按实际车改。")
TRI_TRACTOR = ("三轴车头自重 21,000 lbs：来自一则 Peterbilt 389 重载车头挂牌信息（veritread），不是磅单；"
               "双轴 389 实测干重 18,370 lbs（Overdrive）。请按合作车队的磅单改。")
ROWS = [
    dict(key="stepdeck_tri", name="Step Deck tri-axle 三轴阶梯板（双轴车头，共 6 轴）", deck_height_in=Decimal("40"),
         deck_length_ft=Decimal("40"), trailer_length_ft=Decimal("51"), trailer_tare_lbs=14800, tractor_tare_lbs=17000,
         combo_axles=6, sort=25, active=True, notes=JK + "\n" + STEP_TARE),
    dict(key="stepdeck_tri_tritractor", name="Step Deck tri-axle + 三轴车头（共 7 轴）", deck_height_in=Decimal("40"),
         deck_length_ft=Decimal("40"), trailer_length_ft=Decimal("51"), trailer_tare_lbs=14800, tractor_tare_lbs=21000,
         combo_axles=7, sort=26, active=True, notes=JK + "\n" + STEP_TARE + "\n" + TRI_TRACTOR),
    dict(key="doubledrop_tri", name="Double Drop tri-axle 三轴双落（可拆鹅颈，30' 凹槽）", deck_height_in=Decimal("24"),
         deck_length_ft=Decimal("30"), trailer_length_ft=Decimal("53"), trailer_tare_lbs=16350, tractor_tare_lbs=17000,
         combo_axles=6, sort=35, active=True,
         notes=JK + "\n挂车自重 16,350 lbs：XL Specialized 53' 三轴机械可拆双落、34' 凹槽、40 吨的经销商估算空重（mylittlesalesman.com，另有 2,830 lbs 翻转轴未计）。"),
    dict(key="rgn_9axle_team", name="RGN 共 9 轴（三轴车头 + 2 轴 jeep + 2 轴 RGN + 2 轴 booster）", deck_height_in=Decimal("26"),
         deck_length_ft=Decimal("26"), trailer_length_ft=None, trailer_tare_lbs=None, tractor_tare_lbs=21000,
         combo_axles=9, sort=55, active=False,
         notes="资料不全（缺自重），补齐后再打开“参与自动建议”。\n尺寸来源：公司提供的 RGN 车组图：板高 2'2\"、凹槽 26'–30'、离地 8\"–12\"、整车长 105'–115'。"
               "可装长度按最短 26' 填。\n" + TRI_TRACTOR),
]


def add(apps, schema_editor):
    TrailerType = apps.get_model("core", "TrailerType")
    for r in ROWS:
        if not TrailerType.objects.filter(key=r["key"]).exists():
            TrailerType.objects.create(deck_width_in=Decimal("102"), **r)


class Migration(migrations.Migration):
    dependencies = [("core", "0013_seed_trailers")]
    operations = [migrations.RunPython(add, migrations.RunPython.noop)]
