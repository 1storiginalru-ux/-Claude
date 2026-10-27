import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from django.utils import timezone


class Company(models.Model):
    name = models.CharField('Компания', max_length=160)
    is_demo = models.BooleanField('Демонстрационная компания (без внешних отправок)', default=False)
    legal_entity = models.CharField('Юридическое лицо', max_length=240, blank=True)
    brand = models.CharField('Бренд', max_length=120, blank=True)
    tone_of_voice = models.TextField('Tone of Voice', blank=True)
    knowledge = models.TextField('Подтверждённые знания', blank=True)
    ai_provider = models.CharField(max_length=20, choices=[('none', 'Не подключён'), ('openai', 'OpenAI'), ('anthropic', 'Anthropic')], default='none')
    ai_model = models.CharField(max_length=100, blank=True)
    ai_key_env = models.CharField(max_length=100, default='VOC_AI_KEY')
    sla_minutes = models.PositiveIntegerField(default=120, validators=[MinValueValidator(1)])

    def __str__(self): return self.name


class Team(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'name'], name='team_unique')]

    def __str__(self): return f'{self.company}: {self.name}'


class Membership(models.Model):
    ROLES = [('supervisor', 'Руководитель'), ('operator', 'Оператор'), ('quality', 'VoC / качество'), ('investigator', 'Расследования'), ('viewer', 'Просмотр')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLES, default='operator')
    team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'company'], name='membership_unique')]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.team_id and self.team.company_id != self.company_id:
            raise ValidationError('Команда должна относиться к той же компании.')


class Account(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    name = models.CharField(max_length=80, unique=True)
    platform = models.CharField(max_length=10, choices=[('wb', 'Wildberries'), ('ym', 'Яндекс Маркет')])
    token_env = models.CharField('Имя переменной с API-ключом', max_length=100, validators=[RegexValidator(r'^[A-Z][A-Z0-9_]*$')])
    business_id = models.CharField('Business ID (Яндекс)', max_length=40, blank=True, validators=[RegexValidator(r'^\d*$')])
    enabled = models.BooleanField(default=False)
    sales_enabled = models.BooleanField('Получать продажи WB через API', default=False)
    sales_token_env = models.CharField('Переменная отдельного ключа статистики WB', max_length=100, blank=True, validators=[RegexValidator(r'^[A-Z][A-Z0-9_]*$')], help_text='Пусто — использовать ключ кабинета. Для продаж нужны права «Статистика».')
    last_sync = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(max_length=240, default='Ещё не запускалась')

    def __str__(self): return self.name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.company_id and self.company.is_demo and self.enabled:
            raise ValidationError('Для демо-компании внешние интеграции включать нельзя. Создайте реальную компанию.')
        if self.sales_enabled and (self.platform != 'wb' or not self.company_id or self.company.is_demo):
            raise ValidationError('Автоматические продажи пока доступны только для реального кабинета WB.')
        if self.pk and Account.objects.filter(pk=self.pk).exclude(company_id=self.company_id).exists() and (self.thread_set.exists() or self.productmapping_set.exists()):
            raise ValidationError('Нельзя менять компанию кабинета с историей или Mapping.')


class Product(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    code = models.CharField('ProductID', max_length=100, db_index=True)
    name = models.CharField(max_length=200)
    brand = models.CharField(max_length=100, blank=True)
    volume = models.CharField(max_length=240, blank=True)
    concentration = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['company', 'code'], name='product_unique')]

    def __str__(self): return f'{self.code} · {self.name}'

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.pk and Product.objects.filter(pk=self.pk).exclude(company_id=self.company_id).exists() and (self.mappings.exists() or self.thread_set.exists()):
            raise ValidationError('Нельзя менять компанию товара с историей или Mapping.')
        other = Product.objects.filter(code=self.code).exclude(pk=self.pk).first()
        if other and any(getattr(other, field) != getattr(self, field) for field in ['name', 'brand', 'volume', 'concentration']):
            raise ValidationError('Один ProductID должен иметь одинаковое описание физического товара во всех компаниях.')


