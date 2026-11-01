"""Public control topics and the explicitly enabled starter profile."""
TOPICS = [
    ('LONG_AUTH', 'Стойкость и оригинальность'), ('SCENT', 'Аромат'),
    ('PACK', 'Упаковка'), ('DEFECT', 'Дефект'), ('MARK', 'Маркировка'),
    ('RETURN', 'Возврат'), ('CONTENT', 'Описание товара'), ('DELIVERY', 'Доставка'),
    ('SERVICE', 'Обслуживание'), ('PRICE', 'Цена'), ('OTHER', 'Другая причина'),
]
CONTROL_SIGNALS = [signal for signal, _ in TOPICS]
# Preserve the existing overall series and add description mismatch; price,
# unspecified complaints and process topics use their agreed rate thresholds.
SERIES_SIGNALS = {'LONG_AUTH', 'SCENT', 'PACK', 'DEFECT', 'MARK', 'RETURN', 'CONTENT'}
PROCESS_SIGNALS = {'DELIVERY', 'SERVICE'}
PROFILE_VERSION = 'control-topics-1'
PROFILE_RATES = {
    'NEGATIVE': (5, 10), 'LONG_AUTH': (3, 5), 'SCENT': (4, 7), 'PACK': (2, 4),
    'DEFECT': (1, 2), 'MARK': (1, 2), 'RETURN': (3, 5), 'CONTENT': (2, 4),
    'DELIVERY': (3, 6), 'SERVICE': (2, 4), 'PRICE': (5, 10), 'OTHER': (3, 6),
}


def starter_profile():
    return {signal: {
        'warning': low, 'threshold': high, 'min_redemptions': 100,
        'min_signals': min(3, low),
        'cluster_count': 3 if signal in {'DEFECT', 'MARK'} else 4,
        'cluster_days': 7 if signal in {'DEFECT', 'MARK'} else 3,
        'cluster_enabled': signal == 'NEGATIVE' or signal in SERIES_SIGNALS,
    } for signal, (low, high) in PROFILE_RATES.items()}
