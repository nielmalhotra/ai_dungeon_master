from django.apps import AppConfig
from django.db.models.signals import post_migrate


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from .character_templates import sync_character_templates_after_migrate

        post_migrate.connect(
            sync_character_templates_after_migrate,
            sender=self,
            dispatch_uid="core.sync_character_templates_after_migrate",
        )
