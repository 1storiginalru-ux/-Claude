"""Conservative Russian complaint rules; subject mentions are not complaints.

These rules remain preliminary (confidence=0). They are not a substitute for a
human decision or tested model classification. Never modify the source review.
"""
import re

RULES_VERSION = 'rules-2.0'
PROMPT_VERSION = 'classification-2.0'


def review_sections(thread):
    """Use structured fields only when they describe this exact stored text."""
    if thread.kind != 'review' or not isinstance(thread.source_data, dict):
        return None
    source = thread.source_data
    if thread.account.platform == 'ym':
        description = source.get('description')
        if not isinstance(description, dict): return None
        keys = [('advantages', 'advantages'), ('disadvantages', 'disadvantages'), ('comment', 'comment')]
        source = description
    elif thread.account.platform == 'wb':
        keys = [('text', 'comment'), ('pros', 'advantages'), ('cons', 'disadvantages')]
    else:
        return None
    if any(source.get(key) is not None and not isinstance(source.get(key), str) for key, _ in keys):
        return None
    combined = '\n'.join(source.get(key) or '' for key, _ in keys).strip() or '(Отзыв без текста)'
    if combined != thread.text:
        return None  # An edited text must not inherit old section semantics.
    return {name: source.get(key) or '' for key, name in keys}


def normalize(text):
    return re.sub(r'[^\S\n]+', ' ', text.lower().replace('ё', 'е').replace('−', '-').replace('—', '-')).strip()


# Suppress only explicit negation of a problem, never the word «нет» globally.
NO_PROBLEM = [
    r'\b(?:не|нисколько не)\s+(?:является\s+)?(?:поддел\w*|контрафакт\w*|фальшив\w*)\b(?!\s*(?:ли\b|\?))',
    r'\b(?:нет|никаких нет|не имею)\s+(?:никаких\s+)?(?:сомнений|претензий|подозрений)\b(?:\s+в\s+оригинальности)?',
    r'\b(?:сомнений|подозрений)\s+(?:в\s+оригинальности\s+)?(?:нет|не было)\b',
    r'\bне\s+сомнева\w*\s+в\s+оригинальности\b',
    r'\bне\s+могу\s+(?:назвать|считать)\s+(?:это\s+)?(?:поддел\w*|контрафакт\w*)\b',
    r'\b(?:нет|не было|не возникло)\s+(?:никаких\s+)?(?:проблем|недостатков|претензий|нареканий)\b',
    r'\b(?:без|никаких)\s+(?:проблем|дефектов|повреждений|протечек|задержек|недостатков)\b',
    r'\bне\s+(?:протек\w*|подтек\w*|разбит\w*|поврежден\w*|помят\w*|порван\w*|вскрыт\w*|опоздал\w*)\b',
    r'\bне\s+(?:имеет|было|обнаружено|наш\w*|обнаружил\w*)\s+(?:никаких\s+)?(?:повреждений|дефектов|протечек)\b',
    r'\bне\s+(?:плох\w*|слаб\w*|ужасн\w*)\b',
    r'\bне\s+(?:выветрива\w*|исчеза\w*|пропада\w*)\b',
    r'\bне\s+пахнет\s+(?:спиртом|химией|дешевкой)\b',
    r'\b(?:возврат\w*\s+не\s+(?:понадоб\w*|нуж\w*)|не\s+(?:хочу|нужно|пришлось)\s+возвращать)\b',
    r'\bне\s+дорого\b',
]

