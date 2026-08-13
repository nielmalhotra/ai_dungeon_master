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


class CampaignCreationError(RuntimeError):
    pass


class ScenarioNotReadyError(CampaignCreationError):
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

    starting_locations = [
        template
        for template in template_groups["locations"]
        if template.is_starting_location
    ]
    if len(starting_locations) != 1:
        raise ScenarioNotReadyError(
            f"Scenario {scenario_key!r} must have one active starting location."
        )
    return versions.pop(), template_groups, starting_locations[0]


def _initial_entity_state(template):
    definition = deepcopy(template.definition_json)
    return {
        "public_info": (
            definition.get("public_info", {}) if template.initially_known else {}
        ),
        "dm_only": definition.get("dm_only", {}),
    }


def _validate_selected_characters(selected_characters):
    if len(selected_characters) != 3:
        raise CampaignCreationError("A campaign requires exactly three characters.")
    template_ids = [template.id for template, _ in selected_characters]
    if len(set(template_ids)) != len(template_ids):
        raise CampaignCreationError("Character templates must be unique.")
    if any(not name.strip() for _, name in selected_characters):
        raise CampaignCreationError("Every character requires a name.")


def _runtime_reference(template_reference, runtime_by_template):
    entity_type = template_reference["type"]
    template_id = template_reference["id"]
    runtime_entity = runtime_by_template.get((entity_type, template_id))
    if runtime_entity is None:
        raise ScenarioNotReadyError(
            f"Scenario relationship references missing {entity_type} template "
            f"{template_id}."
        )
    return {"type": entity_type, "id": runtime_entity.id}


@transaction.atomic
def create_campaign(*, user, selected_characters, scenario_key):
    _validate_selected_characters(selected_characters)
    user = type(user).objects.select_for_update().get(pk=user.pk)
    if DndSession.objects.filter(user=user, active=True).exists():
        raise CampaignCreationError("The user already has an active campaign.")

    version, templates, starting_location_template = _active_scenario_templates(
        scenario_key
    )
    campaign = DndSession.objects.create(
        user=user,
        active=True,
        scenario_key=scenario_key,
        scenario_version=version,
    )

    locations_by_template = {}
    for template in templates["locations"]:
        locations_by_template[template.id] = LocationInstance.objects.create(
            dnd_session=campaign,
            template=template,
            name=template.name,
            state_json=_initial_entity_state(template),
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
            state_json=_initial_entity_state(template),
        )

    quests_by_template = {}
    for template in templates["quests"]:
        quests_by_template[template.id] = QuestInstance.objects.create(
            dnd_session=campaign,
            template=template,
            title=template.title,
            status=template.initial_status,
            state_json=_initial_entity_state(template),
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

    for template in templates["quests"]:
        quest = quests_by_template[template.id]
        quest.related_entities_json = [
            _runtime_reference(reference, runtime_by_template)
            for reference in template.related_templates_json
        ]
        quest.save(update_fields=["related_entities_json"])

    starting_location = locations_by_template[starting_location_template.id]
    for template, name in selected_characters:
        CharacterInstance.objects.create(
            dnd_session=campaign,
            name=name.strip(),
            template_json=deepcopy(template.character_template),
            mechanics_json=deepcopy(template.character_template),
            current_location=starting_location,
        )

    starting_state = _initial_entity_state(starting_location_template)
    campaign.current_location = starting_location
    campaign.state_json = {
        "public_info": deepcopy(starting_state["public_info"]),
        "dm_only": deepcopy(starting_state["dm_only"]),
    }
    campaign.save(
        update_fields=["current_location", "state_json", "updated_at"]
    )
    return campaign
