from datetime import timedelta
from django.utils import timezone
from desk.models import Baseline, Thread
from desk.reviews import effective_analysis

SIGNALS = {'AUTH', 'AUTH-SCENT', 'AUTH-LONGEVITY', 'AUTH-MARKING', 'AUTH-PACK', 'AUTH-UNKNOWN', 'LONG', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'DELIVERY', 'SERVICE', 'RETURN', 'CONTENT', 'PRICE', 'OTHER'}
PRODUCT_SIGNALS = ['LONG_AUTH', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'RETURN']
MIN_CLASSIFICATION_CONFIDENCE = 0.7


def trusted_analysis(analysis):
    return bool(analysis and analysis.provider not in {'rules', 'demo'} and analysis.confidence >= MIN_CLASSIFICATION_CONFIDENCE)


def signal_label(signal):
    return {'NEGATIVE': 'Общий негатив', 'LONG_AUTH': 'Стойкость и оригинальность', 'SCENT': 'Аромат',
            'PACK': 'Упаковка', 'DEFECT': 'Дефект', 'MARK': 'Маркировка', 'RETURN': 'Возврат',
            'DELIVERY': 'Доставка', 'SERVICE': 'Обслуживание', 'CONTENT': 'Описание товара',
            'PRICE': 'Цена', 'OTHER': 'Другая причина'}.get(signal, signal)


def grouped_signals(signals):
    return sorted({'LONG_AUTH' if signal == 'LONG' or signal.startswith('AUTH') else signal for signal in signals})


def matches(signals, signal):
    if signal == 'LONG_AUTH':
        return any(value in {'LONG', 'LONG_AUTH'} or value.startswith('AUTH') for value in signals)
    return any(x == signal or (signal == 'AUTH' and x.startswith('AUTH-')) for x in signals)


def human_only(result):
    return result['severity'] == 'P0' or any(s.startswith('AUTH') or s in {'LONG_AUTH', 'RETURN', 'MARK'} or (s == 'DEFECT' and result['severity'] in {'P0', 'P1'}) for s in result['signals'])


def is_negative(analysis):
    return bool(analysis and (analysis.sentiment == 'negative' or analysis.hidden_negative))


def product_family(product, scope=None):
    from desk.models import Product
    products = Product.objects.filter(code=product.code)
    if scope is not None: products = products.filter(company__in=scope)
    return products


