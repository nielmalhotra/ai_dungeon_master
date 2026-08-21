from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


JsonObject = Dict[str, Any]


class OrmSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class VisibilityValue(str, Enum):
    PUBLIC_INFO = "public_info"
    DM_ONLY = "dm_only"


class VisibilityEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_info: JsonObject = Field(default_factory=dict)
    dm_only: JsonObject = Field(default_factory=dict)


class VisibilityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_info: Optional[JsonObject] = None
    dm_only: Optional[JsonObject] = None

    @model_validator(mode="after")
    def require_change(self):
        if not self.public_info and not self.dm_only:
            raise ValueError("A visibility patch must contain a public or DM-only change.")
        return self


class CampaignState(VisibilityEnvelope):
    pass


class CharacterNarrativeState(VisibilityEnvelope):
    pass


class NPCState(VisibilityEnvelope):
    pass


class LocationState(VisibilityEnvelope):
    pass


class QuestState(VisibilityEnvelope):
    pass


class WorldEventState(VisibilityEnvelope):
    pass


class TemplateDefinition(VisibilityEnvelope):
    pass


class RuntimeEntityType(str, Enum):
    CHARACTER = "character"
    NPC = "npc"
    LOCATION = "location"
    QUEST = "quest"
    WORLD_LORE = "world_lore"
    ABILITY = "ability"
    ITEM = "item"


class TemplateEntityType(str, Enum):
    NPC = "npc"
    LOCATION = "location"
    QUEST = "quest"
    WORLD_LORE = "world_lore"


class RuntimeEntityReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RuntimeEntityType
    id: int = Field(gt=0)


class TemplateEntityReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TemplateEntityType
    id: int = Field(gt=0)


class TemplateRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    target: TemplateEntityReference


class RuntimeRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    target: RuntimeEntityReference


class InitiallyKnownEntities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locations: List[UUID] = Field(default_factory=list)
    npcs: List[UUID] = Field(default_factory=list)
    quests: List[UUID] = Field(default_factory=list)
    world_lore: List[UUID] = Field(default_factory=list)


class DndSessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class CharacterTemplateRecord(OrmSchema):
    id: int = Field(gt=0)
    template_key: str
    character_template: JsonObject


class DndSessionRecord(OrmSchema):
    id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    status: DndSessionStatus
    scenario_key: str
    scenario_version: int = Field(ge=1)
    turn_number: int = Field(ge=0)
    opening_text: str
    initially_known_entities_json: InitiallyKnownEntities
    current_location_id: Optional[int] = Field(default=None, gt=0)
    main_quest_id: Optional[int] = Field(default=None, gt=0)
    state_json: CampaignState
    created_at: datetime
    updated_at: datetime


class CharacterInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    name: str
    template_json: JsonObject
    mechanics_json: JsonObject
    modifiers_json: List[JsonObject] = Field(default_factory=list)
    current_location_id: Optional[int] = Field(default=None, gt=0)
    state_json: CharacterNarrativeState
    relationships_json: List[RuntimeRelationship] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VersionedTemplateRecord(OrmSchema):
    id: int = Field(gt=0)
    definition_uuid: UUID
    scenario_key: str
    version: int = Field(ge=1)
    active: bool
    source_file: str
    created_at: datetime


class LocationTemplateRecord(VersionedTemplateRecord):
    name: str
    parent_template_id: Optional[int] = Field(default=None, gt=0)
    initial_status: "LocationStatus"
    definition_json: TemplateDefinition
    relationships_json: List[TemplateRelationship] = Field(default_factory=list)
    metadata_json: JsonObject
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None


class LocationStatus(str, Enum):
    HIDDEN = "hidden"
    ACTIVE = "active"
    DESTROYED = "destroyed"


class LocationInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: Optional[int] = Field(default=None, gt=0)
    name: str
    parent_location_id: Optional[int] = Field(default=None, gt=0)
    status: LocationStatus
    state_json: LocationState
    relationships_json: List[RuntimeRelationship] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class NPCTemplateRecord(VersionedTemplateRecord):
    name: str
    initial_location_template_id: Optional[int] = Field(default=None, gt=0)
    initial_status: "NPCStatus"
    definition_json: TemplateDefinition
    mechanics_json: JsonObject
    relationships_json: List[TemplateRelationship] = Field(default_factory=list)
    metadata_json: JsonObject
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None


