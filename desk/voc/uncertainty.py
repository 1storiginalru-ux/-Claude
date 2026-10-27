"""Keep undated/ambiguous Market purchases out of trusted rolling windows."""
from desk.models import YandexOrderState
from desk.integrations.wb_sales import source_time


def sale_issues(account_ids):
    result=[]
    for row in YandexOrderState.objects.filter(account_id__in=account_ids).exclude(issues=[]).values('account_id','order_id','campaign_id','issues'):
        result.extend({'account_id':row['account_id'],'order_id':row['order_id'],'campaign_id':row['campaign_id'],**issue} for issue in row['issues'])
    return result


def overlaps(issue, since, until):
    low,high=source_time(issue.get('since')),source_time(issue.get('until'))
    return not low or not high or not since or (low<=until and high>=since)
