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


class CharacterTemplateRecord(OrmSchema):
    id: int = Field(gt=0)
    template_key: str
    character_template: JsonObject


class DndSessionRecord(OrmSchema):
    id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    active: bool
    scenario_key: str
    scenario_version: int = Field(ge=1)
    turn_number: int = Field(ge=0)
    current_location_id: Optional[int] = Field(default=None, gt=0)
    state_json: CampaignState
    created_at: datetime
    updated_at: datetime


class CharacterInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    name: str
    template_json: JsonObject
    mechanics_json: JsonObject
    current_location_id: Optional[int] = Field(default=None, gt=0)
    state_json: CharacterNarrativeState
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
    is_starting_location: bool
    initially_known: bool
    definition_json: TemplateDefinition
    metadata_json: JsonObject
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None


class LocationStatus(str, Enum):
    ACTIVE = "active"
    INACCESSIBLE = "inaccessible"
    DESTROYED = "destroyed"
    UNKNOWN = "unknown"


class LocationInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: Optional[int] = Field(default=None, gt=0)
    name: str
    parent_location_id: Optional[int] = Field(default=None, gt=0)
    status: LocationStatus
    state_json: LocationState
    created_at: datetime
    updated_at: datetime


class NPCTemplateRecord(VersionedTemplateRecord):
    name: str
    initial_location_template_id: Optional[int] = Field(default=None, gt=0)
    initially_known: bool
    definition_json: TemplateDefinition
    metadata_json: JsonObject
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None


class NPCStatus(str, Enum):
    ACTIVE = "active"
    MISSING = "missing"
    DEAD = "dead"
    DEPARTED = "departed"


class NPCInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: Optional[int] = Field(default=None, gt=0)
    name: str
    current_location_id: Optional[int] = Field(default=None, gt=0)
    status: NPCStatus
    state_json: NPCState
    created_at: datetime
    updated_at: datetime


class QuestInitialStatus(str, Enum):
    HIDDEN = "hidden"
    AVAILABLE = "available"
    ACTIVE = "active"


class QuestTemplateRecord(VersionedTemplateRecord):
    title: str
    initial_status: QuestInitialStatus
    initially_known: bool
    definition_json: TemplateDefinition
    related_templates_json: List[TemplateEntityReference] = Field(default_factory=list)
    metadata_json: JsonObject
    public_embedding: Optional[List[float]] = None
    dm_embedding: Optional[List[float]] = None


class QuestStatus(str, Enum):
    HIDDEN = "hidden"
    AVAILABLE = "available"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class QuestInstanceRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: Optional[int] = Field(default=None, gt=0)
    title: str
    status: QuestStatus
    current_stage: int = Field(ge=0)
    related_entities_json: List[RuntimeEntityReference] = Field(default_factory=list)
    state_json: QuestState
    created_at: datetime
    updated_at: datetime


class WorldLoreChunkTemplateRecord(VersionedTemplateRecord):
    title: str
    section: str
    chunk_number: int = Field(ge=1)
    visibility: VisibilityValue
    initially_known: bool
    content: str
    metadata_json: JsonObject
    embedding: List[float]

    @model_validator(mode="after")
    def known_lore_must_be_public(self):
        if self.initially_known and self.visibility != VisibilityValue.PUBLIC_INFO:
            raise ValueError("Initially known lore must have public_info visibility.")
        return self


class WorldLoreRecord(OrmSchema):
    id: int = Field(gt=0)
    dnd_session_id: int = Field(gt=0)
    template_id: int = Field(gt=0)
    created_at: datetime


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


class EntityStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: RuntimeEntityReference
    state_json: VisibilityEnvelope


class CreateNPCInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    current_location_id: Optional[int] = Field(default=None, gt=0)
    state_json: NPCState


class CreateLocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    parent_location_id: Optional[int] = Field(default=None, gt=0)
    state_json: LocationState


class CreateQuestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    status: QuestStatus = QuestStatus.HIDDEN
    related_entities: List[RuntimeEntityReference] = Field(default_factory=list)
    state_json: QuestState


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    success: bool
    message: str
    affected_entities: List[RuntimeEntityReference] = Field(default_factory=list)
    world_event_id: Optional[int] = Field(default=None, gt=0)
