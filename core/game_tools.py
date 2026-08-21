import re
import time
from copy import deepcopy
from random import SystemRandom
from uuid import uuid4

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import (
    AbilityInstance,
    AbilityTemplate,
    AgentRun,
    CampaignTurn,
    CharacterInstance,
    DndSession,
    ItemInstance,
    LocationInstance,
    NPCInstance,
    QuestConditionInstance,
    QuestInstance,
    ToolCallRecord,
    WorldEvent,
)
from .schemas import (
    AddRollModifierInput,
    AdvanceQuestInput,
    ConsumeItemInput,
    EntityStateUpdate,
    MoveCharacterInput,
    MoveItemInput,
    MoveNPCInput,
    RemoveRollModifierInput,
    RelationshipSourceType,
    RelationshipTargetType,
    RestCharacterInput,
    RollCheckInput,
    RollContestInput,
    RollDiceInput,
    RollModifierDuration,
    RollModifierEntry,
    RollModifierKind,
    RollSaveInput,
    RollType,
    RuntimeEntityReference,
    RuntimeEntityType,
    ToolResult,
    TransferItemInput,
    UpdateItemStateInput,
    UpdateRelationshipInput,
    UseAbilityInput,
)


_DICE_PATTERN = re.compile(r"^(?P<count>[1-9]\d*)d(?P<sides>[1-9]\d*)(?P<modifier>[+-]\d+)?$")
_SYSTEM_RANDOM = SystemRandom()
_OWNER_TYPES = {
    RuntimeEntityType.CHARACTER: ItemInstance.OwnerType.CHARACTER,
    RuntimeEntityType.NPC: ItemInstance.OwnerType.NPC,
}
_RELATIONSHIP_SOURCE_MODELS = {
    RelationshipSourceType.CHARACTER: CharacterInstance,
    RelationshipSourceType.NPC: NPCInstance,
}
_RELATIONSHIP_TARGET_MODELS = {
    RelationshipTargetType.CHARACTER: CharacterInstance,
    RelationshipTargetType.NPC: NPCInstance,
    RelationshipTargetType.LOCATION: LocationInstance,
}
_RELATIONSHIP_ENTITY_TYPES = ("character", "npc", "location")
_NEXT_QUEST_STATUS = {
    QuestInstance.Status.HIDDEN: QuestInstance.Status.AVAILABLE,
    QuestInstance.Status.AVAILABLE: QuestInstance.Status.ACTIVE,
}


class GameToolError(RuntimeError):
    def __init__(self, message, code="invalid_tool_call"):
        super().__init__(message)
        self.code = code


def _dump(model):
    return model.model_dump(mode="json")


def _entity_ref(entity_type, entity_id):
    return {"type": entity_type, "id": entity_id}


def _tool_campaign(tool_call, require_active=True):
    campaign_id = tool_call.agent_run.campaign_turn.dnd_session_id
    campaign = DndSession.objects.select_for_update().get(pk=campaign_id)
    if require_active and campaign.status != DndSession.Status.ACTIVE:
        raise GameToolError("The campaign no longer accepts changes.", "campaign_inactive")
    return campaign


def _create_world_event(
    tool_call,
    event_type,
    related_entities,
    public_info,
    dm_only=None,
    importance=3,
):
    turn = CampaignTurn.objects.select_for_update().get(
        pk=tool_call.agent_run.campaign_turn_id
    )
    sequence_number = (
        turn.world_events.aggregate(value=Max("sequence_number"))["value"] or 0
    ) + 1
    return WorldEvent.objects.create(
        campaign_turn=turn,
        tool_call=tool_call,
        sequence_number=sequence_number,
        event_type=event_type,
        related_entities_json=related_entities,
        state_json={
            "public_info": public_info,
            "dm_only": dm_only or {},
        },
        importance=importance,
    )


def _actor_from_reference(reference, campaign, lock=True):
    queryset_method = "select_for_update" if lock else "all"
    if reference.type == RuntimeEntityType.CHARACTER:
        queryset = getattr(CharacterInstance.objects, queryset_method)()
    elif reference.type == RuntimeEntityType.NPC:
        queryset = getattr(NPCInstance.objects, queryset_method)()
    else:
        raise GameToolError("Roll actors must be a character or NPC.")
    try:
        return queryset.get(pk=reference.id, dnd_session=campaign)
    except queryset.model.DoesNotExist as exc:
        raise GameToolError("The roll actor does not belong to this campaign.") from exc


def _item_owner(reference, campaign):
    if reference.type not in _OWNER_TYPES:
        raise GameToolError("An item owner must be a character or NPC.")
    return _actor_from_reference(reference, campaign)


def _parse_modifiers(actor):
    try:
        return [RollModifierEntry.model_validate(value) for value in actor.modifiers_json]
    except Exception as exc:
        raise GameToolError(
            f"{actor} has invalid authoritative roll modifier state.",
            "invalid_modifier_state",
        ) from exc


def _modifier_applies(modifier, roll_type, attribute, skill, turn_number):
    if (
        modifier.duration == RollModifierDuration.CURRENT_TURN
        and modifier.created_turn != turn_number
    ):
        return False
    applicability = modifier.applies_to
    allowed_roll_types = set(applicability.roll_types)
    if roll_type == RollType.CONTEST and RollType.ABILITY_CHECK in allowed_roll_types:
        allowed_roll_types.add(RollType.CONTEST)
    if allowed_roll_types and roll_type not in allowed_roll_types:
        return False
    if applicability.attributes and attribute not in applicability.attributes:
        return False
    if applicability.skills and skill not in applicability.skills:
        return False
    return True


def _roll_faces(count, sides=20):
    return [_SYSTEM_RANDOM.randint(1, sides) for _ in range(count)]


