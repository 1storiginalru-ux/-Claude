from datetime import timedelta
from desk.reviews import effective_analysis
from .rules import PRODUCT_SIGNALS, evaluate, review_summary, trusted_analysis


def product_queue(products, dataset, incidents):
    by_code = {}
    for incident in incidents:
        by_code.setdefault(incident.product.code, []).append(incident)
    rows = []
    for product in products:
        summary = review_summary(product, dataset=dataset)
        overall = evaluate(product, 'NEGATIVE', dataset=dataset)
        themes = [evaluate(product, s, dataset=dataset) for s in PRODUCT_SIGNALS]
        accounts = {m.account_id: m.account.name for m in dataset.mappings[product.code]}
        account_metrics = [{'name': name, **evaluate(product, 'NEGATIVE', dataset=dataset, account_id=pk)} for pk, name in accounts.items()]
        open_cases = by_code.get(product.code, [])
        pending = sum(not trusted_analysis(effective_analysis(t)) for t in dataset.threads[product.code]
                      if t.received_at >= dataset.now - timedelta(days=7))
        anomalies = [m for m in [overall, *themes, *account_metrics] if m['status'] == 'ANOMALY']
        serious = any(trusted_analysis(a := effective_analysis(t)) and a.objective_issue and a.severity in {'P0', 'P1'}
                      for t in dataset.threads[product.code] if t.received_at >= dataset.now - timedelta(days=7))
        warnings = [m for m in [overall, *account_metrics] if m['baseline'] and m['status'] == 'WATCH' and m['rate'] is not None and m['period']['complete']]
        warning = bool(warnings)
        if anomalies or serious:
            status, label, css, reason = 'attention', 'Требует проверки', 'anomaly', anomalies[0]['reason'] if anomalies else 'Серьёзная конкретная проблема'
        elif open_cases:
            status, label, css, reason = 'incident', 'Есть расследование', 'watch', 'Проверка продолжается до решения ответственного'
        elif warning:
            status, label, css, reason = 'watch', 'Предупреждение', 'watch', warnings[0]['reason']
        elif not summary['complete']:
            status, label, css, reason = 'insufficient', 'Недостаточно данных', 'neutral', '; '.join(summary['period']['reasons']) or 'Не все отзывы разобраны'
        elif not overall['baseline']:
            status, label, css, reason = 'unconfigured', 'Настройте пороги', 'neutral', 'Правило общего негатива пока не задано'
        else:
            status, label, css, reason = 'normal', 'Обычный уровень', 'normal', 'В пределах настроенного порога'
        rows.append({'product': product, 'summary': summary, 'overall': overall, 'themes': [m for m in themes if m['count'] or m['cluster_count']],
                     'cluster_count': max([m['cluster_count'] for m in [overall, *themes, *account_metrics]], default=0),
                     'incidents': open_cases, 'pending': pending, 'status': status, 'label': label, 'css': css, 'reason': reason,
                     'account_alerts': [m for m in account_metrics if m['status'] == 'ANOMALY' or m in warnings],
                     'attention': status in {'attention', 'watch'} or bool(open_cases), 'incomplete': not summary['complete']})
    priority = {'attention': 0, 'incident': 1, 'watch': 2, 'insufficient': 3, 'unconfigured': 4, 'normal': 5}
    rows.sort(key=lambda row: (priority[row['status']], -(row['summary']['per100'] or 0), -row['summary']['negative'], row['product'].code))
    return rows
