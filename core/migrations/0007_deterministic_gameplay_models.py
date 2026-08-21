import core.models
from copy import deepcopy
from django.db import migrations, models
import django.db.models.deletion
import pgvector.django.vector


def with_relationship_buckets(state):
    state = deepcopy(state or {})
    for visibility in ("public_info", "dm_only"):
        branch = state.setdefault(visibility, {})
        relationships = branch.setdefault("relationships", {})
        for entity_type in ("character", "npc", "location"):
            relationships.setdefault(entity_type, {})
    return state


def backfill_gameplay_templates_and_instances(apps, schema_editor):
    AbilityInstance = apps.get_model("core", "AbilityInstance")
    AbilityTemplate = apps.get_model("core", "AbilityTemplate")
    CharacterInstance = apps.get_model("core", "CharacterInstance")
    CharacterTemplate = apps.get_model("core", "CharacterTemplate")
    CharacterTemplateItem = apps.get_model("core", "CharacterTemplateItem")
    ItemInstance = apps.get_model("core", "ItemInstance")
    ItemTemplate = apps.get_model("core", "ItemTemplate")
    NPCInstance = apps.get_model("core", "NPCInstance")
    alias = schema_editor.connection.alias

    character_templates = list(CharacterTemplate.objects.using(alias).all())
    templates_by_class = {
        row.character_template.get("class"): row for row in character_templates
    }
    item_templates = {}
    for character_template in character_templates:
        definition = character_template.character_template
        for ability in definition.get("abilities", []):
            uses = ability.get("uses")
            AbilityTemplate.objects.using(alias).update_or_create(
                character_template=character_template,
                ability_key=ability["key"],
                defaults={
                    "name": ability["name"],
                    "active": True,
                    "category": ability.get("category", "combat"),
                    "description": ability.get("explanation", ability["name"]),
                    "resolution_json": ability.get("resolution", {}),
                    "effect_json": ability.get("effect", {}),
                    "max_uses": uses.get("maximum") if uses else None,
                    "recharge": uses.get("recharge") if uses else None,
                },
            )
        for item in definition.get("gear", []):
            item_template, _ = ItemTemplate.objects.using(alias).update_or_create(
                template_key=item["key"],
                defaults={
                    "name": item["name"],
                    "active": True,
                    "definition_json": {
                        "public_info": {"summary": item["name"]},
                        "dm_only": {},
                    },
                    "mechanics_json": {
                        "consumable": False,
                        "transferable": True,
                    },
                },
            )
            item_templates[item["key"]] = item_template
            CharacterTemplateItem.objects.using(alias).update_or_create(
                character_template=character_template,
                item_template=item_template,
                defaults={"starting_quantity": item.get("quantity", 1)},
            )

    for character in CharacterInstance.objects.using(alias).all():
        mechanics_json = dict(character.mechanics_json or {})
        template_json = dict(character.template_json or {})
        state_json = with_relationship_buckets(character.state_json)
        mechanics_json.pop("conditions", None)
        template_json.pop("conditions", None)
        update_fields = []
        if mechanics_json != character.mechanics_json:
            character.mechanics_json = mechanics_json
            update_fields.append("mechanics_json")
        if template_json != character.template_json:
            character.template_json = template_json
            update_fields.append("template_json")
        if state_json != character.state_json:
            character.state_json = state_json
            update_fields.append("state_json")
        if update_fields:
            character.save(update_fields=update_fields)

        character_template = templates_by_class.get(
            character.template_json.get("class")
        )
        if character_template is None:
            continue
        for ability_template in AbilityTemplate.objects.using(alias).filter(
            character_template=character_template,
            active=True,
        ):
            AbilityInstance.objects.using(alias).get_or_create(
                character=character,
                template=ability_template,
                defaults={"remaining_uses": ability_template.max_uses},
            )
        for item in character.template_json.get("gear", []):
            item_template = item_templates[item["key"]]
            existing_count = ItemInstance.objects.using(alias).filter(
                dnd_session_id=character.dnd_session_id,
                template=item_template,
                owner_type="character",
                owner_id=character.id,
                status="active",
            ).count()
            for _ in range(max(0, item.get("quantity", 1) - existing_count)):
                ItemInstance.objects.using(alias).create(
                    dnd_session_id=character.dnd_session_id,
                    template=item_template,
                    name=item_template.name,
                    owner_type="character",
                    owner_id=character.id,
                )

    for npc in NPCInstance.objects.using(alias).select_related("template"):
        update_fields = []
        if npc.template_id:
            npc.mechanics_json = npc.template.mechanics_json
            update_fields.append("mechanics_json")
        state_json = with_relationship_buckets(npc.state_json)
        if state_json != npc.state_json:
            npc.state_json = state_json
            update_fields.append("state_json")
        if update_fields:
            npc.save(update_fields=update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_campaign_initialization_and_relationships"),
    ]

    operations = [
        migrations.CreateModel(
            name="ItemTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("template_key", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=120)),
                ("active", models.BooleanField(default=True)),
                ("definition_json", models.JSONField(default=core.models.empty_visibility_state)),
                ("mechanics_json", models.JSONField(default=dict)),
                ("public_embedding", pgvector.django.vector.VectorField(blank=True, dimensions=3072, null=True)),
                ("dm_embedding", pgvector.django.vector.VectorField(blank=True, dimensions=3072, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "item_template", "ordering": ["template_key"]},
        ),
        migrations.AddField(
            model_name="characterinstance",
            name="modifiers_json",
            field=models.JSONField(default=core.models.empty_roll_modifiers),
        ),
        migrations.AlterField(
            model_name="characterinstance",
            name="state_json",
            field=models.JSONField(default=core.models.empty_relationship_state),
        ),
        migrations.AddField(
            model_name="npcinstance",
            name="mechanics_json",
            field=models.JSONField(default=core.models.empty_npc_mechanics),
        ),
        migrations.AddField(
            model_name="npcinstance",
            name="modifiers_json",
            field=models.JSONField(default=core.models.empty_roll_modifiers),
        ),
        migrations.AlterField(
            model_name="npcinstance",
            name="state_json",
            field=models.JSONField(default=core.models.empty_relationship_state),
        ),
        migrations.AddField(
            model_name="npctemplate",
            name="mechanics_json",
            field=models.JSONField(default=core.models.empty_npc_mechanics),
        ),
        migrations.AlterField(
            model_name="retrievedcontextrecord",
            name="source_type",
            field=models.CharField(choices=[("world_lore", "World lore"), ("npc", "NPC"), ("location", "Location"), ("quest", "Quest"), ("world_event", "World event"), ("ability", "Ability"), ("item", "Item")], max_length=32),
        ),
        migrations.CreateModel(
            name="CharacterTemplateItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("starting_quantity", models.PositiveIntegerField(default=1)),
                ("character_template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="starting_items", to="core.charactertemplate")),
                ("item_template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="character_loadouts", to="core.itemtemplate")),
            ],
            options={"db_table": "character_template_item", "ordering": ["character_template", "item_template"]},
        ),
        migrations.CreateModel(
            name="AbilityTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ability_key", models.CharField(max_length=64)),
                ("name", models.CharField(max_length=120)),
                ("active", models.BooleanField(default=True)),
                ("category", models.CharField(choices=[("non_combat", "Non-combat"), ("combat", "Combat")], default="combat", max_length=16)),
                ("description", models.TextField()),
                ("resolution_json", models.JSONField(default=dict)),
                ("effect_json", models.JSONField(default=dict)),
                ("max_uses", models.PositiveIntegerField(blank=True, null=True)),
                ("recharge", models.CharField(blank=True, max_length=32, null=True)),
                ("embedding", pgvector.django.vector.VectorField(blank=True, dimensions=3072, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("character_template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ability_templates", to="core.charactertemplate")),
            ],
            options={"db_table": "ability_template", "ordering": ["character_template", "ability_key"]},
        ),
        migrations.CreateModel(
            name="AbilityInstance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("remaining_uses", models.PositiveIntegerField(blank=True, null=True)),
                ("state_json", models.JSONField(default=core.models.empty_visibility_state)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("character", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="abilities", to="core.characterinstance")),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="instances", to="core.abilitytemplate")),
            ],
            options={"db_table": "ability_instance", "ordering": ["character", "template"]},
        ),
        migrations.CreateModel(
            name="ItemInstance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120)),
                ("owner_type", models.CharField(blank=True, choices=[("character", "Character"), ("npc", "NPC")], max_length=16, null=True)),
                ("owner_id", models.PositiveBigIntegerField(blank=True, null=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("consumed", "Consumed"), ("destroyed", "Destroyed")], default="active", max_length=16)),
                ("state_json", models.JSONField(default=core.models.empty_visibility_state)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("current_location", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="items", to="core.locationinstance")),
                ("dnd_session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="core.dndsession")),
                ("template", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="instances", to="core.itemtemplate")),
            ],
            options={
                "db_table": "item_instance",
                "ordering": ["id"],
                "indexes": [
                    models.Index(fields=["dnd_session", "owner_type", "owner_id", "status"], name="item_session_owner_idx"),
                    models.Index(fields=["dnd_session", "current_location", "status"], name="item_session_loc_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="iteminstance",
            constraint=models.CheckConstraint(check=models.Q(models.Q(("owner_id__isnull", True), ("owner_type__isnull", True)), models.Q(("owner_id__isnull", False), ("owner_type__isnull", False)), _connector="OR"), name="item_owner_pair_ck"),
        ),
        migrations.AddConstraint(
            model_name="iteminstance",
            constraint=models.CheckConstraint(check=models.Q(("owner_id__isnull", True), ("current_location__isnull", True), _connector="OR"), name="item_owner_location_exclusive_ck"),
        ),
        migrations.AddConstraint(
            model_name="iteminstance",
            constraint=models.CheckConstraint(check=models.Q(models.Q(("status", "active"), _negated=True), ("owner_id__isnull", False), ("current_location__isnull", False), _connector="OR"), name="item_active_placement_ck"),
        ),
        migrations.AddConstraint(
            model_name="charactertemplateitem",
            constraint=models.UniqueConstraint(fields=("character_template", "item_template"), name="uniq_character_starting_item"),
        ),
        migrations.AddConstraint(
            model_name="abilitytemplate",
            constraint=models.UniqueConstraint(fields=("character_template", "ability_key"), name="uniq_character_ability_tpl"),
        ),
        migrations.AddConstraint(
            model_name="abilityinstance",
            constraint=models.UniqueConstraint(fields=("character", "template"), name="uniq_character_ability_instance"),
        ),
        migrations.RunPython(
            backfill_gameplay_templates_and_instances,
            migrations.RunPython.noop,
        ),
    ]
