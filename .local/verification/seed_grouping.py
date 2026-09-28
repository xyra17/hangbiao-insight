import json
from pathlib import Path
from django.conf import settings
from insight.models import BusinessEvent
assert settings.DEBUG
fixture = json.loads(Path('../.local/workflow-acceptance.json').read_text())
event = BusinessEvent.objects.get(pk=fixture['event'], shop__workspace__is_test=True)
BusinessEvent.objects.get_or_create(shop=event.shop, child_asin=event.child_asin, object_key=event.object_key, family='experience', defaults={'title':'合成关联退货事件','priority':1,'last_cutoff':event.last_cutoff,'last_seen':event.last_seen})
print('已建立同一合成商品的跨问题族关联事件。')