PATTERNS = {
    'LONG': [
        r'\b(?:не\s+стойк\w*|нестойк\w*|не\s+держ\w*)\b',
        r'\b(?:нет\s+(?:никакой\s+)?стойкости|стойкост\w*\s+(?:нет|никак\w*|нулев\w*|слаб\w*|плох\w*|низк\w*|отсутств\w*|так себе|не очень))\b',
        r'\bстойкост\w*.{0,25}(?:хотелось бы|хочется).{0,15}(?:лучше|больше)\b',
        r'\b(?:быстро|моментально)\s+(?:выветрива\w*|исчеза\w*|улетучива\w*|пропада\w*)\b',
        r'\b(?:исчез\w*|выветр\w*|пропа\w*|улетуч\w*)\s+(?:уже\s+)?(?:через|за)\s+(?:полчаса|час\w*|один час|два часа|\d+\s*минут\w*)\b',
        r'\bчерез\s+(?:полчаса|час\w*|один час|два часа|\d+\s*минут\w*)\s+(?:(?:аромат|запах)\s+)?(?:исчез\w*|пропа\w*|выветр\w*|не\s+(?:пахнет|чувству\w*))\b',
        r'\b(?:держ\w*|хвата\w*)\s+(?:(?:всего|только|лишь)\s+)?(?:полчаса|(?:[1-5]?\d|60)\s*минут\w*)\b',
        r'\b(?:держ\w*|хвата\w*)\s+(?:всего|только|лишь)\s+(?:час|один час|два часа)\b',
    ],
    'AUTH-UNKNOWN': [
        r'\b(?:поддел\w*|контрафакт\w*|фальшив\w*|неоригинал\w*)\b',
        r'\bне\s+(?:похож\w*\s+на\s+)?оригинал\w*\b',
        r'\bне\s+могу\s+(?:назвать|считать)\s+(?:это\s+)?оригинал\w*\b',
        r'\b(?:сомнен\w*|сомнева\w*|подозрен\w*)\b.{0,45}\bоригинальност\w*\b',
        r'\bоригинальност\w*.{0,20}(?:под вопросом|сомнительн\w*|вызыва\w* сомнен\w*)\b',
        r'\bоригинал\s*\?',
    ],
    'DEFECT': [
        r'\b(?:протек\w*|подтек\w*|течет|разбит\w*|сломал\w*)\b',
        r'\bпустой\s+флакон\b',
        r'\b(?:распылител\w*|дозатор\w*|помп\w*|пульверизатор\w*).{0,25}\bне\s+(?:работа\w*|нажима\w*|распыля\w*)\b',
        r'\bне\s+(?:работа\w*|нажима\w*)\s+(?:распылител\w*|дозатор\w*|помп\w*|пульверизатор\w*)\b',
        r'\b(?:нет|отсутствует|не было)\s+(?:крышк\w*|колпачк\w*|распылител\w*)\b',
    ],
    'PACK': [
        r'\b(?:упаков\w*|короб\w*|пленк\w*).{0,40}\b(?:помят\w*|мят\w*|поврежден\w*|порван\w*|вскрыт\w*|мокр\w*|разорв\w*|грязн\w*|плох\w*|ужасн\w*)\b',
        r'\b(?:помят\w*|мят\w*|поврежден\w*|порван\w*|вскрыт\w*|мокр\w*|грязн\w*|плох\w*|ужасн\w*|нет|без).{0,25}\b(?:упаков\w*|короб\w*|пленк\w*)\b',
        r'\b(?:упаковк\w*|коробк\w*|пленк\w*)\s+(?:нет|отсутств\w*)\b',
        r'\bне\s+(?:очень\s+)?(?:хорош\w*|качествен\w*)\s+(?:упаковк\w*|коробк\w*)\b',
        r'\b(?:упаковк\w*|коробк\w*)\s+не\s+(?:очень\s+)?(?:хорош\w*|качествен\w*)\b',
    ],
    'RETURN': [
        r'\b(?:хочу|хотел\w*|прошу|требую|можно ли|как|пришлось)\s+(?:оформить\s+)?(?:вернуть|возврат\w*)\b',
        r'\b(?:оформил\w*|оформля\w*|сделал\w*)\s+возврат\w*\b',
        r'\bвернул\w*\s+(?:товар|парфюм|флакон|заказ)\b',
    ],
    'MARK': [
        r'\b(?:нет|отсутствует|не наш\w*|не вижу).{0,20}\b(?:маркиров\w*|datamatrix|датаматрикс\w*|дата.?матрикс\w*)\b',
        r'\b(?:маркиров\w*|datamatrix|датаматрикс\w*|дата.?матрикс\w*|код).{0,25}\bне\s+(?:чита\w*|сканир\w*|пробива\w*|совпада\w*)\b',
    ],
    'DELIVERY': [
        r'\b(?:достав\w*|привез\w*).{0,25}\b(?:поздно|долго|долгая|задерж\w*|опозда\w*|ужасн\w*|плох\w*)\b',
        r'\b(?:долгая|поздняя|задерж\w*|ужасная|плохая).{0,20}\bдоставк\w*\b',
        r'\b(?:опоздал\w*|не\s+доставил\w*|не\s+привез\w*|заказ\s+потерял\w*)\b',
    ],
    'SCENT': [
        r'\bне\s+(?:нравится|понравил\w*)\s+(?:мне\s+)?(?:запах|аромат)\b',
        r'\bне\s+тот\s+(?:запах|аромат)\b',
        r'\b(?:запах|аромат)\s+(?:совсем\s+)?(?:другой|слаб\w*|неприятн\w*|отлича\w*)\b',
        r'\b(?:нет\s+(?:никакого\s+)?(?:запаха|аромата)|не\s+пахнет)\b',
        r'\bпахнет\s+(?:одним\s+)?(?:спиртом|водой|химией)\b',
    ],
    'SERVICE': [r'\b(?:хамство|нахамил\w*|не\s+отвеча\w*)\b', r'\b(?:грубый\s+продавец|плохое\s+обслуживание)\b'],
    'PRICE': [r'\b(?:слишком\s+дорого|дороговато|неоправданно\s+дорого|цен\w*\s+завышен\w*)\b'],
}
COMPILED = {signal: [re.compile(pattern) for pattern in patterns] for signal, patterns in PATTERNS.items()}
SUPPRESS = [re.compile(pattern) for pattern in NO_PROBLEM]
EMPTY_CONS = re.compile(r'^(?:нет|нету|неа|отсутствуют?|не обнаружен\w*|не выявлен\w*|не наш\w*|не заметил\w*|никаких|без недостатков|(?:недостатков|минусов|претензий) нет)(?:\s|[.!,:),;\-])*$', re.I)
GENERIC_COMPLAINT = re.compile(r'\b(?:не рекомендую|не советую|разочаров\w*|не понравил\w*|ужасн\w*|отвратительн\w*)\b')
CONS_TOPICS = {
    'LONG': r'\bстойкост\w*\b', 'PACK': r'\b(?:упаков\w*|короб\w*)\b',
    'DELIVERY': r'\bдоставк\w*\b', 'SCENT': r'\b(?:запах|аромат)\b',
    'PRICE': r'\b(?:цен\w*|дорого)\b',
    'RETURN': r'\bвозврат\w*\b',
}


