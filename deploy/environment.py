"""Read the simple, literal KEY=value subset used by our systemd env file."""
import re
import shlex
from pathlib import Path


def read_environment(path):
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            raise ValueError(f'Проверьте строку {number} файла настроек.')
        parts = shlex.split(value, comments=False)
        if len(parts) > 1:
            raise ValueError(f'Строка {number}: заключите значение с пробелами в кавычки.')
        values[key] = parts[0] if parts else ''
    return values
