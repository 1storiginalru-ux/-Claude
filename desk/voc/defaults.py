"""Resolve scoped cabinet defaults without creating thousands of product rules."""
from dataclasses import dataclass

FIELDS = ('warning', 'threshold', 'min_redemptions', 'min_signals', 'cluster_count', 'cluster_days')
TOPIC_FIELDS = (*FIELDS, 'cluster_enabled')


@dataclass(frozen=True)
class DefaultRule:
    low: float
    high: float
    min_redemptions: int
    min_signals: int
    cluster_count: int
    cluster_days: int
    source: str = 'default'
    signal: str = 'NEGATIVE'
    cluster_enabled: bool = True
    rule_kind: str = 'thresholds'
    is_default: bool = True

    def get_source_display(self):
        return 'Общее правило кабинетов' if self.signal == 'NEGATIVE' else 'Правило кабинетов по теме'


def fallback_rule(defaults, signal='NEGATIVE'):
    rows = list(defaults)
    if not rows:
        return None
    # A combined ProductID cannot silently choose between conflicting defaults.
    if len({tuple(getattr(row, key) for key in FIELDS) + (getattr(row, 'cluster_enabled', True),) for row in rows}) != 1:
        return None
    row = rows[0]
    return DefaultRule(row.warning, row.threshold, row.min_redemptions, row.min_signals,
                       row.cluster_count, row.cluster_days, signal=signal,
                       cluster_enabled=getattr(row, 'cluster_enabled', True))


def default_for_family(family, account_id=None, signal='NEGATIVE'):
    from desk.models import ControlDefaults, ControlTopicRule
    rows = (ControlDefaults if signal == 'NEGATIVE' else ControlTopicRule).objects.filter(
        account__productmapping__product__in=family,
        account__company_id__in=family.values('company_id')).distinct()
    if signal != 'NEGATIVE': rows = rows.filter(signal=signal)
    if account_id is not None: rows = rows.filter(account_id=account_id)
    return fallback_rule(rows, signal)
