"""Load a permission-scoped queue in bounded query count, then rank before paging."""
from collections import defaultdict
from django.db.models import F
from django.utils import timezone
from desk.models import Baseline, ControlDefaults, ProductMapping, SaleEvent, SalesDaily, SalesSyncState, SyncState, Thread
from .windows import calculate_window


class ControlDataset:
    def __init__(self, products, companies, now=None, include_reviews=True):
        self.now = now or timezone.now()
        codes = [p.code for p in products]
        self.company_ids = set(companies.values_list('pk', flat=True))
        self.mappings, self.events, self.daily, self.threads = (defaultdict(list) for _ in range(4))
        self.baselines, self.periods = {}, {}
        mappings = ProductMapping.objects.filter(product__code__in=codes, product__company_id__in=self.company_ids).select_related('account', 'product')
        accounts = set()
        for mapping in mappings:
            self.mappings[mapping.product.code].append(mapping)
            accounts.add(mapping.account_id)
        sales = SaleEvent.objects.filter(account_id__in=accounts, account__sales_enabled=True, account__platform__in=['wb','ym'], kind='sale', occurred_at__lte=self.now,
            account__productmapping__product__code__in=codes, account__productmapping__product__company_id__in=self.company_ids,
            sku=F('account__productmapping__sku')).annotate(product_code=F('account__productmapping__product__code')).order_by('-occurred_at', '-pk')
        for row in sales.values('id', 'account_id', 'sku', 'occurred_at', 'product_code'):
            self.events[row['product_code']].append(row)
        daily = SalesDaily.objects.filter(account_id__in=accounts, account__productmapping__product__code__in=codes,
            account__productmapping__product__company_id__in=self.company_ids, sku=F('account__productmapping__sku'))
        for row in daily.annotate(product_code=F('account__productmapping__product__code')).values('account_id', 'sku', 'date', 'redemptions', 'product_code'):
            self.daily[row['product_code']].append(row)
        reviews = Thread.objects.filter(product__code__in=codes, account__company_id__in=self.company_ids, product__company_id__in=self.company_ids, kind='review', received_at__lte=self.now)
        if include_reviews:
            for thread in reviews.select_related('analysis', 'assessment', 'product').defer('source_data', 'history_cursor', 'analysis__draft', 'analysis__summary', 'assessment__comment'):
                self.threads[thread.product.code].append(thread)
        self.sales_states = {s.account_id: s for s in SalesSyncState.objects.filter(account_id__in=accounts)}
        from .uncertainty import sale_issues
        self.uncertainties=defaultdict(list)
        codes_by_pair=defaultdict(set)
        for code,pairs in self.mappings.items():
            for pair in pairs:
                codes_by_pair[pair.account_id,pair.sku].add(code)
        for issue in sale_issues(accounts):
            for code in codes_by_pair[issue['account_id'],issue['sku']]: self.uncertainties[code].append(issue)
        from desk.integrations.coverage import feedback_coverage
        self.feedback_states = feedback_coverage(accounts)
        for baseline in Baseline.objects.filter(product__code__in=codes, product__company_id__in=self.company_ids).select_related('product').order_by('product_id'):
            self.baselines.setdefault((baseline.product.code, baseline.signal), baseline)
        self.explicit_baselines = dict(self.baselines)
        self.default_rules = {row.account_id: row for row in ControlDefaults.objects.filter(
            account_id__in=accounts, account__company_id__in=self.company_ids)}
        for code in codes:
            rule = self.baseline(code, 'NEGATIVE')
            if rule: self.baselines.setdefault((code, 'NEGATIVE'), rule)

    def baseline(self, code, signal, account_id=None):
        explicit = self.explicit_baselines.get((code, signal))
        if explicit or signal != 'NEGATIVE':
            return explicit
        from .defaults import fallback_rule
        accounts = {m.account_id for m in self.mappings[code] if account_id is None or m.account_id == account_id}
        return fallback_rule(self.default_rules[pk] for pk in accounts if pk in self.default_rules)

    def has_rule(self, code, signal):
        return ((code, signal) in self.baselines or signal == 'NEGATIVE'
                and any(m.account_id in self.default_rules for m in self.mappings[code]))

    def period(self, code, window='100', account_id=None):
        key = (code, window, account_id)
        if key not in self.periods:
            pairs = [m for m in self.mappings[code] if account_id is None or m.account_id == account_id]
            accounts = {m.account_id for m in pairs}
            api = {m.account_id for m in pairs if m.account.platform in {'wb','ym'} and m.account.sales_enabled}
            manual = accounts - api
            events = [e for e in self.events[code] if e['account_id'] in api]
            daily = [d for d in self.daily[code] if d['account_id'] in manual]
            self.periods[key] = calculate_window(window, self.now, api, accounts, manual, self.sales_states, self.feedback_states, daily, events,self.uncertainties[code])
        return self.periods[key]

    def reviews(self, code, period, account_id=None):
        if not period['start_at']:
            return []
        return [t for t in self.threads[code] if (account_id is None or t.account_id == account_id)
                and period['start_at'] <= t.received_at <= period['until']]
