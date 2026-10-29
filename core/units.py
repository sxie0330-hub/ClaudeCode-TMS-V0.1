"""Cargo measurements: the TMS stores lengths in feet and weights in lbs.

People (and emails) write them every which way: 13'6", 162 in, 4.1 m,
35,000 kg, 20 t. These parsers turn any of those into feet / lbs. A bare
number means the TMS unit (ft or lbs).
"""
import re
from decimal import Decimal, InvalidOperation

FT = Decimal("0.01")
LBS = Decimal("0.1")

# unit -> feet per unit
_LENGTH = {
    "ft": Decimal(1), "feet": Decimal(1), "foot": Decimal(1), "'": Decimal(1), "’": Decimal(1), "英尺": Decimal(1), "尺": Decimal(1),
    "in": Decimal(1) / 12, "inch": Decimal(1) / 12, "inches": Decimal(1) / 12, '"': Decimal(1) / 12, "”": Decimal(1) / 12,
    "''": Decimal(1) / 12, "英寸": Decimal(1) / 12,
    "m": Decimal("3.280839895"), "meter": Decimal("3.280839895"), "meters": Decimal("3.280839895"),
    "metre": Decimal("3.280839895"), "metres": Decimal("3.280839895"), "米": Decimal("3.280839895"),
    "cm": Decimal("0.03280839895"), "厘米": Decimal("0.03280839895"),
    "mm": Decimal("0.003280839895"), "毫米": Decimal("0.003280839895"),
}
# unit -> lbs per unit. "ton" in US freight means a short ton (2,000 lbs);
# t / mt / tonne / 吨 is a metric ton (1,000 kg).
_WEIGHT = {
    "lb": Decimal(1), "lbs": Decimal(1), "#": Decimal(1), "pound": Decimal(1), "pounds": Decimal(1), "磅": Decimal(1),
    "kg": Decimal("2.20462262"), "kgs": Decimal("2.20462262"), "kilo": Decimal("2.20462262"), "kilos": Decimal("2.20462262"),
    "kilogram": Decimal("2.20462262"), "kilograms": Decimal("2.20462262"), "公斤": Decimal("2.20462262"), "千克": Decimal("2.20462262"),
    "t": Decimal("2204.62262"), "mt": Decimal("2204.62262"), "tonne": Decimal("2204.62262"), "tonnes": Decimal("2204.62262"),
    "metric ton": Decimal("2204.62262"), "metric tons": Decimal("2204.62262"), "吨": Decimal("2204.62262"), "公吨": Decimal("2204.62262"),
    "ton": Decimal(2000), "tons": Decimal(2000), "st": Decimal(2000), "short ton": Decimal(2000), "short tons": Decimal(2000),
}

_NUM = r"(\d+(?:\.\d+)?|\.\d+)"
LENGTH_HELP = "数字默认是 ft；也可以写 13'6\"、162 in、4.1 m、410 cm"
WEIGHT_HELP = "数字默认是 lbs；也可以写 15,000 kg、20 t（公吨）、10 ton（短吨 2,000 lbs）"


def _num(s):
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"不是数字：{s}") from e


def _clean(text):
    s = str(text or "").strip().lower().replace(",", "")
    return re.sub(r"\s+", " ", s)


def parse_length_ft(text):
    """'13\\'6"' / '162 in' / '4.1m' / '13.5' -> Decimal feet (2 decimals). None if blank."""
    s = _clean(text)
    if not s:
        return None
    # feet + inches: 13'6", 13' 6, 13'-6", 13 ft 6 in, 13ft6in
    m = re.fullmatch(rf"{_NUM}\s*(?:'|’|ft|feet|foot)\s*-?\s*{_NUM}\s*(?:\"|”|''|in|inch|inches)?", s)
    if m:
        ft, inch = _num(m.group(1)), _num(m.group(2))
        if inch >= 12:
            raise ValueError(f"英寸部分应小于 12：{text}")
        return (ft + inch / 12).quantize(FT)
    m = re.fullmatch(rf"{_NUM}\s*(.*)", s)
    if not m:
        raise ValueError(f"看不懂这个尺寸：{text}")
    unit = m.group(2).strip().rstrip(".")
    if unit and unit not in _LENGTH:
        raise ValueError(f"不认识的长度单位「{m.group(2)}」：{text}")
    return (_num(m.group(1)) * (_LENGTH[unit] if unit else 1)).quantize(FT)


def parse_weight_lbs(text):
    """'35,000 kg' / '20 t' / '44000' -> Decimal lbs (1 decimal). None if blank."""
    s = _clean(text)
    if not s:
        return None
    m = re.fullmatch(rf"{_NUM}\s*(.*)", s)
    if not m:
        raise ValueError(f"看不懂这个重量：{text}")
    unit = m.group(2).strip().rstrip(".")
    if unit and unit not in _WEIGHT:
        raise ValueError(f"不认识的重量单位「{m.group(2)}」：{text}")
    return (_num(m.group(1)) * (_WEIGHT[unit] if unit else 1)).quantize(LBS)


def plain(d):
    """Decimal('96.00') -> '96', Decimal('13.50') -> '13.5'; '' for None."""
    if d is None or d == "":
        return ""
    d = Decimal(d)
    return f"{d.quantize(Decimal(1)):f}" if d == d.to_integral() else f"{d.normalize():f}"
