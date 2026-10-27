from django.core.management.base import BaseCommand, CommandError
from django.db.models.deletion import ProtectedError
from desk.demo_cleanup import DemoCleanupProblem, purge_demo


class Command(BaseCommand):
    help = 'Удалить стандартные демо-данные. По умолчанию — просмотр; --apply удаляет. Перед удалением остановите службы и создайте backup_db.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            counts = purge_demo(apply=options['apply'])
        except (DemoCleanupProblem, ProtectedError) as error:
            message = str(error) if isinstance(error, DemoCleanupProblem) else 'Найдены дополнительные связи с данными. Очистка отменена; данные не удалены.'
            raise CommandError(message) from error
        self.stdout.write('Удалены демо-данные:' if options['apply'] else 'Предварительный просмотр демо-данных (без удаления):')
        labels = {'reviews':'Отзывы','questions':'Вопросы','messages':'Сообщения','analyses':'Разметка','assessments':'Ручные решения демо','outgoing':'Демо-очередь','incidents':'Демо-расследования','products':'Демо-товары','accounts':'Демо-кабинеты','mappings':'Сопоставления демо','daily_sales':'Строки искусственных продаж','baselines':'Демо-пороги','sync_logs':'Демо-журнал','sync_states':'Демо-состояния синхронизации'}
        for name,value in counts.items(): self.stdout.write(f'{labels[name]}: {value}')
        self.stdout.write('Пользователи, рабочие компании/кабинеты, отзывы, продажи, ответы, API-ключи и лимиты сохранены. Аудит и пустая демо-компания сохранены; примеры автоматически не появятся снова.')
        if not options['apply']: self.stdout.write('Для удаления: voice-manage purge_demo --apply')
