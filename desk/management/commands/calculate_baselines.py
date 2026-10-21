from django.core.management.base import BaseCommand
from desk.models import Product
from desk.voc.baselines import refresh_baselines


class Command(BaseCommand):
    help = 'Рассчитать исторические baseline вне текущего окна; ручные baseline сохраняются.'

    def handle(self, *args, **options):
        count = sum(refresh_baselines(p) for p in Product.objects.all())
        self.stdout.write(f'Рассчитано baseline: {count}. Для расчёта нужно ≥100 исторических выкупов вне текущего окна.')