def evaluate(product, signal, window='100', now=None, scope=None, account_id=None, dataset=None):
    now = now or (dataset.now if dataset else timezone.now())
    from .windows import sales_window
    family = product_family(product, scope) if dataset is None else None
    period = dataset.period(product.code, window, account_id) if dataset else sales_window(family, window, now, account_id)
    total, start = period['redemptions'], period['start']
    if dataset:
        threads = dataset.reviews(product.code, period, account_id)
    else:
        threads = Thread.objects.filter(product__in=family, kind='review', received_at__lte=period['until']).select_related('analysis', 'assessment')
        if account_id is not None: threads = threads.filter(account_id=account_id)
        threads = list(threads.filter(received_at__gte=period['start_at'])) if period['start_at'] else []
    analyses = [(t, effective_analysis(t)) for t in threads]
    classification_missing = any(not trusted_analysis(a) for _, a in analyses)
    selected = [t for t, a in analyses if trusted_analysis(a) and is_negative(a) and (signal == 'NEGATIVE' or matches(a.signals, signal))]
    count = len(selected)
    raw_rate = count / total * 100 if total else None
    rate = round(raw_rate, 2) if raw_rate is not None else None
    baseline = (dataset.baselines.get((product.code, signal)) if dataset else Baseline.objects.filter(product__in=family, signal=signal).order_by('product_id').first()) if account_id is None or signal == 'NEGATIVE' else None
    status, reason = 'WATCH', 'Порог не настроен'
    cluster = False
    cluster_selected = []
    if baseline:
        cluster_start = now - timedelta(days=baseline.cluster_days)
        if dataset:
            recent_source = dataset.threads[product.code]
        else:
            recent_source = Thread.objects.filter(product__in=family, kind='review', received_at__gte=cluster_start, received_at__lte=now).select_related('analysis', 'assessment')
            if account_id is not None: recent_source = recent_source.filter(account_id=account_id)
        recent = [t for t in recent_source if cluster_start <= t.received_at <= now and (account_id is None or t.account_id == account_id)
                  and trusted_analysis(a := effective_analysis(t)) and is_negative(a) and (signal == 'NEGATIVE' or matches(a.signals, signal))]
        if signal == 'NEGATIVE':
            groups = [[t for t in recent if matches(effective_analysis(t).signals, theme)] for theme in PRODUCT_SIGNALS]
            cluster_selected = max(groups, key=len, default=[])
        else:
            cluster_selected = recent
        cluster = len(cluster_selected) >= baseline.cluster_count
        if total < baseline.min_redemptions:
            reason = 'Недостаточная выборка'
        elif raw_rate is not None and (raw_rate >= baseline.high if signal == 'NEGATIVE' else raw_rate > baseline.high) and count >= baseline.min_signals:
            status, reason = 'ANOMALY', 'Достигнут порог расследования' if signal == 'NEGATIVE' else 'Частота выше обычного уровня'
        elif raw_rate is not None and (raw_rate >= baseline.high if signal == 'NEGATIVE' else raw_rate > baseline.high):
            reason = 'Недостаточно сигналов для аномалии'
        elif signal == 'NEGATIVE' and raw_rate is not None and raw_rate >= baseline.low and count >= baseline.min_signals:
            reason = 'Достигнут порог предупреждения'
        else:
            status, reason = 'NORMAL', 'В пределах настроенного порога'
        if cluster: status, reason = 'ANOMALY', 'Кластер похожих сигналов'
    if (period['has_api'] or signal == 'NEGATIVE') and not period['complete']:
        rate, status, reason = None, 'WATCH', '; '.join(period['reasons'])
    if classification_missing:
        rate, status, reason = None, 'WATCH', 'Не все отзывы разобраны; предварительная разметка не подтверждает отсутствие жалоб'
    if cluster:
        status, reason = 'ANOMALY', 'Кластер похожих подтверждённых сигналов'
    evidence = {t.pk: t for t in selected}
    if cluster: evidence.update({t.pk: t for t in cluster_selected})
    return {'signal': signal, 'label': signal_label(signal), 'count': count, 'redemptions': total, 'rate': rate, 'start': start, 'status': status, 'reason': reason, 'baseline': baseline, 'cluster': cluster, 'cluster_count': len(cluster_selected) if cluster else 0,
            'period': period, 'selected': list(evidence.values()), 'account_id': account_id}


def review_summary(product, window='100', now=None, scope=None, account_id=None, dataset=None):
    now = now or (dataset.now if dataset else timezone.now())
    from .windows import sales_window
    if dataset:
        period = dataset.period(product.code, window, account_id)
        reviews = dataset.reviews(product.code, period, account_id)
    else:
        family = product_family(product, scope)
        period = sales_window(family, window, now, account_id)
        reviews = Thread.objects.filter(product__in=family, kind='review', received_at__lte=period['until']).select_related('analysis', 'assessment')
        if account_id is not None: reviews = reviews.filter(account_id=account_id)
        reviews = reviews.filter(received_at__gte=period['start_at']) if period['start_at'] else reviews.none()
    total, classified, negative, preliminary = 0, 0, 0, 0
    for review in reviews:
        total += 1
        analysis = effective_analysis(review)
        if not trusted_analysis(analysis):
            preliminary += bool(analysis and analysis.provider in {'rules', 'demo'})
            continue
        classified += 1
        negative += is_negative(analysis)
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