def _actor_roll_context(actor, roll_type, attribute, skill, turn_number):
    mechanics = actor.mechanics_json or {}
    attributes = mechanics.get("attributes", {})
    if attribute.value not in attributes:
        raise GameToolError(f"{actor} has no {attribute.value} attribute.")
    attribute_modifier = attributes[attribute.value]
    if not isinstance(attribute_modifier, int):
        raise GameToolError("The actor's attribute modifier is invalid.")

    trained_skills = mechanics.get("trained_skills", [])
    strong_saves = mechanics.get("strong_saves", [])
    training_bonus = 0
    strong_save_bonus = 0
    if roll_type in (RollType.ABILITY_CHECK, RollType.CONTEST):
        if skill is not None and skill.value in trained_skills:
            training_bonus = 2
    elif roll_type == RollType.SAVING_THROW:
        if isinstance(strong_saves, dict):
            strong_save_bonus = 2 if attribute.value in strong_saves else 0
        elif attribute.value in strong_saves:
            strong_save_bonus = 2

    applicable = []
    retained = []
    for modifier in _parse_modifiers(actor):
        if (
            modifier.duration == RollModifierDuration.CURRENT_TURN
            and modifier.created_turn < turn_number
        ):
            continue
        if _modifier_applies(modifier, roll_type, attribute, skill, turn_number):
            applicable.append(modifier)
            if modifier.duration not in (
                RollModifierDuration.CURRENT_ROLL,
                RollModifierDuration.NEXT_APPLICABLE_ROLL,
            ):
                retained.append(modifier)
        else:
            retained.append(modifier)

    advantage_sources = sum(
        modifier.value
        for modifier in applicable
        if modifier.kind == RollModifierKind.ADVANTAGE
    )
    disadvantage_sources = sum(
        modifier.value
        for modifier in applicable
        if modifier.kind == RollModifierKind.DISADVANTAGE
    )
    flat_bonus = sum(
        modifier.value
        for modifier in applicable
        if modifier.kind == RollModifierKind.FLAT_BONUS
    )
    flat_penalty = sum(
        modifier.value
        for modifier in applicable
        if modifier.kind == RollModifierKind.FLAT_PENALTY
    )
    return {
        "attribute_modifier": attribute_modifier,
        "training_bonus": training_bonus,
        "strong_save_bonus": strong_save_bonus,
        "flat_modifier": flat_bonus - flat_penalty,
        "advantage_sources": advantage_sources,
        "disadvantage_sources": disadvantage_sources,
        "net_advantage": advantage_sources - disadvantage_sources,
        "applied_modifiers": applicable,
        "retained_modifiers": retained,
    }


def _perform_d20_roll(context):
    net_advantage = context["net_advantage"]
    faces = _roll_faces(1 + abs(net_advantage))
    if net_advantage > 0:
        kept = max(faces)
    elif net_advantage < 0:
        kept = min(faces)
    else:
        kept = faces[0]
    ordinary_modifier = (
        context["attribute_modifier"]
        + context["training_bonus"]
        + context["strong_save_bonus"]
        + context["flat_modifier"]
    )
    return {
        "faces": faces,
        "kept": kept,
        "ordinary_modifier": ordinary_modifier,
        "total": kept + ordinary_modifier,
    }


def _save_retained_modifiers(actor, context):
    retained = [_dump(value) for value in context["retained_modifiers"]]
    if actor.modifiers_json != retained:
        actor.modifiers_json = retained
        actor.save(update_fields=["modifiers_json", "updated_at"])


def _roll_breakdown(actor, roll_type, attribute, skill, context, rolled):
    return {
        "actor": _entity_ref(
            RuntimeEntityType.CHARACTER.value
            if isinstance(actor, CharacterInstance)
            else RuntimeEntityType.NPC.value,
            actor.id,
        ),
        "roll_type": roll_type.value,
        "attribute": attribute.value,
        "skill": skill.value if skill else None,
        "faces": rolled["faces"],
        "kept": rolled["kept"],
        "attribute_modifier": context["attribute_modifier"],
        "training_bonus": context["training_bonus"],
        "strong_save_bonus": context["strong_save_bonus"],
        "flat_modifier": context["flat_modifier"],
        "advantage_sources": context["advantage_sources"],
        "disadvantage_sources": context["disadvantage_sources"],
        "net_advantage": context["net_advantage"],
        "ordinary_modifier": rolled["ordinary_modifier"],
        "total": rolled["total"],
        "consumed_modifier_ids": [
            str(modifier.id)
            for modifier in context["applied_modifiers"]
            if modifier.duration
            in (
                RollModifierDuration.CURRENT_ROLL,
                RollModifierDuration.NEXT_APPLICABLE_ROLL,
            )
        ],
    }


def _record_consumed_modifier_event(tool_call, actor, breakdown):
    if not breakdown["consumed_modifier_ids"]:
        return None
    entity_type = (
        RuntimeEntityType.CHARACTER.value
        if isinstance(actor, CharacterInstance)
        else RuntimeEntityType.NPC.value
    )
    return _create_world_event(
        tool_call,
        "roll_modifiers_consumed",
        [_entity_ref(entity_type, actor.id)],
        {
            "summary": f"Temporary roll modifiers were consumed for {actor}.",
            "modifier_ids": breakdown["consumed_modifier_ids"],
        },
        importance=1,
    )


def _handle_roll_dice(tool_call, arguments):
    _tool_campaign(tool_call)
    match = _DICE_PATTERN.fullmatch(arguments.dice)
    if match is None:
        raise GameToolError("The dice expression is invalid.")
    count = int(match.group("count"))
    sides = int(match.group("sides"))
    modifier = int(match.group("modifier") or 0)
    if count > 100 or sides > 1000 or abs(modifier) > 1000:
        raise GameToolError("The dice expression exceeds the supported limits.")
    faces = _roll_faces(count, sides)
    total = sum(faces) + modifier
    return ToolResult(
        success=True,
        message=f"Rolled {arguments.dice} for {arguments.reason}.",
        data={
            "dice": arguments.dice,
            "faces": faces,
            "modifier": modifier,
            "total": total,
        },
    )


