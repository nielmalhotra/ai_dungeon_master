import json
from pathlib import Path

from django.db import transaction

from .models import CharacterTemplate


CHARACTER_TEMPLATES_PATH = (
    Path(__file__).resolve().parent / "assets" / "CHARACTER_TEMPLATES.json"
)


def load_character_templates():
    templates = json.loads(CHARACTER_TEMPLATES_PATH.read_text())
    if not isinstance(templates, dict):
        raise ValueError("Character templates must be a JSON object.")
    return templates


def sync_character_templates(using="default"):
    templates = load_character_templates()

    with transaction.atomic(using=using):
        for template_key, character_template in templates.items():
            CharacterTemplate.objects.using(using).update_or_create(
                template_key=template_key,
                defaults={"character_template": character_template},
            )

    return len(templates)


def sync_character_templates_after_migrate(sender, using, **kwargs):
    sync_character_templates(using=using)
