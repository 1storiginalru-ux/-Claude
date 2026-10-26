from datetime import timedelta
from django.utils import timezone
from desk.models import Baseline, Thread

SIGNALS = {'AUTH', 'AUTH-SCENT', 'AUTH-LONGEVITY', 'AUTH-MARKING', 'AUTH-PACK', 'AUTH-UNKNOWN', 'LONG', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'DELIVERY', 'SERVICE', 'RETURN', 'CONTENT', 'PRICE', 'OTHER'}
PRODUCT_SIGNALS = ['LONG_AUTH', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'RETURN']
MIN_CLASSIFICATION_CONFIDENCE = 0.7


def trusted_analysis(analysis):
    return bool(analysis and analysis.provider not in {'rules', 'demo'} and analysis.confidence >= MIN_CLASSIFICATION_CONFIDENCE)


def signal_label(signal):
    return {'LONG_AUTH': 'Стойкость и оригинальность'}.get(signal, signal)


def grouped_signals(signals):
    return sorted({'LONG_AUTH' if signal == 'LONG' or signal.startswith('AUTH') else signal for signal in signals})


def matches(signals, signal):
    if signal == 'LONG_AUTH':
        return any(value == 'LONG' or value.startswith('AUTH') for value in signals)
    return any(x == signal or (signal == 'AUTH' and x.startswith('AUTH-')) for x in signals)


def human_only(result):
    return result['severity'] == 'P0' or any(s.startswith('AUTH') or s in {'RETURN', 'MARK'} or (s == 'DEFECT' and result['severity'] in {'P0', 'P1'}) for s in result['signals'])


def product_family(product, scope=None):
    from desk.models import Product
    products = Product.objects.filter(code=product.code)
    if scope is not None: products = products.filter(company__in=scope)
    return products


def evaluate(product, signal, window='100', now=None, scope=None, account_id=None):
    now = now or timezone.now()
    from .windows import sales_window
    family = product_family(product, scope)
    period = sales_window(family, window, now, account_id)
    total, start = period['redemptions'], period['start']
    threads = Thread.objects.filter(product__in=family, kind='review', received_at__lte=period['until']).select_related('analysis')
    if account_id is not None: threads = threads.filter(account_id=account_id)
    if period['start_at']: threads = threads.filter(received_at__gte=period['start_at'])
    else: threads = threads.none()
    threads = list(threads)
    classification_missing = any(not trusted_analysis(getattr(t, 'analysis', None)) for t in threads)
    selected = [t for t in threads if trusted_analysis(getattr(t, 'analysis', None)) and matches(t.analysis.signals, signal)]
    count = len(selected)
    rate = round(count / total * 100, 2) if total else None
    baseline = None if account_id is not None else Baseline.objects.filter(product__code=product.code, signal=signal).order_by('product_id').first()
    status, reason = 'WATCH', 'Baseline не настроен'
    cluster = False
    if baseline:
        cluster = sum(t.received_at >= now - timedelta(days=baseline.cluster_days) for t in selected) >= baseline.cluster_count
        if total < baseline.min_redemptions:
            reason = 'Недостаточная выборка'
        elif rate is not None and rate > baseline.high and count >= baseline.min_signals:
            status, reason = 'ANOMALY', 'Частота выше baseline'
        elif rate is not None and rate > baseline.high:
            reason = 'Недостаточно сигналов для аномалии'
        else:
            status, reason = 'NORMAL', 'В пределах baseline'
        if cluster: status, reason = 'ANOMALY', 'Кластер похожих сигналов'
    if period['has_api'] and not period['complete']:
        rate, status, reason = None, 'WATCH', '; '.join(period['reasons'])
    if classification_missing:
        rate, status, reason = None, 'WATCH', 'Не все отзывы разобраны; предварительная разметка не подтверждает отсутствие жалоб'
    if cluster:
        status, reason = 'ANOMALY', 'Кластер похожих подтверждённых сигналов'
    return {'signal': signal, 'label': signal_label(signal), 'count': count, 'redemptions': total, 'rate': rate, 'start': start, 'status': status, 'reason': reason, 'baseline': baseline, 'cluster': cluster, 'period': period}


def review_summary(product, window='100', now=None, scope=None, account_id=None):
    now = now or timezone.now()
    from .windows import sales_window
    family = product_family(product, scope)
    period = sales_window(family, window, now, account_id)
    reviews = Thread.objects.filter(product__in=family, kind='review', received_at__lte=period['until']).select_related('analysis')
    if account_id is not None: reviews = reviews.filter(account_id=account_id)
    if period['start_at']:
        reviews = reviews.filter(received_at__gte=period['start_at'])
    else:
        reviews = reviews.none()
    total, classified, negative, preliminary = 0, 0, 0, 0
    for review in reviews:
        total += 1
        analysis = getattr(review, 'analysis', None)
        if not trusted_analysis(analysis):
            preliminary += bool(analysis and analysis.provider in {'rules', 'demo'})
            continue
        classified += 1
        negative += bool(analysis.sentiment == 'negative' or analysis.hidden_negative)
    reviews_complete = (bool(period['accounts']) and not period['feedback_reasons']
                        and (period['target'] is None or period['redemptions'] >= period['target'])
                        and classified == total)
    complete = period['complete'] and reviews_complete
    return {'period': period, 'total': total, 'classified': classified, 'unclassified': total - classified,
            'preliminary': preliminary, 'negative': negative,
            'review_rate': round(negative / total * 100, 2) if reviews_complete and total else None,
            'classified_rate': round(negative / classified * 100, 2) if classified else None,
            'per100': round(negative / period['redemptions'] * 100, 2) if complete and period['redemptions'] else None,
            'complete': complete}
