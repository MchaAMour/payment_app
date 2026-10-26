from django import template

register = template.Library()


@register.filter
def money(minor):
    return f"${(minor or 0) / 100:,.2f}"