def _handle_roll_check(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    actor = _actor_from_reference(arguments.actor, campaign)
    turn_number = tool_call.agent_run.campaign_turn.turn_number
    context = _actor_roll_context(
        actor,
        RollType.ABILITY_CHECK,
        arguments.attribute,
        arguments.skill,
        turn_number,
    )
    rolled = _perform_d20_roll(context)
    _save_retained_modifiers(actor, context)
    breakdown = _roll_breakdown(
        actor,
        RollType.ABILITY_CHECK,
        arguments.attribute,
        arguments.skill,
        context,
        rolled,
    )
    breakdown["dc"] = arguments.dc
    breakdown["success"] = (
        rolled["total"] >= arguments.dc if arguments.dc is not None else None
    )
    event = _record_consumed_modifier_event(tool_call, actor, breakdown)
    return ToolResult(
        success=True,
        message=f"Resolved {arguments.reason} for {actor}.",
        affected_entities=[arguments.actor],
        world_event_id=event.id if event else None,
        data=breakdown,
    )


def _handle_roll_save(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    actor = _actor_from_reference(arguments.actor, campaign)
    turn_number = tool_call.agent_run.campaign_turn.turn_number
    context = _actor_roll_context(
        actor,
        RollType.SAVING_THROW,
        arguments.attribute,
        None,
        turn_number,
    )
    rolled = _perform_d20_roll(context)
    _save_retained_modifiers(actor, context)
    breakdown = _roll_breakdown(
        actor,
        RollType.SAVING_THROW,
        arguments.attribute,
        None,
        context,
        rolled,
    )
    breakdown["dc"] = arguments.dc
    breakdown["success"] = (
        rolled["total"] >= arguments.dc if arguments.dc is not None else None
    )
    event = _record_consumed_modifier_event(tool_call, actor, breakdown)
    return ToolResult(
        success=True,
        message=f"Resolved {arguments.reason} for {actor}.",
        affected_entities=[arguments.actor],
        world_event_id=event.id if event else None,
        data=breakdown,
    )


def _handle_roll_contest(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    actor = _actor_from_reference(arguments.actor, campaign)
    opponent = _actor_from_reference(arguments.opponent, campaign)
    if actor.pk == opponent.pk and type(actor) is type(opponent):
        raise GameToolError("A contest requires two different participants.")
    turn_number = tool_call.agent_run.campaign_turn.turn_number
    actor_context = _actor_roll_context(
        actor,
        RollType.CONTEST,
        arguments.actor_attribute,
        arguments.actor_skill,
        turn_number,
    )
    opponent_context = _actor_roll_context(
        opponent,
        RollType.CONTEST,
        arguments.opponent_attribute,
        arguments.opponent_skill,
        turn_number,
    )
    rounds = []
    for _ in range(100):
        actor_roll = _perform_d20_roll(actor_context)
        opponent_roll = _perform_d20_roll(opponent_context)
        rounds.append(
            {
                "actor": _roll_breakdown(
                    actor,
                    RollType.CONTEST,
                    arguments.actor_attribute,
                    arguments.actor_skill,
                    actor_context,
                    actor_roll,
                ),
                "opponent": _roll_breakdown(
                    opponent,
                    RollType.CONTEST,
                    arguments.opponent_attribute,
                    arguments.opponent_skill,
                    opponent_context,
                    opponent_roll,
                ),
            }
        )
        if actor_roll["total"] != opponent_roll["total"]:
            break
    else:
        raise GameToolError("The contest could not resolve after repeated ties.")

    _save_retained_modifiers(actor, actor_context)
    _save_retained_modifiers(opponent, opponent_context)
    winner = arguments.actor if actor_roll["total"] > opponent_roll["total"] else arguments.opponent
    consumed = rounds[0]["actor"]["consumed_modifier_ids"] + rounds[0]["opponent"]["consumed_modifier_ids"]
    event = None
    if consumed:
        event = _create_world_event(
            tool_call,
            "roll_modifiers_consumed",
            [_dump(arguments.actor), _dump(arguments.opponent)],
            {
                "summary": "Temporary roll modifiers were consumed in a contest.",
                "modifier_ids": consumed,
            },
            importance=1,
        )
    return ToolResult(
        success=True,
        message=f"Resolved {arguments.reason}.",
        affected_entities=[arguments.actor, arguments.opponent],
        world_event_id=event.id if event else None,
        data={"rounds": rounds, "winner": _dump(winner)},
    )


def _handle_add_roll_modifier(tool_call, arguments):
    if arguments.kind not in (
        RollModifierKind.ADVANTAGE,
        RollModifierKind.DISADVANTAGE,
    ):
        raise GameToolError("The LLM may only create advantage or disadvantage sources.")
    if arguments.duration == RollModifierDuration.PERMANENT:
        raise GameToolError("Only templates or system rules may create permanent modifiers.")
    campaign = _tool_campaign(tool_call)
    actor = _actor_from_reference(arguments.actor, campaign)
    modifiers = _parse_modifiers(actor)
    same_source = next(
        (
            modifier
            for modifier in modifiers
            if modifier.source_key == arguments.source_key
            and modifier.applies_to == arguments.applies_to
        ),
        None,
    )
    if same_source is not None:
        if same_source.kind != arguments.kind:
            raise GameToolError(
                "The modifier source is already active with a different kind."
            )
        return ToolResult(
            success=True,
            message="That modifier source is already active.",
            affected_entities=[arguments.actor],
            data={"modifier": _dump(same_source), "created": False},
        )
    modifier = RollModifierEntry(
        id=uuid4(),
        kind=arguments.kind,
        value=1,
        source_key=arguments.source_key,
        reason=arguments.reason,
        applies_to=arguments.applies_to,
        duration=arguments.duration,
        created_turn=tool_call.agent_run.campaign_turn.turn_number,
    )
    modifiers.append(modifier)
    actor.modifiers_json = [_dump(value) for value in modifiers]
    actor.save(update_fields=["modifiers_json", "updated_at"])
    event = _create_world_event(
        tool_call,
        "roll_modifier_added",
        [_dump(arguments.actor)],
        {
            "summary": f"{actor} has an active {arguments.kind.value} source.",
            "modifier_id": str(modifier.id),
        },
        {"reason": arguments.reason},
        importance=1,
    )
    return ToolResult(
        success=True,
        message=f"Added {arguments.kind.value} to {actor}.",
        affected_entities=[arguments.actor],
        world_event_id=event.id,
        data={"modifier": _dump(modifier), "created": True},
    )


def _handle_remove_roll_modifier(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    actor = _actor_from_reference(arguments.actor, campaign)
    modifiers = _parse_modifiers(actor)
    retained = [value for value in modifiers if value.id != arguments.modifier_id]
    if len(retained) == len(modifiers):
        raise GameToolError("The modifier does not exist on that actor.")
    actor.modifiers_json = [_dump(value) for value in retained]
    actor.save(update_fields=["modifiers_json", "updated_at"])
    event = _create_world_event(
        tool_call,
        "roll_modifier_removed",
        [_dump(arguments.actor)],
        {
            "summary": f"A roll modifier was removed from {actor}.",
            "modifier_id": str(arguments.modifier_id),
        },
        importance=1,
    )
    return ToolResult(
        success=True,
        message=f"Removed the modifier from {actor}.",
        affected_entities=[arguments.actor],
        world_event_id=event.id,
    )


def _relationship_state_buckets(state):
    if not isinstance(state, dict):
        raise GameToolError("The source entity has invalid relationship state.")
    buckets = {}
    for visibility in ("public_info", "dm_only"):
        branch = state.setdefault(visibility, {})
        if not isinstance(branch, dict):
            raise GameToolError("The source entity has invalid visibility state.")
        relationships = branch.setdefault("relationships", {})
        if not isinstance(relationships, dict):
            raise GameToolError("The source entity has invalid relationship state.")
        for entity_type in _RELATIONSHIP_ENTITY_TYPES:
            bucket = relationships.setdefault(entity_type, {})
            if not isinstance(bucket, dict):
                raise GameToolError("The source entity has invalid relationship state.")
        buckets[visibility] = relationships
    return buckets


def _handle_update_relationship(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    source_model = _RELATIONSHIP_SOURCE_MODELS[arguments.entity_to_update.type]
    target_model = _RELATIONSHIP_TARGET_MODELS[arguments.target.type]
    try:
        source = source_model.objects.select_for_update().get(
            pk=arguments.entity_to_update.id,
            dnd_session=campaign,
        )
    except source_model.DoesNotExist as exc:
        raise GameToolError(
            "The relationship source does not belong to this campaign."
        ) from exc
    try:
        target = target_model.objects.get(
            pk=arguments.target.id,
            dnd_session=campaign,
        )
    except target_model.DoesNotExist as exc:
        raise GameToolError(
            "The relationship target does not belong to this campaign."
        ) from exc
    if (
        arguments.entity_to_update.type.value == arguments.target.type.value
        and source.id == target.id
    ):
        raise GameToolError("An entity cannot have a relationship with itself.")

    state = deepcopy(source.state_json)
    buckets = _relationship_state_buckets(state)
    target_type = arguments.target.type.value
    target_key = str(target.id)
    previous_public = deepcopy(
        buckets["public_info"][target_type].get(target_key, {})
    )
    previous_dm = deepcopy(buckets["dm_only"][target_type].get(target_key, {}))
    current_public = deepcopy(arguments.public_info_json)
    current_dm = deepcopy(arguments.dm_only_json)
    changed = (
        previous_public != current_public
        or previous_dm != current_dm
    )
    source_reference = RuntimeEntityReference(
        type=arguments.entity_to_update.type.value,
        id=source.id,
    )
    target_reference = RuntimeEntityReference(
        type=arguments.target.type.value,
        id=target.id,
    )
    if not changed:
        return ToolResult(
            success=True,
            message="The relationship already has the supplied state.",
            affected_entities=[source_reference, target_reference],
            data={"changed": False},
        )

    for visibility, replacement in (
        ("public_info", current_public),
        ("dm_only", current_dm),
    ):
        bucket = buckets[visibility][target_type]
        if replacement:
            bucket[target_key] = replacement
        else:
            bucket.pop(target_key, None)
    source.state_json = state
    source.save(update_fields=["state_json", "updated_at"])

    removed = not current_public and not current_dm
    public_event_state = {}
    if previous_public or current_public:
        public_event_state = {
            "summary": f"{source}'s relationship with {target} changed.",
            "previous_relationship": previous_public,
            "current_relationship": current_public,
        }
    event = _create_world_event(
        tool_call,
        "relationship_removed" if removed else "relationship_updated",
        [_dump(source_reference), _dump(target_reference)],
        public_event_state,
        {
            "summary": f"{source}'s relationship with {target} changed.",
            "previous_relationship": previous_dm,
            "current_relationship": current_dm,
        },
        importance=2,
    )
    return ToolResult(
        success=True,
        message=f"Updated {source}'s relationship with {target}.",
        affected_entities=[source_reference, target_reference],
        world_event_id=event.id,
        data={
            "changed": True,
            "removed": removed,
            "previous": {
                "public_info": previous_public,
                "dm_only": previous_dm,
            },
            "current": {
                "public_info": current_public,
                "dm_only": current_dm,
            },
        },
    )


def _replace_located_in_relationship(relationships, location_id):
    retained = [
        relationship
        for relationship in relationships
        if not (
            relationship.get("relation") == "located_in"
            and relationship.get("target", {}).get("type") == "location"
        )
    ]
    retained.append(
        {
            "relation": "located_in",
            "target": {"type": "location", "id": location_id},
        }
    )
    return retained


def _move_entity(tool_call, model, entity_id, destination_id, entity_type, reason):
    campaign = _tool_campaign(tool_call)
    try:
        entity = model.objects.select_for_update().get(
            pk=entity_id,
            dnd_session=campaign,
        )
        destination = LocationInstance.objects.select_for_update().get(
            pk=destination_id,
            dnd_session=campaign,
        )
    except (model.DoesNotExist, LocationInstance.DoesNotExist) as exc:
        raise GameToolError("The entity or destination does not belong to this campaign.") from exc
    previous_location_id = entity.current_location_id
    entity.current_location = destination
    entity.relationships_json = _replace_located_in_relationship(
        entity.relationships_json,
        destination.id,
    )
    entity.save(update_fields=["current_location", "relationships_json", "updated_at"])
    public_info = {}
    dm_only = {
        "summary": f"{entity} moved to {destination}. {reason}",
        "from_location_id": previous_location_id,
        "to_location_id": destination.id,
    }
    if entity_type == RuntimeEntityType.CHARACTER.value:
        public_info = {
            "summary": f"{entity} moved to {destination}.",
            "from_location_id": previous_location_id,
            "to_location_id": destination.id,
        }
    event = _create_world_event(
        tool_call,
        f"{entity_type}_moved",
        [
            _entity_ref(entity_type, entity.id),
            _entity_ref(RuntimeEntityType.LOCATION.value, destination.id),
        ],
        public_info,
        dm_only,
    )
    return ToolResult(
        success=True,
        message=f"Moved {entity} to {destination}.",
        affected_entities=[
            RuntimeEntityReference(type=entity_type, id=entity.id),
            RuntimeEntityReference(type=RuntimeEntityType.LOCATION, id=destination.id),
        ],
        world_event_id=event.id,
        data={
            "from_location_id": previous_location_id,
            "to_location_id": destination.id,
        },
    )


def _handle_move_character(tool_call, arguments):
    return _move_entity(
        tool_call,
        CharacterInstance,
        arguments.character_id,
        arguments.destination_location_id,
        RuntimeEntityType.CHARACTER.value,
        arguments.reason,
    )


def _handle_move_npc(tool_call, arguments):
    return _move_entity(
        tool_call,
        NPCInstance,
        arguments.npc_id,
        arguments.destination_location_id,
        RuntimeEntityType.NPC.value,
        arguments.reason,
    )


def _handle_use_ability(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    try:
        ability = AbilityInstance.objects.select_for_update().select_related(
            "character",
            "template",
        ).get(pk=arguments.ability_instance_id, character__dnd_session=campaign)
    except AbilityInstance.DoesNotExist as exc:
        raise GameToolError("The ability does not belong to this campaign.") from exc
    if ability.template.category != AbilityTemplate.Category.NON_COMBAT:
        raise GameToolError("Combat abilities are unavailable in the combatless prototype.")
    if ability.remaining_uses is not None and ability.remaining_uses == 0:
        raise GameToolError("The ability has no uses remaining.")

    target = None
    target_reference = arguments.target
    if target_reference is not None:
        target = _actor_from_reference(target_reference, campaign)
    effect = ability.template.effect_json
    added_modifier = None
    if effect.get("type") == "next_non_combat_check_advantage":
        modifier_target = target or ability.character
        modifier_target_type = (
            RuntimeEntityType.CHARACTER
            if isinstance(modifier_target, CharacterInstance)
            else RuntimeEntityType.NPC
        )
        modifiers = _parse_modifiers(modifier_target)
        source_key = f"ability:{ability.id}:turn:{tool_call.agent_run.campaign_turn.turn_number}"
        applies_to = {
            "roll_types": [RollType.ABILITY_CHECK, RollType.CONTEST],
            "attributes": [effect["attribute"]] if effect.get("attribute") else [],
            "skills": effect.get("skills", []),
        }
        existing = next(
            (value for value in modifiers if value.source_key == source_key),
            None,
        )
        if existing is None:
            added_modifier = RollModifierEntry(
                id=uuid4(),
                kind=RollModifierKind.ADVANTAGE,
                value=effect.get("sources", 1),
                source_key=source_key,
                reason=ability.template.name,
                applies_to=applies_to,
                duration=RollModifierDuration.NEXT_APPLICABLE_ROLL,
                created_turn=tool_call.agent_run.campaign_turn.turn_number,
            )
            modifiers.append(added_modifier)
            modifier_target.modifiers_json = [_dump(value) for value in modifiers]
            modifier_target.save(update_fields=["modifiers_json", "updated_at"])
        target_reference = RuntimeEntityReference(
            type=modifier_target_type,
            id=modifier_target.id,
        )

    if ability.remaining_uses is not None:
        ability.remaining_uses -= 1
        ability.save(update_fields=["remaining_uses", "updated_at"])

    related_entities = [
        _entity_ref(RuntimeEntityType.ABILITY.value, ability.id),
        _entity_ref(RuntimeEntityType.CHARACTER.value, ability.character_id),
    ]
    affected = [
        RuntimeEntityReference(type=RuntimeEntityType.ABILITY, id=ability.id),
        RuntimeEntityReference(
            type=RuntimeEntityType.CHARACTER,
            id=ability.character_id,
        ),
    ]
    if target_reference is not None:
        related_entities.append(_dump(target_reference))
        affected.append(target_reference)
    event = _create_world_event(
        tool_call,
        "ability_used",
        related_entities,
        {
            "summary": f"{ability.character} used {ability.template.name}.",
            "ability_instance_id": ability.id,
            "remaining_uses": ability.remaining_uses,
        },
        {"reason": arguments.reason},
    )
    return ToolResult(
        success=True,
        message=f"{ability.character} used {ability.template.name}.",
        affected_entities=affected,
        world_event_id=event.id,
        data={
            "remaining_uses": ability.remaining_uses,
            "effect": deepcopy(effect),
            "modifier": _dump(added_modifier) if added_modifier else None,
        },
    )


def _handle_rest_character(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    try:
        character = CharacterInstance.objects.select_for_update().get(
            pk=arguments.character_id,
            dnd_session=campaign,
        )
    except CharacterInstance.DoesNotExist as exc:
        raise GameToolError("The character does not belong to this campaign.") from exc
    mechanics = deepcopy(character.mechanics_json)
    hp = mechanics.get("hp", {})
    previous_hp = hp.get("current")
    maximum_hp = hp.get("maximum")
    if isinstance(maximum_hp, int):
        hp["current"] = maximum_hp
        mechanics["hp"] = hp
    retained_modifiers = [
        modifier
        for modifier in _parse_modifiers(character)
        if modifier.duration == RollModifierDuration.PERMANENT
    ]
    character.mechanics_json = mechanics
    character.modifiers_json = [_dump(value) for value in retained_modifiers]
    character.save(
        update_fields=["mechanics_json", "modifiers_json", "updated_at"]
    )
    restored_abilities = []
    for ability in character.abilities.select_for_update().select_related("template"):
        if ability.template.max_uses is not None:
            if ability.remaining_uses != ability.template.max_uses:
                restored_abilities.append(ability.id)
            ability.remaining_uses = ability.template.max_uses
            ability.save(update_fields=["remaining_uses", "updated_at"])
    event = _create_world_event(
        tool_call,
        "character_rested",
        [_entity_ref(RuntimeEntityType.CHARACTER.value, character.id)],
        {
            "summary": f"{character} rested and recovered.",
            "previous_hp": previous_hp,
            "current_hp": maximum_hp,
            "restored_ability_ids": restored_abilities,
        },
        {"reason": arguments.reason},
    )
    return ToolResult(
        success=True,
        message=f"{character} restored HP and ability uses.",
        affected_entities=[
            RuntimeEntityReference(
                type=RuntimeEntityType.CHARACTER,
                id=character.id,
            )
        ],
        world_event_id=event.id,
        data={
            "previous_hp": previous_hp,
            "current_hp": maximum_hp,
            "restored_ability_ids": restored_abilities,
        },
    )


def _item_for_update(item_id, campaign):
    try:
        return ItemInstance.objects.select_for_update().get(
            pk=item_id,
            dnd_session=campaign,
        )
    except ItemInstance.DoesNotExist as exc:
        raise GameToolError("The item does not belong to this campaign.") from exc


def _handle_transfer_item(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    item = _item_for_update(arguments.item_instance_id, campaign)
    if item.status != ItemInstance.Status.ACTIVE:
        raise GameToolError("Only an active item can be transferred.")
    if item.template and not item.template.mechanics_json.get("transferable", True):
        raise GameToolError("That item cannot be transferred.")
    owner = _item_owner(arguments.new_owner, campaign)
    previous_owner = (
        {"type": item.owner_type, "id": item.owner_id}
        if item.owner_type and item.owner_id
        else None
    )
    item.owner_type = _OWNER_TYPES[arguments.new_owner.type]
    item.owner_id = owner.id
    item.current_location = None
    item.save(
        update_fields=["owner_type", "owner_id", "current_location", "updated_at"]
    )
    event = _create_world_event(
        tool_call,
        "item_transferred",
        [_entity_ref(RuntimeEntityType.ITEM.value, item.id), _dump(arguments.new_owner)],
        {
            "summary": f"{item} was transferred to {owner}.",
            "previous_owner": previous_owner,
            "new_owner": _dump(arguments.new_owner),
        },
        {"reason": arguments.reason},
    )
    return ToolResult(
        success=True,
        message=f"Transferred {item} to {owner}.",
        affected_entities=[
            RuntimeEntityReference(type=RuntimeEntityType.ITEM, id=item.id),
            arguments.new_owner,
        ],
        world_event_id=event.id,
    )


def _handle_move_item(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    item = _item_for_update(arguments.item_instance_id, campaign)
    if item.status != ItemInstance.Status.ACTIVE:
        raise GameToolError("Only an active item can be placed at a location.")
    try:
        location = LocationInstance.objects.select_for_update().get(
            pk=arguments.destination_location_id,
            dnd_session=campaign,
        )
    except LocationInstance.DoesNotExist as exc:
        raise GameToolError("The destination does not belong to this campaign.") from exc
    previous_owner = (
        {"type": item.owner_type, "id": item.owner_id}
        if item.owner_type and item.owner_id
        else None
    )
    previous_location_id = item.current_location_id
    item.owner_type = None
    item.owner_id = None
    item.current_location = location
    item.save(
        update_fields=["owner_type", "owner_id", "current_location", "updated_at"]
    )
    event = _create_world_event(
        tool_call,
        "item_moved",
        [
            _entity_ref(RuntimeEntityType.ITEM.value, item.id),
            _entity_ref(RuntimeEntityType.LOCATION.value, location.id),
        ],
        {
            "summary": f"{item} was placed at {location}.",
            "previous_owner": previous_owner,
            "previous_location_id": previous_location_id,
            "current_location_id": location.id,
        },
        {"reason": arguments.reason},
    )
    return ToolResult(
        success=True,
        message=f"Moved {item} to {location}.",
        affected_entities=[
            RuntimeEntityReference(type=RuntimeEntityType.ITEM, id=item.id),
            RuntimeEntityReference(type=RuntimeEntityType.LOCATION, id=location.id),
        ],
        world_event_id=event.id,
    )


def _handle_consume_item(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    item = _item_for_update(arguments.item_instance_id, campaign)
    if item.status != ItemInstance.Status.ACTIVE:
        raise GameToolError("Only an active item can be consumed.")
    if not item.template or not item.template.mechanics_json.get("consumable", False):
        raise GameToolError("That item is not consumable.")
    previous_owner = (
        {"type": item.owner_type, "id": item.owner_id}
        if item.owner_type and item.owner_id
        else None
    )
    previous_location_id = item.current_location_id
    item.status = ItemInstance.Status.CONSUMED
    item.owner_type = None
    item.owner_id = None
    item.current_location = None
    item.save(
        update_fields=[
            "status",
            "owner_type",
            "owner_id",
            "current_location",
            "updated_at",
        ]
    )
    event = _create_world_event(
        tool_call,
        "item_consumed",
        [_entity_ref(RuntimeEntityType.ITEM.value, item.id)],
        {
            "summary": f"{item} was consumed.",
            "previous_owner": previous_owner,
            "previous_location_id": previous_location_id,
        },
        {"reason": arguments.reason},
    )
    return ToolResult(
        success=True,
        message=f"Consumed {item}.",
        affected_entities=[
            RuntimeEntityReference(type=RuntimeEntityType.ITEM, id=item.id)
        ],
        world_event_id=event.id,
    )


def _merge_visibility_state(current, patch):
    for branch in (patch.public_info, patch.dm_only):
        if branch is not None and "relationships" in branch:
            raise GameToolError(
                "Relationship state must be changed with update_relationship."
            )
    state = deepcopy(current or {"public_info": {}, "dm_only": {}})
    state.setdefault("public_info", {})
    state.setdefault("dm_only", {})
    if patch.public_info is not None:
        state["public_info"].update(patch.public_info)
    if patch.dm_only is not None:
        state["dm_only"].update(patch.dm_only)
    return state


def _handle_update_item_state(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    item = _item_for_update(arguments.item_instance_id, campaign)
    item.state_json = _merge_visibility_state(item.state_json, arguments.state_patch)
    item.save(update_fields=["state_json", "updated_at"])
    public_patch = arguments.state_patch.public_info or {}
    dm_patch = arguments.state_patch.dm_only or {}
    event = _create_world_event(
        tool_call,
        "item_state_updated",
        [_entity_ref(RuntimeEntityType.ITEM.value, item.id)],
        (
            {"summary": f"{item} changed state.", "state_patch": public_patch}
            if public_patch
            else {}
        ),
        {"reason": arguments.reason, "state_patch": dm_patch},
    )
    return ToolResult(
        success=True,
        message=f"Updated {item}.",
        affected_entities=[
            RuntimeEntityReference(type=RuntimeEntityType.ITEM, id=item.id)
        ],
        world_event_id=event.id,
        data={"state_json": deepcopy(item.state_json)},
    )


def _state_entity_for_update(reference, campaign):
    lookups = {
        RuntimeEntityType.CHARACTER: (CharacterInstance, {"dnd_session": campaign}),
        RuntimeEntityType.NPC: (NPCInstance, {"dnd_session": campaign}),
        RuntimeEntityType.LOCATION: (LocationInstance, {"dnd_session": campaign}),
        RuntimeEntityType.QUEST: (QuestInstance, {"dnd_session": campaign}),
        RuntimeEntityType.ITEM: (ItemInstance, {"dnd_session": campaign}),
        RuntimeEntityType.ABILITY: (
            AbilityInstance,
            {"character__dnd_session": campaign},
        ),
    }
    if reference.type not in lookups:
        raise GameToolError("That entity type does not have mutable state.")
    model, ownership_lookup = lookups[reference.type]
    try:
        return model.objects.select_for_update().get(pk=reference.id, **ownership_lookup)
    except model.DoesNotExist as exc:
        raise GameToolError("The entity does not belong to this campaign.") from exc


def _handle_update_entity_state(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    entity = _state_entity_for_update(arguments.entity, campaign)
    entity.state_json = _merge_visibility_state(entity.state_json, arguments.state_patch)
    entity.save(update_fields=["state_json", "updated_at"])
    public_patch = arguments.state_patch.public_info or {}
    dm_patch = arguments.state_patch.dm_only or {}
    event = _create_world_event(
        tool_call,
        f"{arguments.entity.type.value}_state_updated",
        [_dump(arguments.entity)],
        (
            {"summary": f"{entity} changed state.", "state_patch": public_patch}
            if public_patch
            else {}
        ),
        {"reason": arguments.reason, "state_patch": dm_patch},
    )
    return ToolResult(
        success=True,
        message=f"Updated {entity}.",
        affected_entities=[arguments.entity],
        world_event_id=event.id,
        data={"state_json": deepcopy(entity.state_json)},
    )


def _handle_advance_quest(tool_call, arguments):
    campaign = _tool_campaign(tool_call)
    try:
        quest = QuestInstance.objects.select_for_update().get(
            pk=arguments.quest_id,
            dnd_session=campaign,
        )
    except QuestInstance.DoesNotExist as exc:
        raise GameToolError("The quest does not belong to this campaign.") from exc
    if quest.status == QuestInstance.Status.FINISHED:
        raise GameToolError("The quest is already finished.")

    previous_status = quest.status
    if arguments.state is not None:
        requested_status = arguments.state.value
        if _NEXT_QUEST_STATUS.get(quest.status) != requested_status:
            raise GameToolError(
                "Quest status must advance exactly from hidden to available or "
                "from available to active."
            )
        quest.status = requested_status

    condition = None
    quest_finished = False
    campaign_completed = False
    if arguments.step_completed is not None:
        if quest.status != QuestInstance.Status.ACTIVE:
            raise GameToolError("A quest must be active before completing a step.")
        if quest.template_id is None:
            raise GameToolError("The quest does not have authored conditions.")
        condition_count = quest.template.conditions.count()
        instance_count = quest.conditions.count()
        finished_count = quest.conditions.filter(
            status=QuestConditionInstance.Status.FINISHED
        ).count()
        if (
            instance_count != condition_count
            or finished_count != quest.steps_completed
        ):
            raise GameToolError("The quest condition state is inconsistent.")
        if arguments.step_completed != quest.steps_completed:
            raise GameToolError(
                f"The next quest step is {quest.steps_completed}, not "
                f"{arguments.step_completed}."
            )
        try:
            condition = (
                QuestConditionInstance.objects.select_for_update()
                .select_related("template")
                .get(
                    quest_instance=quest,
                    template__quest_template_id=quest.template_id,
                    template__order=arguments.step_completed,
                )
            )
        except QuestConditionInstance.DoesNotExist as exc:
            raise GameToolError(
                f"Quest step {arguments.step_completed} is not configured."
            ) from exc
        if condition.status != QuestConditionInstance.Status.NOT_FINISHED:
            raise GameToolError("The next quest step is already finished.")

        condition.status = QuestConditionInstance.Status.FINISHED
        condition.finish_text = arguments.finish_text
        condition.save(update_fields=["status", "finish_text", "updated_at"])
        quest.steps_completed += 1
        if quest.steps_completed == condition_count:
            quest.status = QuestInstance.Status.FINISHED
            quest_finished = True
            campaign_completed = campaign.main_quest_id == quest.id

    quest.save(update_fields=["status", "steps_completed", "updated_at"])
    if campaign_completed:
        campaign.status = DndSession.Status.COMPLETED
        campaign.save(update_fields=["status", "updated_at"])

    if condition is not None:
        public_info = {
            "summary": arguments.finish_text,
            "quest": quest.title,
            "condition": condition.template.text,
            "step_completed": condition.template.order,
            "finish_text": arguments.finish_text,
            "previous_status": previous_status,
            "status": quest.status,
            "steps_completed": quest.steps_completed,
            "quest_finished": quest_finished,
            "campaign_completed": campaign_completed,
        }
        event_type = "quest_finished" if quest_finished else "quest_advanced"
        message = (
            f"Finished {quest}."
            if quest_finished
            else f"Completed step {condition.template.order} of {quest}."
        )
    else:
        public_info = {
            "summary": f"{quest} became {quest.status}.",
            "quest": quest.title,
            "previous_status": previous_status,
            "status": quest.status,
            "steps_completed": quest.steps_completed,
            "quest_finished": False,
            "campaign_completed": False,
        }
        event_type = "quest_status_changed"
        message = f"Advanced {quest} to {quest.status}."

    event = _create_world_event(
        tool_call,
        event_type,
        [_entity_ref(RuntimeEntityType.QUEST.value, quest.id)],
        public_info,
        importance=(5 if campaign_completed else 4 if quest_finished else 3),
    )
    return ToolResult(
        success=True,
        message=message,
        affected_entities=[
            RuntimeEntityReference(type=RuntimeEntityType.QUEST, id=quest.id)
        ],
        world_event_id=event.id,
        data={
            "status": quest.status,
            "steps_completed": quest.steps_completed,
            "step_completed": (
                condition.template.order if condition is not None else None
            ),
            "quest_finished": quest_finished,
            "campaign_completed": campaign_completed,
        },
    )


TOOL_REGISTRY = {
    "roll_dice": (RollDiceInput, _handle_roll_dice),
    "roll_check": (RollCheckInput, _handle_roll_check),
    "roll_save": (RollSaveInput, _handle_roll_save),
    "roll_contest": (RollContestInput, _handle_roll_contest),
    "add_roll_modifier": (AddRollModifierInput, _handle_add_roll_modifier),
    "remove_roll_modifier": (RemoveRollModifierInput, _handle_remove_roll_modifier),
    "update_relationship": (UpdateRelationshipInput, _handle_update_relationship),
    "move_character": (MoveCharacterInput, _handle_move_character),
    "move_npc": (MoveNPCInput, _handle_move_npc),
    "use_ability": (UseAbilityInput, _handle_use_ability),
    "rest_character": (RestCharacterInput, _handle_rest_character),
    "transfer_item": (TransferItemInput, _handle_transfer_item),
    "move_item": (MoveItemInput, _handle_move_item),
    "consume_item": (ConsumeItemInput, _handle_consume_item),
    "update_item_state": (UpdateItemStateInput, _handle_update_item_state),
    "update_entity_state": (EntityStateUpdate, _handle_update_entity_state),
    "advance_quest": (AdvanceQuestInput, _handle_advance_quest),
}


def available_game_tools():
    return tuple(TOOL_REGISTRY)


def execute_game_tool(*, agent_run, call_id, tool_name, arguments):
    if tool_name not in TOOL_REGISTRY:
        raise GameToolError(f"Unknown game tool {tool_name!r}.", "unknown_tool")
    input_schema, handler = TOOL_REGISTRY[tool_name]
    parsed_arguments = input_schema.model_validate(arguments)
    normalized_arguments = _dump(parsed_arguments)

    with transaction.atomic():
        locked_run = AgentRun.objects.select_for_update().select_related(
            "campaign_turn"
        ).get(pk=agent_run.pk)
        existing = ToolCallRecord.objects.filter(
            agent_run=locked_run,
            call_id=call_id,
        ).first()
        if existing is not None:
            if (
                existing.tool_name != tool_name
                or existing.arguments_json != normalized_arguments
            ):
                raise GameToolError(
                    "The tool call ID was already used with different input.",
                    "idempotency_conflict",
                )
            if existing.status == ToolCallRecord.Status.COMPLETED:
                return ToolResult.model_validate(existing.result_json)
            if existing.status == ToolCallRecord.Status.FAILED:
                error = existing.error_json or {}
                raise GameToolError(
                    error.get("message", "The prior tool call failed."),
                    error.get("code", "prior_tool_failure"),
                )
            raise GameToolError("The tool call is already running.", "tool_in_progress")

        sequence_number = (
            locked_run.tool_calls.aggregate(value=Max("sequence_number"))["value"]
            or 0
        ) + 1
        tool_call = ToolCallRecord.objects.create(
            agent_run=locked_run,
            call_id=call_id,
            sequence_number=sequence_number,
            tool_name=tool_name,
            arguments_json=normalized_arguments,
            status=ToolCallRecord.Status.RUNNING,
        )

    started = time.monotonic()
    try:
        with transaction.atomic():
            tool_call = ToolCallRecord.objects.select_for_update().select_related(
                "agent_run__campaign_turn"
            ).get(pk=tool_call.pk)
            result = handler(tool_call, parsed_arguments)
            tool_call.result_json = _dump(result)
            tool_call.status = ToolCallRecord.Status.COMPLETED
            tool_call.completed_at = timezone.now()
            tool_call.latency_ms = max(0, int((time.monotonic() - started) * 1000))
            tool_call.save(
                update_fields=[
                    "result_json",
                    "status",
                    "completed_at",
                    "latency_ms",
                ]
            )
            return result
    except Exception as exc:
        code = exc.code if isinstance(exc, GameToolError) else "tool_execution_failed"
        with transaction.atomic():
            ToolCallRecord.objects.filter(pk=tool_call.pk).update(
                status=ToolCallRecord.Status.FAILED,
                error_json={"code": code, "message": str(exc)},
                completed_at=timezone.now(),
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        raise