class NPCStatus(str, Enum):
    HIDDEN = "hidden"
    ACTIVE = "active"
    DEAD = "dead"


class NPCInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: Optional[int] = Field(default=None, gt=0)
    name: str
    current_location_id: Optional[int] = Field(default=None, gt=0)
    status: NPCStatus
    mechanics_json: JsonObject
    modifiers_json: List[JsonObject] = Field(default_factory=list)
    state_json: NPCState
    relationships_json: List[RuntimeRelationship] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class QuestInitialStatus(str, Enum):
    HIDDEN = "hidden"
    AVAILABLE = "available"
    ACTIVE = "active"


class QuestTemplateRecord(VersionedTemplateRecord):
    title: str
    initial_status: QuestInitialStatus
    definition_json: TemplateDefinition
    relationships_json: List[TemplateRelationship] = Field(default_factory=list)
    metadata_json: JsonObject
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None


class QuestConditionTemplateRecord(OrmSchema):
    id: int = Field(gt=0)
    quest_template_id: int = Field(gt=0)
    order: int = Field(ge=0)
    text: str


class QuestStatus(str, Enum):
    HIDDEN = "hidden"
    AVAILABLE = "available"
    ACTIVE = "active"
    FINISHED = "finished"


class QuestInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: Optional[int] = Field(default=None, gt=0)
    title: str
    status: QuestStatus
    steps_completed: int = Field(ge=0)
    relationships_json: List[RuntimeRelationship] = Field(default_factory=list)
    state_json: QuestState
    created_at: datetime
    updated_at: datetime


class QuestConditionStatus(str, Enum):
    NOT_FINISHED = "not_finished"
    FINISHED = "finished"


class QuestConditionInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    quest_instance_id: int = Field(gt=0)
    template_id: int = Field(gt=0)
    status: QuestConditionStatus
    finish_text: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class WorldLoreChunkTemplateRecord(VersionedTemplateRecord):
    title: str
    section: str
    chunk_number: int = Field(ge=1)
    visibility: VisibilityValue
    content: str
    relationships_json: List[TemplateRelationship] = Field(default_factory=list)
    metadata_json: JsonObject
    embedding: List[float]


class WorldLoreRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: int = Field(gt=0)
    relationships_json: List[RuntimeRelationship] = Field(default_factory=list)
    created_at: datetime


class AbilityCategory(str, Enum):
    NON_COMBAT = "non_combat"
    COMBAT = "combat"


class AbilityTemplateRecord(OrmSchema):
    id: int = Field(gt=0)
    character_template_id: int = Field(gt=0)
    ability_key: str
    name: str
    active: bool
    category: AbilityCategory
    description: str
    resolution_json: JsonObject
    effect_json: JsonObject
    max_uses: Optional[int] = Field(default=None, ge=1)
    recharge: Optional[str] = None
    embedding: Optional[List[float]] = None
    created_at: datetime
    updated_at: datetime


class AbilityInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    character_id: int = Field(gt=0)
    template_id: int = Field(gt=0)
    remaining_uses: Optional[int] = Field(default=None, ge=0)
    state_json: VisibilityEnvelope
    created_at: datetime
    updated_at: datetime


class ItemStatus(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    DESTROYED = "destroyed"


class ItemOwnerType(str, Enum):
    CHARACTER = "character"
    NPC = "npc"


class ItemTemplateRecord(OrmSchema):
    id: int = Field(gt=0)
    template_key: str
    name: str
    active: bool
    definition_json: TemplateDefinition
    mechanics_json: JsonObject
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None
    created_at: datetime
    updated_at: datetime


class ItemInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: Optional[int] = Field(default=None, gt=0)
    name: str
    owner_type: Optional[ItemOwnerType] = None
    owner_id: Optional[int] = Field(default=None, gt=0)
    current_location_id: Optional[int] = Field(default=None, gt=0)
    status: ItemStatus
    state_json: VisibilityEnvelope
    created_at: datetime
    updated_at: datetime


class CampaignTurnStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DMTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narration: str
    suggested_actions: List[str] = Field(default_factory=list)
    context_references: List[RuntimeEntityReference] = Field(default_factory=list)


class CampaignTurnRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    turn_number: int = Field(ge=1)
    player_input: str
    dm_response_json: Optional[DMTurnResponse] = None
    status: CampaignTurnStatus
    created_at: datetime
    completed_at: Optional[datetime] = None


class AgentRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunRecord(OrmSchema):
    id: int = Field(gt=0)
    campaign_turn_id: int = Field(gt=0)
    run_number: int = Field(ge=1)
    status: AgentRunStatus
    model: str
    model_settings_json: JsonObject
    input_json: JsonObject
    output_json: Optional[JsonObject] = None
    trace_json: JsonObject
    provider_trace_id: Optional[str] = None
    retry_count: int = Field(ge=0)
    validation_failure_count: int = Field(ge=0)
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    estimated_cost_usd: Optional[Decimal] = Field(default=None, ge=0)
    error_json: Optional[JsonObject] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)