def no_disadvantages(text):
    value = normalize(text).strip(' .!,;:()-')
    if not value: return True
    if EMPTY_CONS.fullmatch(value): return True
    return bool(re.fullmatch(r'(?:нет|не было|не наш\w*|не обнаружил\w*|не заметил\w*)(?:\s+(?:никаких\s+)?(?:недостатков|минусов))?(?:\s+и\s+быть\s+не\s+может)?(?:[, .]+не\s+первый\s+раз\s+заказываю)?', value))


def labeled_sections(text):
    names = {'достоинства': 'advantages', 'плюсы': 'advantages', 'недостатки': 'disadvantages', 'минусы': 'disadvantages', 'комментарий': 'comment'}
    matches = list(re.finditer(r'(?im)(?:^|[\n;])\s*(достоинства|плюсы|недостатки|минусы|комментарий)\s*:', text))
    if not matches: return {'comment': text}
    fields = {'comment': text[:matches[0].start()]}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        name = names[match.group(1).lower()]
        fields[name] = '\n'.join([fields.get(name, ''), text[match.end():end]]).strip()
    return fields


def local_classify(text, rating, *, sections=None, kind='review'):
    fields = sections if isinstance(sections, dict) else labeled_sections(text)
    signals, complaint = set(), False
    for name in ['advantages', 'disadvantages', 'comment']:
        value = normalize(fields.get(name) or '')
        if not value: continue
        if name == 'disadvantages' and no_disadvantages(value): continue
        # Keep clause boundaries: «упаковка отличная. Нет стойкости» is not PACK.
        clauses = re.split(r'(?<=[\n.!?;,])|\b(?:но|однако|зато|хотя|а)\b', value)
        for clause in clauses:
            # Explicit doubt questions need recognition before punctuation is removed.
            doubtful_auth = bool(re.search(r'\bне\s+поддел\w*\s+ли\b', clause))
            original_clause = clause
            for pattern in SUPPRESS: clause = pattern.sub(' ', clause)
            found = {signal for signal, patterns in COMPILED.items() if any(pattern.search(clause) for pattern in patterns)}
            if doubtful_auth: found.add('AUTH-UNKNOWN')
            if name == 'disadvantages' and not found and clause == original_clause:
                # A topic alone in the cons field conveys a concern; the same word
                # in advantages or unstructured text does not.
                words = clause.strip(' ,:-')
                if len(words.split()) <= 3 and not re.search(r'\b(?:хорош\w*|отличн\w*|прекрасн\w*|нрав\w*|быстр\w*|аккуратн\w*|приятн\w*|идеальн\w*|нормальн\w*|целая|целый|целое|стойкий|стойкая|супер|нет|без)\b', words):
                    found.update(s for s, pattern in CONS_TOPICS.items() if re.search(pattern, words))
            signals.update(found)
            complaint |= bool(found or GENERIC_COMPLAINT.search(clause))
    negative = complaint or (rating is not None and rating <= 3)
    positive = not negative and ((rating is not None and rating >= 4) or bool(re.search(r'\b(?:отличн\w*|нравится|спасибо|супер|хорош\w*|норм)\b', normalize(text))))
    if complaint and not signals: signals.add('OTHER')
    objective = 'DEFECT' in signals
    return {
        'sentiment': 'negative' if negative else 'positive' if positive else 'neutral',
        'hidden_negative': bool(kind == 'review' and rating and rating >= 4 and complaint),
        'signals': sorted(signals),
        'severity': 'P1' if objective or any(s.startswith('AUTH') for s in signals) else 'P2' if negative else 'P3',
        'objective_issue': objective, 'requires_human': True, 'confidence': 0.0,
        'summary': 'Предварительная разметка по фразам и полям отзыва. Подтвердите или исправьте решение оператора.',
        'draft': '',
    }
