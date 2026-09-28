"""Small-team login limits, shared atomically by every API process."""
from collections.abc import Mapping
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.throttling import BaseThrottle

from .models import LoginThrottleBucket


class LoginThrottle(BaseThrottle):
    def allow_request(self, request, view):
        now = timezone.now()
        window = now.replace(second=0, microsecond=0)
        self.retry_after = max(1, (window + timedelta(minutes=1) - now).total_seconds())
        username = str(request.data.get("username", ""))[:150].casefold() if isinstance(request.data, Mapping) else ""
        identifiers = [("account", username, 10), ("address", self.get_ident(request), 60)]
        allowed = True
        with transaction.atomic():
            LoginThrottleBucket.objects.filter(window_start__lt=window - timedelta(minutes=2)).delete()
            for kind, identifier, limit in identifiers:
                key = salted_hmac("login-throttle", f"{kind}:{identifier}:{window.isoformat()}", algorithm="sha256").hexdigest()
                bucket, _ = LoginThrottleBucket.objects.get_or_create(key=key, defaults={"window_start": window})
                bucket = LoginThrottleBucket.objects.select_for_update().get(pk=bucket.pk)
                bucket.attempts += 1
                bucket.save(update_fields=["attempts"])
                allowed = allowed and bucket.attempts <= limit
        return allowed

    def wait(self):
        return self.retry_after
