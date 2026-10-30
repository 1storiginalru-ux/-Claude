"""Local greeting substitution. Never infers a name from the review text."""
import re
import unicodedata

GENERIC = {'покупатель', 'пользователь', 'клиент', 'аноним', 'анонимный', 'неизвестно',
           'скрыто', 'имя', 'нет', 'удален', 'удалён', 'гость', 'wildberries', 'ozon',
           'яндекс', 'anonymous', 'unknown', 'user', 'buyer', 'customer', 'guest', 'deleted'}


def marketplace_author(subject):
    source = getattr(subject, 'source_data', None) or {}
    if not isinstance(source, dict): source = {}
    key = {'wb': 'userName', 'ym': 'author', 'ozon': 'author_name'}.get(subject.account.platform)
    if key and key in source:
        author = source[key]
        if isinstance(author, dict): author = author.get('name', '')
        return author if isinstance(author, str) else ''
    return getattr(subject, 'customer', '') or ''


def customer_name(subject):
    raw = unicodedata.normalize('NFC', marketplace_author(subject)).strip()
    if not raw or len(raw) > 120: return ''
    parts = raw.split()
    if len(parts) > 3: return ''
    name = parts[0]
    # Reject addresses, nicknames with digits/symbols and generic marketplace labels.
    if any(not (ch.isalpha() or ch in " -'.’") for ch in raw): return ''
    if any(part.strip('.').casefold() in GENERIC for part in parts): return ''
    components = re.split("[-'’]", name)
    if not all(2 <= len(piece) <= 30 and piece.isalpha() and piece[0].isupper() and piece[1:].islower() for piece in components): return ''
    for part in parts[1:]:
        if re.fullmatch(r'[^\W\d_]\.', part): continue
        if not part[0].isupper() or not all(ch.isalpha() or ch in "-'’" for ch in part): return ''
    return name


def greeting(subject):
    name = customer_name(subject)
    return f'Здравствуйте, {name}!' if name else 'Здравствуйте!'


def apply_greeting(text, subject):
    hello = greeting(subject)
    text = text.replace('{{имя_клиента}}', customer_name(subject))
    if '{{приветствие}}' in text: return text.replace('{{приветствие}}', hello)
    # Keep the approved body byte-for-byte, replacing only its opening greeting.
    opening = re.match(r'\AЗдравствуйте(?:,[^!\n?.]{1,80})?[!.]', text)
    if opening: return hello + text[opening.end():]
    if text.startswith('Здравствуйте\n') or text == 'Здравствуйте':
        return hello + text[len('Здравствуйте'):]
    return hello + '\n\n' + text
