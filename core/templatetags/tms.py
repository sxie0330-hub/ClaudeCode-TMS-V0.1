from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(value):
    if value in (None, ""):
        return "—"
    value = Decimal(value)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


@register.filter
def dash(value):
    return "—" if value in (None, "", []) else value


@register.filter
def qty(value):
    if value in (None, ""):
        return ""
    value = Decimal(value)
    return f"{value.quantize(Decimal(1)):f}" if value == value.to_integral() else f"{value.normalize():f}"


@register.filter
def get_item(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def in_ft(value):
    """Inches -> 13'6" (for OOG heights and widths)."""
    from core.oog import in_ft as conv
    return conv(value) if value not in (None, "") else "?"


@register.filter
def pdate(value):
    """A (date, planned?) pair from Project.shown_start / shown_end: planned dates are greyed and marked 计划."""
    from django.utils.html import format_html

    day, planned = value if value else (None, False)
    if not day:
        return "—"
    if planned:
        return format_html('<span class="muted" title="来自 Load 的计划日期">计划 {}</span>', day.strftime("%Y-%m-%d"))
    return day.strftime("%Y-%m-%d")
