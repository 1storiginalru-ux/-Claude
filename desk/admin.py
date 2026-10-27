from django.contrib import admin
from django.forms.models import model_to_dict
from .models import Account, Analysis, Audit, Baseline, Company, HistorySyncState, Incident, Membership, Message, Outgoing, Product, ProductMapping, ReviewAssessment, SaleEvent, SalesSyncState, SalesDaily, SyncLog, Team, Thread


class AuditedAdmin(admin.ModelAdmin):
    def save_model(self, request, obj, form, change):
        before = model_to_dict(type(obj).objects.get(pk=obj.pk)) if change else {}
        super().save_model(request, obj, form, change)
        def clean(data): return {k: str(v) for k, v in data.items()}
        company = getattr(obj, 'company', None) or getattr(getattr(obj, 'product', None), 'company', None)
        if isinstance(obj, Company): company = obj
        Audit.objects.create(company=company, actor=request.user, action=f'admin.{obj._meta.model_name}.save', object_id=str(obj.pk), before=clean(before), after=clean(model_to_dict(obj)))

    def has_delete_permission(self, request, obj=None): return False


@admin.register(Company)
class CompanyAdmin(AuditedAdmin):
    list_display = ['name', 'legal_entity', 'brand', 'ai_provider']


@admin.register(Account)
class AccountAdmin(AuditedAdmin):
    list_display = ['name', 'company', 'platform', 'enabled', 'sales_enabled', 'history_enabled', 'last_sync']
    readonly_fields = ['last_sync', 'sync_status']


@admin.register(ProductMapping)
class MappingAdmin(AuditedAdmin):
    list_display = ['account', 'sku', 'product', 'external_product_id']
    search_fields = ['sku', 'product__code']

    def get_readonly_fields(self, request, obj=None):
        return ['account', 'sku'] if obj else []

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        Thread.objects.filter(account=obj.account, sku=obj.sku).update(product=obj.product)


@admin.register(Product)
class ProductAdmin(AuditedAdmin):
    list_display = ['code', 'name', 'company']
    search_fields = ['code', 'name']


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


@admin.register(SaleEvent)
class SaleEventAdmin(ReadOnlyAdmin):
    list_display = ['account', 'sku', 'kind', 'occurred_at', 'external_id']
    search_fields = ['sku', 'external_id', 'order_id']
    list_filter = ['account', 'kind']


admin.site.register(SalesSyncState, ReadOnlyAdmin)
admin.site.register(HistorySyncState, ReadOnlyAdmin)
admin.site.register(ReviewAssessment, ReadOnlyAdmin)


@admin.register(Audit)
class AuditAdmin(ReadOnlyAdmin):
    list_display = ['created_at', 'company', 'actor', 'action', 'object_id']
    list_filter = ['company', 'action']


admin.site.register(Membership, AuditedAdmin)
admin.site.register(Team, AuditedAdmin)
admin.site.register(Baseline, AuditedAdmin)
for model in [Thread, Message, Analysis, Outgoing, Incident, SalesDaily, SyncLog]:
    admin.site.register(model, ReadOnlyAdmin)
