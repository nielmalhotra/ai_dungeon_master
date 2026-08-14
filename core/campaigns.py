from copy import deepcopy

from django.db import transaction

from .models import (
    CharacterInstance,
    DndSession,
    LocationInstance,
    LocationTemplate,
    NPCInstance,
    NPCTemplate,
    QuestInstance,
    QuestTemplate,
    WorldLore,
    WorldLoreChunkTemplate,
)
from .scenario_lore import grouped_known_entities, load_scenario_release


class CampaignCreationError(RuntimeError):
    pass


class ScenarioNotReadyError(CampaignCreationError):
    pass


class CampaignNotActiveError(RuntimeError):
    pass


def _active_scenario_templates(scenario_key):
    template_groups = {
        "locations": list(
            LocationTemplate.objects.filter(
                scenario_key=scenario_key,
                active=True,
            ).order_by("id")
        ),
        "npcs": list(
            NPCTemplate.objects.filter(
                scenario_key=scenario_key,
                active=True,
            ).order_by("id")
        ),
        "quests": list(
            QuestTemplate.objects.filter(
                scenario_key=scenario_key,
                active=True,
            ).order_by("id")
        ),
        "world_lore": list(
            WorldLoreChunkTemplate.objects.filter(
                scenario_key=scenario_key,
                active=True,
            ).order_by("id")
        ),
    }
    if any(not templates for templates in template_groups.values()):
        raise ScenarioNotReadyError(
            f"Scenario {scenario_key!r} does not have a complete active release."
        )
    versions = {
        template.version
        for templates in template_groups.values()
        for template in templates
    }
    if len(versions) != 1:
        raise ScenarioNotReadyError(
            f"Scenario {scenario_key!r} has inconsistent active template versions."
        )
    return versions.pop(), template_groups


def _validate_selected_characters(selected_characters):
    if len(selected_characters) != 3:
        raise CampaignCreationError("A campaign requires exactly three characters.")
    template_ids = [template.id for template, _ in selected_characters]
    if len(set(template_ids)) != len(template_ids):
        raise CampaignCreationError("Character templates must be unique.")
    if any(not name.strip() for _, name in selected_characters):
        raise CampaignCreationError("Every character requires a name.")


def _templates_by_definition(templates):
    grouped = {}
    for template_type, group_name in (
        ("location", "locations"),
        ("npc", "npcs"),
        ("quest", "quests"),
        ("world_lore", "world_lore"),
    ):
        for template in templates[group_name]:
            grouped.setdefault(
                (template_type, template.definition_uuid),
                [],
            ).append(template)
    return grouped


def _verify_release_matches_templates(release, templates_by_definition):
    for definition in release.definitions:
        if not templates_by_definition.get(
            (definition.definition_type, definition.definition_uuid)
        ):
            raise ScenarioNotReadyError(
                f"Active templates are missing {definition.source_file}."
            )
    expected_definitions = {
        (definition.definition_type, definition.definition_uuid)
        for definition in release.definitions
    }
    if set(templates_by_definition) != expected_definitions:
        raise ScenarioNotReadyError(
            "Active templates do not match the synchronized scenario release."
        )


def _runtime_relationships(template, runtime_by_template):
    relationships = []
    for relationship in template.relationships_json:
        target = relationship["target"]
        runtime_entity = runtime_by_template.get((target["type"], target["id"]))
        if runtime_entity is None:
            raise ScenarioNotReadyError(
                f"Scenario relationship references missing {target['type']} "
                f"template {target['id']}."
            )
        relationships.append(
            {
                "relation": relationship["relation"],
                "target": {
                    "type": target["type"],
                    "id": runtime_entity.id,
                },
            }
        )
    return relationships


