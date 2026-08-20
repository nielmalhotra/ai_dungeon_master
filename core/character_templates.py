import json
from pathlib import Path

from django.db import transaction

from .models import (
    AbilityTemplate,
    CharacterTemplate,
    CharacterTemplateItem,
    ItemTemplate,
)


CHARACTER_TEMPLATES_PATH = (
    Path(__file__).resolve().parent / "assets" / "CHARACTER_TEMPLATES.json"
)
ITEM_TEMPLATES_PATH = (
    Path(__file__).resolve().parent / "assets" / "ITEM_TEMPLATES.json"
)


def load_character_templates():
    templates = json.loads(CHARACTER_TEMPLATES_PATH.read_text())
    if not isinstance(templates, dict):
        raise ValueError("Character templates must be a JSON object.")
    return templates


def load_item_templates():
    templates = json.loads(ITEM_TEMPLATES_PATH.read_text())
    if not isinstance(templates, dict):
        raise ValueError("Item templates must be a JSON object.")
    return templates


def _ability_values(ability):
    uses = ability.get("uses")
    return {
        "name": ability["name"],
        "active": True,
        "category": ability.get("category", AbilityTemplate.Category.COMBAT),
        "description": ability["explanation"],
        "resolution_json": ability.get("resolution", {}),
        "effect_json": ability.get("effect", {}),
        "max_uses": uses.get("maximum") if uses else None,
        "recharge": uses.get("recharge") if uses else None,
    }


def sync_character_templates(using="default"):
    templates = load_character_templates()
    item_definitions = load_item_templates()

    with transaction.atomic(using=using):
        character_rows = {}
        for template_key, character_template in templates.items():
            character_rows[template_key], _ = CharacterTemplate.objects.using(
                using
            ).update_or_create(
                template_key=template_key,
                defaults={"character_template": character_template},
            )

        AbilityTemplate.objects.using(using).update(active=False)
        for template_key, character_template in templates.items():
            character_row = character_rows[template_key]
            for ability in character_template["abilities"]:
                AbilityTemplate.objects.using(using).update_or_create(
                    character_template=character_row,
                    ability_key=ability["key"],
                    defaults=_ability_values(ability),
                )

        ItemTemplate.objects.using(using).update(active=False)
        item_rows = {}
        for item_key, definition in item_definitions.items():
            item_rows[item_key], _ = ItemTemplate.objects.using(using).update_or_create(
                template_key=item_key,
                defaults={
                    "name": definition["name"],
                    "active": True,
                    "definition_json": {
                        "public_info": definition.get("public_info", {}),
                        "dm_only": definition.get("dm_only", {}),
                    },
                    "mechanics_json": definition.get("mechanics", {}),
                },
            )

        CharacterTemplateItem.objects.using(using).all().delete()
        loadout_rows = []
        for template_key, character_template in templates.items():
            for item in character_template["gear"]:
                if item["key"] not in item_rows:
                    raise ValueError(
                        f"Missing item template definition for {item['key']!r}."
                    )
                loadout_rows.append(
                    CharacterTemplateItem(
                        character_template=character_rows[template_key],
                        item_template=item_rows[item["key"]],
                        starting_quantity=item.get("quantity", 1),
                    )
                )
        CharacterTemplateItem.objects.using(using).bulk_create(loadout_rows)

    return len(templates)


def sync_gameplay_template_embeddings(client=None, batch_size=100, using="default"):
    from .scenario_lore import ScenarioEmbeddingInput, embed_scenario_inputs

    sync_character_templates(using=using)
    abilities = list(AbilityTemplate.objects.using(using).filter(active=True))
    items = list(ItemTemplate.objects.using(using).filter(active=True))
    inputs = [
        ScenarioEmbeddingInput(
            key=f"ability:{ability.id}",
            content=f"{ability.name}\n\n{ability.description}",
        )
        for ability in abilities
    ]
    for item in items:
        public_summary = item.definition_json.get("public_info", {}).get("summary", "")
        dm_summary = item.definition_json.get("dm_only", {}).get("summary", "")
        if public_summary:
            inputs.append(
                ScenarioEmbeddingInput(
                    key=f"item:{item.id}:public_info",
                    content=f"{item.name}\n\n{public_summary}",
                )
            )
        if dm_summary:
            inputs.append(
                ScenarioEmbeddingInput(
                    key=f"item:{item.id}:dm_only",
                    content=f"{item.name}\n\n{dm_summary}",
                )
            )

    embeddings = embed_scenario_inputs(
        inputs,
        client=client,
        batch_size=batch_size,
    )
    with transaction.atomic(using=using):
        for ability in abilities:
            ability.embedding = embeddings[f"ability:{ability.id}"]
            ability.save(using=using, update_fields=["embedding", "updated_at"])
        for item in items:
            public_key = f"item:{item.id}:public_info"
            dm_key = f"item:{item.id}:dm_only"
            item.public_embedding = embeddings.get(public_key)
            item.dm_embedding = embeddings.get(dm_key)
            item.save(
                using=using,
                update_fields=["public_embedding", "dm_embedding", "updated_at"],
            )

    return len(abilities), len(items)


def sync_character_templates_after_migrate(sender, using, **kwargs):
    sync_character_templates(using=using)
