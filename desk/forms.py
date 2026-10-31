from django import forms
from desk.models import ReviewAssessment
from desk.voc.rules import PRODUCT_SIGNALS, signal_label


class ReviewAssessmentForm(forms.Form):
    decision = forms.ChoiceField(label='Решение оператора', choices=ReviewAssessment.DECISIONS)
    signals = forms.MultipleChoiceField(label='Причины', required=False,
        choices=[(s, signal_label(s)) for s in [*PRODUCT_SIGNALS, 'DELIVERY', 'SERVICE', 'CONTENT', 'PRICE', 'OTHER']],
        widget=forms.CheckboxSelectMultiple)
    severity = forms.ChoiceField(label='Важность', choices=[('P0', 'P0 — срочно'), ('P1', 'P1 — высокая'), ('P2', 'P2 — обычная'), ('P3', 'P3 — низкая')], initial='P2')
    objective_issue = forms.BooleanField(label='Есть конкретная проблема, требующая проверки', required=False)
    comment = forms.CharField(label='Комментарий к решению', required=False, max_length=4000, widget=forms.Textarea(attrs={'rows': 2}))
    source_fingerprint = forms.CharField(min_length=64, max_length=64, widget=forms.HiddenInput)
    assessment_version = forms.CharField(required=False, max_length=64, widget=forms.HiddenInput)


class NegativeRuleForm(forms.Form):
    warning = forms.FloatField(label='Предупреждение: негативных отзывов на 100 выкупов', min_value=0, initial=5)
    threshold = forms.FloatField(label='Расследование: негативных отзывов на 100 выкупов', min_value=0, initial=10)
    min_redemptions = forms.IntegerField(label='Минимум выкупов для окна 100', min_value=100, max_value=100, initial=100,
        help_text='Для процентного правила требуется полное окно из 100 выкупов. Серия похожих жалоб проверяется отдельно.')
    min_signals = forms.IntegerField(label='Минимум негативных отзывов', min_value=1, initial=3)
    cluster_count = forms.IntegerField(label='Серия похожих жалоб: минимум отзывов', min_value=2, initial=4)
    cluster_days = forms.IntegerField(label='Период серии, дней', min_value=1, max_value=90, initial=3)

    def clean(self):
        import math
        data = super().clean()
        if any(key in data and not math.isfinite(data[key]) for key in ['warning', 'threshold']):
            raise forms.ValidationError('Пороги должны быть конечными числами.')
        if 'warning' in data and 'threshold' in data and data['warning'] >= data['threshold']:
            raise forms.ValidationError('Порог расследования должен быть выше порога предупреждения.')
        return data
