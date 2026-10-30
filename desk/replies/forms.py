from django import forms
from desk.models import Account, Product, ReplyMaterial, ReplyRule, ReplyTestCase, KnowledgeEntry
from desk.voc.rules import PRODUCT_SIGNALS, signal_label

TOPICS = [('', 'Любая тема'), *[(s, signal_label(s)) for s in [*PRODUCT_SIGNALS, 'DELIVERY', 'SERVICE', 'CONTENT', 'PRICE', 'OTHER']]]

class MaterialForm(forms.ModelForm):
    topic = forms.ChoiceField(label='Тема', choices=TOPICS, required=False)
    class Meta:
        model = ReplyMaterial
        fields = ['kind', 'title', 'customer_text', 'answer', 'explanation', 'topic', 'sentiment', 'thread_kind', 'platform', 'product_code', 'brand', 'excluded_words', 'active', 'approved']
        labels = {'sentiment': 'Тональность', 'thread_kind': 'Тип обращения', 'platform': 'Площадка', 'active': 'Использовать', 'approved': 'Подтверждено руководителем'}
        widgets = {name: forms.Textarea(attrs={'rows': 3}) for name in ['customer_text','answer','explanation','excluded_words']}
    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs); self.company = company
        self.fields['kind'].help_text = 'Пример направляет формулировки AI. Готовый шаблон подставляется без генерации только через сценарий с действием «Готовый шаблон».'
    def clean(self):
        data = super().clean()
        if data.get('kind') == 'example' and not data.get('customer_text'): self.add_error('customer_text', 'Для примера нужен текст обращения.')
        if data.get('product_code') and not Product.objects.filter(company=self.company, code=data['product_code']).exists(): self.add_error('product_code', 'ProductID не найден в этой компании.')
        return data

class RuleForm(forms.ModelForm):
    kind = forms.ChoiceField(label='Тип обращения', choices=[('', 'Любой'), ('review', 'Отзыв'), ('question', 'Вопрос')], required=False)
    ratings = forms.MultipleChoiceField(label='Оценки', choices=[(str(n), str(n)) for n in range(1,6)], required=False, widget=forms.CheckboxSelectMultiple)
    signals = forms.MultipleChoiceField(label='Темы (достаточно одной выбранной)', choices=TOPICS[1:], required=False)
    sentiment = forms.ChoiceField(label='Тональность', choices=ReplyMaterial._meta.get_field('sentiment').choices, required=False)
    words = forms.CharField(label='Любое из слов/фраз, по одной на строке', max_length=2000, required=False, widget=forms.Textarea(attrs={'rows':2}))
    brands = forms.CharField(label='Бренды, по одному на строке', max_length=2000, required=False, widget=forms.Textarea(attrs={'rows':2}))
    products = forms.CharField(label='ProductID, по одному на строке', max_length=2000, required=False, widget=forms.Textarea(attrs={'rows':2}))
    has_text = forms.ChoiceField(label='Текст', choices=[('', 'Неважно'), ('yes', 'Есть'), ('no', 'Нет')], required=False)
    has_photo = forms.ChoiceField(label='Фото', choices=[('', 'Неважно'), ('yes', 'Есть'), ('no', 'Нет')], required=False)
    has_video = forms.ChoiceField(label='Видео', choices=[('', 'Неважно'), ('yes', 'Есть'), ('no', 'Нет')], required=False)
    class Meta:
        model = ReplyRule
        fields = ['name', 'active', 'priority', 'accounts', 'condition_mode', 'action', 'material']
        labels = {'accounts':'Кабинеты (пусто — все в компании)', 'condition_mode':'Как объединять условия', 'action':'Способ подготовки', 'material':'Шаблон'}
    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs); self.company = company
        self.fields['accounts'].queryset = Account.objects.filter(company=company)
        self.fields['material'].queryset = ReplyMaterial.objects.filter(company=company, kind='template', approved=True, active=True)
        if self.instance.pk:
            for key, value in self.instance.conditions.items():
                self.initial[key] = ('yes' if value else 'no') if key.startswith('has_') else '\n'.join(value) if key in {'words','brands','products'} else [str(v) for v in value] if key == 'ratings' else value
    def clean(self):
        data = super().clean()
        if data.get('action') == 'template' and not data.get('material'): self.add_error('material', 'Выберите подтверждённый шаблон.')
        return data
    def conditions(self):
        from .policy import lines
        out = {}
        for key in ['kind','ratings','signals','sentiment','words','brands','products','has_text','has_photo','has_video']:
            value = self.cleaned_data.get(key)
            if not value: continue
            out[key] = value == 'yes' if key.startswith('has_') else lines(value) if key in {'words','brands','products'} else [int(v) for v in value] if key == 'ratings' else value
        return out

class FactForm(forms.ModelForm):
    class Meta:
        model = KnowledgeEntry
        fields = ['product_code', 'title', 'content', 'source', 'active', 'approved']
        widgets = {'content': forms.Textarea(attrs={'rows':5})}
    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs); self.company = company
    def clean_product_code(self):
        code = self.cleaned_data['product_code']
        if code and not Product.objects.filter(company=self.company, code=code).exists(): raise forms.ValidationError('ProductID не найден в этой компании.')
        return code
    def clean(self):
        data = super().clean()
        if data.get('approved') and not data.get('source','').strip(): self.add_error('source', 'Укажите источник подтверждения.')
        return data

class TestCaseForm(forms.ModelForm):
    class Meta:
        model = ReplyTestCase
        fields = ['account','title','kind','text','rating','product','expected_answer']
        labels = {'account':'Кабинет', 'kind':'Тип обращения', 'rating':'Оценка', 'product':'Товар'}
        widgets = {'text': forms.Textarea(attrs={'rows':4}), 'expected_answer': forms.Textarea(attrs={'rows':3})}
    def __init__(self, *args, scope, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['account'].queryset = Account.objects.filter(company__in=scope, company__is_demo=False, enabled=True)
        self.fields['product'].queryset = Product.objects.filter(company__in=scope)
    def clean(self):
        data = super().clean(); account,product = data.get('account'),data.get('product')
        if self.instance.pk and account and self.instance.account_id != account.pk: self.add_error('account','Кабинет сохранённого испытания менять нельзя. Создайте новый случай.')
        if account and product and account.company_id != product.company_id: self.add_error('product', 'Товар должен относиться к компании кабинета.')
        if data.get('kind') == 'question': data['rating'] = None
        return data
