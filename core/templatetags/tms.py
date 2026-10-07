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