class RetrievedSourceType(str, Enum):
    WORLD_LORE = "world_lore"
    NPC = "npc"
    LOCATION = "location"
    QUEST = "quest"
    WORLD_EVENT = "world_event"
    ABILITY = "ability"
    ITEM = "item"


class RetrievedContextRecordSchema(OrmSchema):
    id: int = Field(gt=0)
    agent_run_id: int = Field(gt=0)
    query: str
    source_type: RetrievedSourceType
    source_id: int = Field(gt=0)
    visibility: VisibilityValue
    content_snapshot: str
    similarity_score: Optional[float] = None
    initial_rank: Optional[int] = Field(default=None, ge=1)
    rerank_score: Optional[float] = None
    final_rank: Optional[int] = Field(default=None, ge=1)
    included_in_prompt: bool
    exclusion_reason: Optional[str] = None
    prompt_position: Optional[int] = Field(default=None, ge=1)
    token_count: Optional[int] = Field(default=None, ge=0)
    created_at: datetime


class ToolCallStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolCallRecordSchema(OrmSchema):
    id: int = Field(gt=0)
    agent_run_id: int = Field(gt=0)
    call_id: str
    sequence_number: int = Field(ge=1)
    tool_name: str
    arguments_json: JsonObject
    result_json: Optional[JsonObject] = None
    status: ToolCallStatus
    error_json: Optional[JsonObject] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    latency_ms: Optional[int] = Field(default=None, ge=0)


class WorldEventRecord(OrmSchema):
    id: int = Field(gt=0)
    campaign_turn_id: int = Field(gt=0)
    tool_call_id: Optional[int] = Field(default=None, gt=0)
    sequence_number: int = Field(ge=1)
    event_type: str
    related_entities_json: List[RuntimeEntityReference] = Field(default_factory=list)
    state_json: WorldEventState
    importance: int = Field(ge=1, le=5)
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None
    created_at: datetime


class RollDiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dice: str = Field(pattern=r"^[1-9]\d*d[1-9]\d*(?:[+-]\d+)?$")
    reason: str


class AttributeName(str, Enum):
    STRENGTH = "strength"
    DEXTERITY = "dexterity"
    CONSTITUTION = "constitution"
    INTELLIGENCE = "intelligence"
    WISDOM = "wisdom"
    CHARISMA = "charisma"


class SkillName(str, Enum):
    ATHLETICS = "athletics"
    STEALTH = "stealth"
    SLEIGHT_OF_HAND = "sleight_of_hand"
    KNOWLEDGE = "knowledge"
    INVESTIGATION = "investigation"
    PERCEPTION = "perception"
    SURVIVAL = "survival"
    INSIGHT = "insight"
    PERSUASION = "persuasion"
    DECEPTION = "deception"
    INTIMIDATION = "intimidation"
    PERFORMANCE = "performance"


class RollType(str, Enum):
    ABILITY_CHECK = "ability_check"
    SAVING_THROW = "saving_throw"
    CONTEST = "contest"


class RollModifierKind(str, Enum):
    ADVANTAGE = "advantage"
    DISADVANTAGE = "disadvantage"
    FLAT_BONUS = "flat_bonus"
    FLAT_PENALTY = "flat_penalty"


class RollModifierDuration(str, Enum):
    CURRENT_ROLL = "current_roll"
    NEXT_APPLICABLE_ROLL = "next_applicable_roll"
    CURRENT_TURN = "current_turn"
    UNTIL_REST = "until_rest"
    PERMANENT = "permanent"


class ModifierApplicability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roll_types: List[RollType] = Field(default_factory=list)
    attributes: List[AttributeName] = Field(default_factory=list)
    skills: List[SkillName] = Field(default_factory=list)


class RollModifierEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: RollModifierKind
    value: int = Field(default=1, ge=1, le=20)
    source_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    reason: str
    applies_to: ModifierApplicability
    duration: RollModifierDuration
    created_turn: int = Field(ge=0)


class RollCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: RuntimeEntityReference
    attribute: AttributeName
    skill: Optional[SkillName] = None
    dc: Optional[int] = Field(default=None, ge=0, le=100)
    reason: str


class RollSaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: RuntimeEntityReference
    attribute: AttributeName
    dc: Optional[int] = Field(default=None, ge=0, le=100)
    reason: str


class RollContestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: RuntimeEntityReference
    actor_attribute: AttributeName
    actor_skill: Optional[SkillName] = None
    opponent: RuntimeEntityReference
    opponent_attribute: AttributeName
    opponent_skill: Optional[SkillName] = None
    reason: str


class AddRollModifierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: RuntimeEntityReference
    kind: RollModifierKind
    source_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    reason: str
    applies_to: ModifierApplicability
    duration: RollModifierDuration = RollModifierDuration.NEXT_APPLICABLE_ROLL


class RemoveRollModifierInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: RuntimeEntityReference
    modifier_id: UUID


class RelationshipSourceType(str, Enum):
    CHARACTER = "character"
    NPC = "npc"


class RelationshipTargetType(str, Enum):
    CHARACTER = "character"
    NPC = "npc"
    LOCATION = "location"


class RelationshipSourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RelationshipSourceType
    id: int = Field(gt=0)


class RelationshipTargetReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RelationshipTargetType
    id: int = Field(gt=0)


class UpdateRelationshipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_to_update: RelationshipSourceReference
    target: RelationshipTargetReference
    public_info_json: JsonObject
    dm_only_json: JsonObject


class MoveCharacterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: int = Field(gt=0)
    destination_location_id: int = Field(gt=0)
    reason: str


class MoveNPCInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    npc_id: int = Field(gt=0)
    destination_location_id: int = Field(gt=0)
    reason: str


class UseAbilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ability_instance_id: int = Field(gt=0)
    target: Optional[RuntimeEntityReference] = None
    reason: str


class RestCharacterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: int = Field(gt=0)
    reason: str


class TransferItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_instance_id: int = Field(gt=0)
    new_owner: RuntimeEntityReference
    reason: str


class MoveItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_instance_id: int = Field(gt=0)
    destination_location_id: int = Field(gt=0)
    reason: str


class ConsumeItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_instance_id: int = Field(gt=0)
    reason: str


class UpdateItemStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_instance_id: int = Field(gt=0)
    state_patch: VisibilityPatch
    reason: str


class EntityStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: RuntimeEntityReference
    state_patch: VisibilityPatch
    reason: str


class QuestAdvanceState(str, Enum):
    HIDDEN = "hidden"
    AVAILABLE = "available"
    ACTIVE = "active"


class AdvanceQuestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quest_id: int = Field(gt=0)
    state: Optional[QuestAdvanceState] = None
    step_completed: Optional[int] = Field(default=None, ge=0)
    finish_text: Optional[str] = None

    @model_validator(mode="after")
    def require_state_or_step(self):
        if self.state is None and self.step_completed is None:
            raise ValueError("Quest advancement requires a state or completed step.")
        if self.step_completed is None:
            if self.finish_text is not None:
                raise ValueError("finish_text requires a completed step.")
        elif self.finish_text is None or not self.finish_text.strip():
            raise ValueError("A completed step requires non-empty finish_text.")
        else:
            self.finish_text = self.finish_text.strip()
        return self


class CreateNPCInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    current_location_id: Optional[int] = Field(default=None, gt=0)
    state_json: NPCState
    relationships: List[RuntimeRelationship] = Field(default_factory=list)


class CreateLocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    parent_location_id: Optional[int] = Field(default=None, gt=0)
    state_json: LocationState
    relationships: List[RuntimeRelationship] = Field(default_factory=list)


class CreateQuestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    status: QuestStatus = QuestStatus.HIDDEN
    relationships: List[RuntimeRelationship] = Field(default_factory=list)
    state_json: QuestState


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    message: str
    affected_entities: List[RuntimeEntityReference] = Field(default_factory=list)
    world_event_id: Optional[int] = Field(default=None, gt=0)
    data: JsonObject = Field(default_factory=dict)