@transaction.atomic
def create_campaign(
    *,
    user,
    selected_characters,
    scenario_key,
    scenario_dir=None,
):
    _validate_selected_characters(selected_characters)
    user = type(user).objects.select_for_update().get(pk=user.pk)
    if DndSession.objects.filter(
        user=user,
        status=DndSession.Status.ACTIVE,
    ).exists():
        raise CampaignCreationError("The user already has an active campaign.")

    version, templates = _active_scenario_templates(scenario_key)
    release = load_scenario_release(
        scenario_dir=scenario_dir,
        scenario_key=scenario_key,
        version=version,
    )
    templates_by_definition = _templates_by_definition(templates)
    _verify_release_matches_templates(release, templates_by_definition)

    campaign = DndSession.objects.create(
        user=user,
        status=DndSession.Status.ACTIVE,
        scenario_key=scenario_key,
        scenario_version=version,
        opening_text=release.initialization.opening,
        initially_known_entities_json=grouped_known_entities(release),
        state_json={
            "public_info": {},
            "dm_only": (
                {"summary": release.initialization.dm_only}
                if release.initialization.dm_only
                else {}
            ),
        },
    )

    locations_by_template = {}
    for template in templates["locations"]:
        locations_by_template[template.id] = LocationInstance.objects.create(
            dnd_session=campaign,
            template=template,
            name=template.name,
            status=template.initial_status,
            state_json=deepcopy(template.definition_json),
        )
    for template in templates["locations"]:
        if template.parent_template_id:
            location = locations_by_template[template.id]
            location.parent_location = locations_by_template[
                template.parent_template_id
            ]
            location.save(update_fields=["parent_location"])

    npcs_by_template = {}
    for template in templates["npcs"]:
        npcs_by_template[template.id] = NPCInstance.objects.create(
            dnd_session=campaign,
            template=template,
            name=template.name,
            current_location=(
                locations_by_template[template.initial_location_template_id]
                if template.initial_location_template_id
                else None
            ),
            status=template.initial_status,
            state_json=deepcopy(template.definition_json),
        )

    quests_by_template = {}
    main_quest_template = templates_by_definition[
        ("quest", release.initialization.main_quest_uuid)
    ][0]
    for template in templates["quests"]:
        quests_by_template[template.id] = QuestInstance.objects.create(
            dnd_session=campaign,
            template=template,
            title=template.title,
            status=(
                QuestInstance.Status.ACTIVE
                if template.id == main_quest_template.id
                else template.initial_status
            ),
            state_json=deepcopy(template.definition_json),
        )

    lore_by_template = {}
    for template in templates["world_lore"]:
        lore_by_template[template.id] = WorldLore.objects.create(
            dnd_session=campaign,
            template=template,
        )

    runtime_by_template = {}
    runtime_by_template.update(
        (("location", template_id), instance)
        for template_id, instance in locations_by_template.items()
    )
    runtime_by_template.update(
        (("npc", template_id), instance)
        for template_id, instance in npcs_by_template.items()
    )
    runtime_by_template.update(
        (("quest", template_id), instance)
        for template_id, instance in quests_by_template.items()
    )
    runtime_by_template.update(
        (("world_lore", template_id), instance)
        for template_id, instance in lore_by_template.items()
    )

    for template_group, instances in (
        (templates["locations"], locations_by_template),
        (templates["npcs"], npcs_by_template),
        (templates["quests"], quests_by_template),
        (templates["world_lore"], lore_by_template),
    ):
        for template in template_group:
            instance = instances[template.id]
            instance.relationships_json = _runtime_relationships(
                template,
                runtime_by_template,
            )
            instance.save(update_fields=["relationships_json"])

    starting_location_template = templates_by_definition[
        ("location", release.initialization.starting_location_uuid)
    ][0]
    starting_location = locations_by_template[starting_location_template.id]
    for template, name in selected_characters:
        CharacterInstance.objects.create(
            dnd_session=campaign,
            name=name.strip(),
            template_json=deepcopy(template.character_template),
            mechanics_json=deepcopy(template.character_template),
            current_location=starting_location,
            relationships_json=[
                {
                    "relation": "located_in",
                    "target": {
                        "type": "location",
                        "id": starting_location.id,
                    },
                }
            ],
        )

    campaign.current_location = starting_location
    campaign.main_quest = quests_by_template[main_quest_template.id]
    campaign.save(
        update_fields=["current_location", "main_quest", "updated_at"]
    )
    return campaign


@transaction.atomic
def finish_quest(quest):
    quest = QuestInstance.objects.select_for_update().select_related(
        "dnd_session"
    ).get(pk=quest.pk)
    campaign = DndSession.objects.select_for_update().get(
        pk=quest.dnd_session_id
    )
    if campaign.status != DndSession.Status.ACTIVE:
        raise CampaignNotActiveError("The campaign no longer accepts changes.")
    quest.status = QuestInstance.Status.FINISHED
    quest.save(update_fields=["status", "updated_at"])
    if campaign.main_quest_id == quest.id:
        campaign.status = DndSession.Status.COMPLETED
        campaign.save(update_fields=["status", "updated_at"])
    return campaign.status == DndSession.Status.COMPLETED


@transaction.atomic
def abandon_campaign(campaign):
    campaign = DndSession.objects.select_for_update().get(pk=campaign.pk)
    if campaign.status == DndSession.Status.ACTIVE:
        campaign.status = DndSession.Status.ABANDONED
        campaign.save(update_fields=["status", "updated_at"])
    return campaign
