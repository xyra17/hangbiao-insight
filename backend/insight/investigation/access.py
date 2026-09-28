from django.db.models import Q

from insight.access import shops_for
from insight.metrics.api import scoped
from insight.models import InvestigationRun


def visible_investigations(user):
    owned = InvestigationRun.objects.filter(requested_by=user, shop__in=shops_for(user))
    # A run without a resolved ASIN is itself the user's scoped clarification
    # record.  Metadata such as the selected planner provider must not make it
    # disappear from the cancel/clarify endpoints.
    unresolved = owned.filter(child_asin="")
    return owned.filter(Q(pk__in=scoped(owned, user).values("pk")) | Q(pk__in=unresolved.values("pk")))
