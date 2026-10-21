from django.core.exceptions import PermissionDenied
from .models import Company, Membership


def companies_for(user):
    return Company.objects.all() if user.is_superuser else Company.objects.filter(membership__user=user)


def require_role(user, company_id, roles):
    if user.is_superuser:
        return
    if not Membership.objects.filter(user=user, company_id=company_id, role__in=roles).exists():
        raise PermissionDenied('Недостаточно прав для этого действия.')


OPERATORS = ['supervisor', 'operator']
QUALITY = ['supervisor', 'quality', 'investigator']
