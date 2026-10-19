from django.apps import AppConfig


class InsightConfig(AppConfig):
    name = "insight"

    def ready(self):
        from django.conf import settings

        if settings.ACCEPTANCE_ENVIRONMENT:
            from .management.commands._acceptance import require_isolated_acceptance_environment

            require_isolated_acceptance_environment()
