from django.db.models import Q

from insight.access import shops_for
from insight.metrics.api import scoped
from insight.models import InvestigationRun


def visible_investigations(user):
    owned = InvestigationRun.objects.filter(requested_by=user, shop__in=shops_for(user))
    unresolved = owned.filter(child_asin="").filter(
        Q(state={}) | (Q(state__evidence=[]) & (Q(state__asin="") | ~Q(state__has_key="asin")))
    )
    return owned.filter(Q(pk__in=scoped(owned, user).values("pk")) | Q(pk__in=unresolved.values("pk")))