class ProductMapping(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='mappings')
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    sku = models.CharField(max_length=120)
    external_product_id = models.CharField(max_length=120, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'sku'], name='mapping_unique')]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.product_id and self.account_id and self.product.company_id != self.account.company_id:
            raise ValidationError('Товар и кабинет должны принадлежать одной компании.')
        if self.account_id and self.account.platform == 'wb' and not self.account.company.is_demo:
            from .products.identifiers import wb_nm_id
            card = wb_nm_id(self.sku)
            if not card or card != wb_nm_id(self.external_product_id):
                raise ValidationError('Для WB поля SKU и External product id должны содержать одинаковый числовой артикул WB (nmId).')
            self.sku = self.external_product_id = card


class SalesDaily(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    sku = models.CharField(max_length=120)
    date = models.DateField()
    redemptions = models.PositiveIntegerField()
    orders = models.PositiveIntegerField(default=0)
    delivered = models.PositiveIntegerField(default=0)
    returns = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'sku', 'date'], name='daily_sales_unique')]


class SaleEvent(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    external_id = models.CharField(max_length=160)
    sku = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=[('sale', 'Выкуп'), ('return', 'Возврат')])
    occurred_at = models.DateTimeField(db_index=True)
    changed_at = models.DateTimeField()
    order_id = models.CharField(max_length=160, blank=True)
    source_data = models.JSONField(default=dict)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'external_id'], name='sale_event_unique')]
        indexes = [models.Index(fields=['account', 'sku', 'kind', '-occurred_at'], name='sale_product_window')]


class SalesSyncState(models.Model):
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name='sales_state')
    cursor = models.JSONField(default=dict)
    next_attempt = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    coverage_since = models.DateTimeField(null=True, blank=True)
    coverage_until = models.DateTimeField(null=True, blank=True)
    last_sync = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=240, blank=True)


class Thread(models.Model):
    STATUSES = [(v, v) for v in ['Новое', 'Назначено', 'В работе', 'Ожидаем клиента', 'Ожидаем внутреннее решение', 'Ответ готов', 'Ответ отправлен', 'Закрыто', 'Эскалировано', 'Ошибка отправки']]
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    external_id = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=[('review', 'Отзыв'), ('question', 'Вопрос')])
    sku = models.CharField(max_length=120, blank=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True)
    customer = models.CharField(max_length=120, default='Покупатель')
    text = models.TextField()
    rating = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    received_at = models.DateTimeField(default=timezone.now)
    due_at = models.DateTimeField()
    status = models.CharField(max_length=50, choices=STATUSES, default='Новое')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    source_data = models.JSONField(default=dict, blank=True)
    source_synced_at = models.DateTimeField(null=True, blank=True)
    history_synced_at = models.DateTimeField(null=True, blank=True)
    history_cursor = models.JSONField(default=dict, blank=True)
    details_next_attempt = models.DateTimeField(default=timezone.now, db_index=True)
    details_error = models.CharField(max_length=240, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'kind', 'external_id'], name='thread_external_unique')]
        ordering = ['-received_at']


class Message(models.Model):
    thread = models.ForeignKey(Thread, on_delete=models.PROTECT, related_name='messages')
    external_id = models.CharField(max_length=200)
    direction = models.CharField(max_length=10, choices=[('in', 'Входящее'), ('out', 'Исходящее')])
    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    source_data = models.JSONField(default=dict, blank=True)

    @property
    def source_author(self):
        if not isinstance(self.source_data, dict): return ''
        author = self.source_data.get('author') or self.source_data.get('userName')
        return author.get('name', '') if isinstance(author, dict) else (author or '')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['thread', 'external_id', 'direction'], name='message_external_unique')]
        ordering = ['created_at', 'id']


class Analysis(models.Model):
    thread = models.OneToOneField(Thread, on_delete=models.CASCADE, related_name='analysis')
    sentiment = models.CharField(max_length=20)
    hidden_negative = models.BooleanField(default=False)
    signals = models.JSONField(default=list)
    severity = models.CharField(max_length=2)
    objective_issue = models.BooleanField(default=False)
    requires_human = models.BooleanField(default=True)
    confidence = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    summary = models.TextField()
    draft = models.TextField(blank=True)
    provider = models.CharField(max_length=30)
    model = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=30, default='1.0')
    created_at = models.DateTimeField(auto_now=True)

    @property
    def grouped_signals(self):
        from .voc.rules import grouped_signals, signal_label
        return [signal_label(signal) for signal in grouped_signals(self.signals)]


