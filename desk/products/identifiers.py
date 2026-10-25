"""Marketplace identifiers must not be confused with seller article numbers."""
import re


def wb_nm_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ''
    value = str(value).strip()
    if not re.fullmatch(r'[0-9]{1,120}', value) or not int(value):
        return ''
    return str(int(value))
