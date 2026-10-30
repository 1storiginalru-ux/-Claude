from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from desk.models import ReplyTestCase, Thread
from desk.replies.diagnostics import current_selection, saved_context
from desk.replies.service import case_subject


class Command(BaseCommand):
    help = 'Объяснить подбор примеров и шаблона: сохранённые результаты и текущие настройки. Без AI и изменений.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--thread', type=int)
        group.add_argument('--case', type=int)

    def handle(self, *args, **options):
        if options['thread']:
            item = Thread.objects.select_related('account__company', 'product', 'assessment').filter(pk=options['thread']).first()
            if not item: raise CommandError('Обращение не найдено.')
            case = ReplyTestCase.objects.select_related('account__company', 'product').filter(source_thread=item).first()
        else:
            case = ReplyTestCase.objects.select_related('account__company', 'product').filter(pk=options['case']).first()
            if not case: raise CommandError('Испытание не найдено.')
            item = case
        self.stdout.write(f'Компания: {item.account.company.name}. Кабинет: {item.account.name}.')
        self.stdout.write('Диагностика только читает данные. API не вызывается, токены не расходуются.')
        targets = [('Обращение', item, item)] if options['thread'] else []
        if case: targets.append((f'Испытание №{case.pk}', case_subject(case), case))
        for label, subject, target in targets:
            self.stdout.write(f'{label}: текущий подбор (локальная предварительная разметка).')
            selection = current_selection(subject)
            routing = selection['plan']
            self.stdout.write(f'Действие: {routing["action"]}. Сценарий: {routing["rule"]["name"] if routing["rule"] else "не выбран"}.')
            for candidate in selection['candidates']:
                self.stdout.write(f'  #{candidate["id"]} {candidate["title"]}, {candidate["kind"]}, v{candidate["version"]}: {candidate["status"]}.')
            for result in target.reply_drafts.select_related('job').order_by('-pk')[:6]:
                trace = saved_context(result)
                when = timezone.localtime(result.created_at).strftime('%d.%m.%Y %H:%M')
                self.stdout.write(f'Сохранённый вариант #{result.pk}, {when} МСК: {trace["mode"]}; {trace["method"]}; {trace["pipeline"]}.')
                self.stdout.write(f'  Сценарий: {trace["rule"]["name"] if trace["rule"] else "не выбран"}; стиль: {trace["style_chars"]} символов; фактов: {len(trace["facts"])}.')
                examples = ', '.join(f'#{m["id"]} {m["title"]} v{m["version"]}' for m in trace['examples'])
                self.stdout.write('  Примеры, переданные AI: ' + (examples if trace['action'] == 'ai' and examples else 'нет') + '.')
                if trace['template']: self.stdout.write(f'  Подставленный шаблон: {trace["template"]["title"]} v{trace["template"]["version"]}.')
                for note in trace['notes']: self.stdout.write('  ' + note)