class Baseline(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    signal = models.CharField(max_length=40)
    low = models.FloatField(default=0, validators=[MinValueValidator(0)])
    high = models.FloatField(default=5, validators=[MinValueValidator(0)])
    source = models.CharField(max_length=20, choices=[('manual', 'Ручной'), ('historical', 'Исторический')], default='manual')
    sample_redemptions = models.PositiveIntegerField(default=0)
    min_redemptions = models.PositiveIntegerField(default=20)
    min_signals = models.PositiveIntegerField(default=3)
    cluster_count = models.PositiveIntegerField(default=4)
    cluster_days = models.PositiveIntegerField(default=3)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['product', 'signal'], name='baseline_unique')]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.low > self.high: raise ValidationError('Нижняя граница больше верхней.')
        if min(self.min_redemptions, self.min_signals, self.cluster_count, self.cluster_days) < 1:
            raise ValidationError('Размеры выборки и кластера должны быть больше нуля.')


class ReviewAssessment(models.Model):
    DECISIONS = [('negative', 'Негатив'), ('non_negative', 'Не негатив'), ('pending', 'Требует проверки')]
    thread = models.OneToOneField(Thread, on_delete=models.CASCADE, related_name='assessment')
    decision = models.CharField(max_length=20, choices=DECISIONS)
    signals = models.JSONField(default=list)
    severity = models.CharField(max_length=2, default='P2', choices=[(v, v) for v in ['P0', 'P1', 'P2', 'P3']])
    objective_issue = models.BooleanField(default=False)
    comment = models.TextField(blank=True)
    source_fingerprint = models.CharField(max_length=64)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    reviewed_at = models.DateTimeField(auto_now=True)


class Outgoing(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(Thread, on_delete=models.PROTECT, related_name='outgoing')
    request_key = models.UUIDField(unique=True)
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    text = models.TextField()
    ai_draft = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=[(x, x) for x in ['Draft', 'Queued', 'Sending', 'Sent', 'Confirmed', 'Failed']], default='Queued')
    external_id = models.CharField(max_length=160, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt = models.DateTimeField(default=timezone.now)
    error = models.CharField(max_length=240, blank=True)
    retry_safe = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['thread'], condition=Q(status__in=['Queued', 'Sending', 'Sent']), name='one_pending_outgoing')]


class Incident(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    signal = models.CharField(max_length=40)
    severity = models.CharField(max_length=2)
    status = models.CharField(max_length=20, choices=[('INVESTIGATION', 'Проверка'), ('CAPA', 'Принимаем меры'), ('CONTROL', 'Контроль'), ('CLOSED', 'Закрыто')], default='INVESTIGATION')
    threads = models.ManyToManyField(Thread, blank=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    hypothesis = models.TextField(blank=True)
    actions = models.TextField(blank=True)
    result = models.TextField(blank=True)
    opening_snapshot = models.JSONField(default=dict, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    bitrix_id = models.CharField(max_length=80, blank=True)
    bitrix_status = models.CharField(max_length=240, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def display_signal(self):
        from .voc.rules import grouped_signals, signal_label
        return signal_label(grouped_signals([self.signal])[0])

    class Meta:
        constraints = [models.UniqueConstraint(fields=['product', 'signal'], condition=~Q(status='CLOSED'), name='open_incident_unique')]


class Audit(models.Model):
    company = models.ForeignKey(Company, on_delete=models.PROTECT, null=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    action = models.CharField(max_length=80)
    object_id = models.CharField(max_length=160)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class SyncLog(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    success = models.BooleanField()
    detail = models.CharField(max_length=240)
    retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ApiThrottle(models.Model):
    # An opaque digest scopes a shared budget without persisting the API credential.
    scope = models.CharField(max_length=64, unique=True)
    next_request_at = models.DateTimeField(default=timezone.now)
    strikes = models.PositiveIntegerField(default=0)


class SyncState(models.Model):
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name='sync_state')
    cursor = models.JSONField(default=dict)
    next_attempt = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    coverage_since = models.DateTimeField(null=True, blank=True)
    coverage_until = models.DateTimeField(null=True, blank=True)
