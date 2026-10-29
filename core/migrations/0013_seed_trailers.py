"""Seed the trailer library from core/data/trailer_specs.json (manufacturer / dealer spec sheets, with sources).
Entries missing deck height, deck length or tare are added switched off until the team fills them in."""
import json
from decimal import Decimal
from pathlib import Path

from django.db import migrations

DATA = Path(__file__).resolve().parent.parent / "data" / "trailer_specs.json"
SORT = {"flatbed48_steel": 10, "flatbed48_alum": 11, "flatbed53_steel": 12, "flatbed53_alum": 13, "stepdeck48": 20,
        "stepdeck53": 21, "doubledrop_fixed": 30, "rgn_2axle": 40, "rgn_3axle": 50, "rgn_9axle": 60, "rgn_13axle": 70,
        "ext_flatbed": 80, "ext_stepdeck": 81, "ext_doubledrop": 82, "perimeter_frame": 90, "beam_dual_lane": 95,
        "conestoga53": 98, "dryvan53": 99}
ENCLOSED = {"dryvan53", "conestoga53"}
# Flatbeds and step decks: overall trailer length is the nominal length.
OVERALL = {"flatbed48_steel": 48, "flatbed48_alum": 48, "flatbed53_steel": 53, "flatbed53_alum": 53, "stepdeck48": 48, "stepdeck53": 53}


def dec(v):
    return None if v is None else Decimal(str(v))


def seed(apps, schema_editor):
    TrailerType = apps.get_model("core", "TrailerType")
    for t in json.loads(DATA.read_text(encoding="utf-8")):
        key = t["key"]
        if TrailerType.objects.filter(key=key).exists():
            continue
        width = t.get("deck_width_in")
        complete = all(t.get(k) is not None for k in ("deck_height_in", "deck_length_ft", "trailer_tare_lbs", "tractor_tare_lbs"))
        notes = (t.get("notes") or "").strip()
        if t.get("sources"):
            notes += "\n\n来源：\n" + "\n".join(f"- {s}" for s in t["sources"])
        if not complete:
            notes = "资料不全（缺板高 / 长度 / 自重），补齐后再打开“参与自动建议”。\n\n" + notes
        TrailerType.objects.create(
            key=key, name=t["name"][:80], deck_height_in=dec(t.get("deck_height_in")), deck_length_ft=dec(t.get("deck_length_ft")),
            deck_width_in=dec(width if isinstance(width, (int, float)) else 102), trailer_tare_lbs=t.get("trailer_tare_lbs"),
            tractor_tare_lbs=t.get("tractor_tare_lbs") or 17000, trailer_length_ft=dec(OVERALL.get(key)),
            max_extended_length_ft=dec(t.get("max_extended_length_ft")), rated_capacity_lbs=t.get("rated_capacity_lbs"),
            combo_axles=t.get("combo_axles"), enclosed=key in ENCLOSED, sort=SORT.get(key, 50),
            active=complete and key not in ENCLOSED, notes=notes)


class Migration(migrations.Migration):
    dependencies = [("core", "0012_oog_library")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
