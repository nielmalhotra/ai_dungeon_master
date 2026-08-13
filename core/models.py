from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from pgvector.django import VectorField

from accounts.models import User


def empty_visibility_state():
    return {"public_info": {}, "dm_only": {}}


class Visibility(models.TextChoices):
    PUBLIC_INFO = "public_info", "Public info"
    DM_ONLY = "dm_only", "DM only"


class CharacterTemplate(models.Model):
    template_key = models.CharField(max_length=32, unique=True)
    character_template = models.JSONField()

    class Meta:
        db_table = "character_templates"
        ordering = ["template_key"]

    def __str__(self):
        return self.character_template.get("class", self.template_key)


class DndSession(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="dnd_sessions")
    active = models.BooleanField(default=True)
    scenario_key = models.CharField(max_length=64, default=settings.SCENARIO_KEY)
    scenario_version = models.PositiveIntegerField(default=1)
    turn_number = models.PositiveBigIntegerField(default=0)
    current_location = models.ForeignKey(
        "LocationInstance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="focused_sessions",
    )
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dnd_session"
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(active=True),
                name="one_active_dnd_session_per_user",
            )
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "scenario_version"],
                name="session_scenario_idx",
            )
        ]

    def __str__(self):
        return f"{self.user.email} ({'active' if self.active else 'inactive'})"


class CharacterInstance(models.Model):
    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="characters",
    )
    name = models.CharField(max_length=80)
    template_json = models.JSONField()
    mechanics_json = models.JSONField(default=dict)
    current_location = models.ForeignKey(
        "LocationInstance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="characters",
    )
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "character_instance"
        ordering = ["id"]

    def __str__(self):
        character_class = self.template_json.get("class", "Character")
        return f"{self.name} ({character_class})"


class LocationTemplate(models.Model):
    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    name = models.CharField(max_length=160)
    parent_template = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="child_templates",
    )
    is_starting_location = models.BooleanField(default=False)
    initially_known = models.BooleanField(default=False)
    definition_json = models.JSONField(default=empty_visibility_state)
    metadata_json = models.JSONField(default=dict)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "location_template"
        ordering = ["scenario_key", "version", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["scenario_key", "version", "definition_uuid"],
                name="uniq_location_tpl_uuid",
            ),
            models.UniqueConstraint(
                fields=["scenario_key", "version"],
                condition=Q(is_starting_location=True),
                name="uniq_start_location_tpl",
            ),
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active"],
                name="location_tpl_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.scenario_key} v{self.version})"


class LocationInstance(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACCESSIBLE = "inaccessible", "Inaccessible"
        DESTROYED = "destroyed", "Destroyed"
        UNKNOWN = "unknown", "Unknown"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="locations",
    )
    template = models.ForeignKey(
        LocationTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    name = models.CharField(max_length=160)
    parent_location = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="child_locations",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "location_instance"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_location_instance_tpl",
            )
        ]
        indexes = [
            models.Index(
                fields=["dnd_session", "status"],
                name="location_session_status_idx",
            )
        ]

    def __str__(self):
        return self.name


class NPCTemplate(models.Model):
    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    name = models.CharField(max_length=120)
    initial_location_template = models.ForeignKey(
        LocationTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="initial_npc_templates",
    )
    initially_known = models.BooleanField(default=False)
    definition_json = models.JSONField(default=empty_visibility_state)
    metadata_json = models.JSONField(default=dict)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "npc_template"
        ordering = ["scenario_key", "version", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["scenario_key", "version", "definition_uuid"],
                name="uniq_npc_tpl_uuid",
            )
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active"],
                name="npc_tpl_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.name} ({self.scenario_key} v{self.version})"


class NPCInstance(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MISSING = "missing", "Missing"
        DEAD = "dead", "Dead"
        DEPARTED = "departed", "Departed"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="npcs",
    )
    template = models.ForeignKey(
        NPCTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    name = models.CharField(max_length=120)
    current_location = models.ForeignKey(
        LocationInstance,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="npcs",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "npc_instance"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_npc_instance_tpl",
            )
        ]
        indexes = [
            models.Index(
                fields=["dnd_session", "current_location", "status"],
                name="npc_session_loc_status_idx",
            )
        ]

    def __str__(self):
        return self.name


class QuestTemplate(models.Model):
    class InitialStatus(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        AVAILABLE = "available", "Available"
        ACTIVE = "active", "Active"

    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    title = models.CharField(max_length=200)
    initial_status = models.CharField(
        max_length=16,
        choices=InitialStatus.choices,
        default=InitialStatus.HIDDEN,
    )
    initially_known = models.BooleanField(default=False)
    definition_json = models.JSONField(default=empty_visibility_state)
    related_templates_json = models.JSONField(default=list)
    metadata_json = models.JSONField(default=dict)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quest_template"
        ordering = ["scenario_key", "version", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["scenario_key", "version", "definition_uuid"],
                name="uniq_quest_tpl_uuid",
            )
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active"],
                name="quest_tpl_active_idx",
            )
        ]

    def __str__(self):
        return f"{self.title} ({self.scenario_key} v{self.version})"


class QuestInstance(models.Model):
    class Status(models.TextChoices):
        HIDDEN = "hidden", "Hidden"
        AVAILABLE = "available", "Available"
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="quests",
    )
    template = models.ForeignKey(
        QuestTemplate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="instances",
    )
    title = models.CharField(max_length=200)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.HIDDEN,
    )
    current_stage = models.PositiveIntegerField(default=0)
    related_entities_json = models.JSONField(default=list)
    state_json = models.JSONField(default=empty_visibility_state)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "quest_instance"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_quest_instance_tpl",
            )
        ]
        indexes = [
            models.Index(
                fields=["dnd_session", "status"],
                name="quest_session_status_idx",
            )
        ]

    def __str__(self):
        return self.title


class WorldLoreChunkTemplate(models.Model):
    definition_uuid = models.UUIDField()
    scenario_key = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    source_file = models.CharField(max_length=255)
    title = models.CharField(max_length=200)
    section = models.CharField(max_length=200)
    chunk_number = models.PositiveIntegerField()
    visibility = models.CharField(max_length=16, choices=Visibility.choices)
    initially_known = models.BooleanField(default=False)
    content = models.TextField()
    metadata_json = models.JSONField(default=dict)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIMENSIONS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "world_lore_chunk_template"
        ordering = ["source_file", "chunk_number"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "scenario_key",
                    "version",
                    "definition_uuid",
                    "chunk_number",
                ],
                name="uniq_lore_tpl_chunk",
            ),
            models.CheckConstraint(
                check=Q(initially_known=False) | Q(visibility=Visibility.PUBLIC_INFO),
                name="lore_known_public_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["scenario_key", "active", "visibility"],
                name="lore_tpl_active_vis_idx",
            )
        ]

    def __str__(self):
        return f"{self.scenario_key} v{self.version}: {self.section}"


class WorldLore(models.Model):
    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="world_lore",
    )
    template = models.ForeignKey(
        WorldLoreChunkTemplate,
        on_delete=models.PROTECT,
        related_name="campaign_lore",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "world_lore"
        ordering = ["template__source_file", "template__chunk_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "template"],
                name="uniq_session_lore_tpl",
            )
        ]

    def __str__(self):
        return f"{self.template.source_file}: {self.template.section}"


class CampaignTurn(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    dnd_session = models.ForeignKey(
        DndSession,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    turn_number = models.PositiveBigIntegerField()
    player_input = models.TextField()
    dm_response_json = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "campaign_turn"
        ordering = ["dnd_session", "turn_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["dnd_session", "turn_number"],
                name="uniq_campaign_turn_number",
            )
        ]


class AgentRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    campaign_turn = models.ForeignKey(
        CampaignTurn,
        on_delete=models.CASCADE,
        related_name="agent_runs",
    )
    run_number = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    model = models.CharField(max_length=100)
    model_settings_json = models.JSONField(default=dict)
    input_json = models.JSONField(default=dict)
    output_json = models.JSONField(null=True, blank=True)
    trace_json = models.JSONField(default=dict)
    provider_trace_id = models.CharField(max_length=255, null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    validation_failure_count = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    estimated_cost_usd = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        null=True,
        blank=True,
    )
    error_json = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "agent_run"
        ordering = ["campaign_turn", "run_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign_turn", "run_number"],
                name="uniq_turn_agent_run",
            )
        ]


class RetrievedContextRecord(models.Model):
    class SourceType(models.TextChoices):
        WORLD_LORE = "world_lore", "World lore"
        NPC = "npc", "NPC"
        LOCATION = "location", "Location"
        QUEST = "quest", "Quest"
        WORLD_EVENT = "world_event", "World event"

    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.CASCADE,
        related_name="retrieved_context",
    )
    query = models.TextField()
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    source_id = models.BigIntegerField()
    visibility = models.CharField(max_length=16, choices=Visibility.choices)
    content_snapshot = models.TextField()
    similarity_score = models.FloatField(null=True, blank=True)
    initial_rank = models.PositiveIntegerField(null=True, blank=True)
    rerank_score = models.FloatField(null=True, blank=True)
    final_rank = models.PositiveIntegerField(null=True, blank=True)
    included_in_prompt = models.BooleanField(default=False)
    exclusion_reason = models.CharField(max_length=100, null=True, blank=True)
    prompt_position = models.PositiveIntegerField(null=True, blank=True)
    token_count = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "retrieved_context_record"
        ordering = ["agent_run", "initial_rank", "id"]
        constraints = [
            models.CheckConstraint(
                check=Q(source_id__gt=0),
                name="retrieved_source_id_ck",
            )
        ]
        indexes = [
            models.Index(
                fields=["agent_run", "included_in_prompt"],
                name="retrieved_run_included_idx",
            ),
            models.Index(
                fields=["source_type", "source_id"],
                name="retrieved_source_idx",
            ),
        ]


class ToolCallRecord(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    agent_run = models.ForeignKey(
        AgentRun,
        on_delete=models.CASCADE,
        related_name="tool_calls",
    )
    call_id = models.CharField(max_length=255)
    sequence_number = models.PositiveIntegerField()
    tool_name = models.CharField(max_length=100)
    arguments_json = models.JSONField(default=dict)
    result_json = models.JSONField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    error_json = models.JSONField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        db_table = "tool_call_record"
        ordering = ["agent_run", "sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent_run", "call_id"],
                name="uniq_run_tool_call_id",
            ),
            models.UniqueConstraint(
                fields=["agent_run", "sequence_number"],
                name="uniq_run_tool_sequence",
            ),
        ]


class WorldEvent(models.Model):
    campaign_turn = models.ForeignKey(
        CampaignTurn,
        on_delete=models.CASCADE,
        related_name="world_events",
    )
    tool_call = models.ForeignKey(
        ToolCallRecord,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="world_events",
    )
    sequence_number = models.PositiveIntegerField()
    event_type = models.CharField(max_length=64)
    related_entities_json = models.JSONField(default=list)
    state_json = models.JSONField(default=empty_visibility_state)
    importance = models.PositiveSmallIntegerField(default=3)
    public_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    dm_embedding = VectorField(
        dimensions=settings.EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "world_event"
        ordering = ["campaign_turn", "sequence_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign_turn", "sequence_number"],
                name="uniq_turn_event_sequence",
            ),
            models.CheckConstraint(
                check=Q(importance__gte=1) & Q(importance__lte=5),
                name="world_event_importance_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=["event_type", "created_at"],
                name="world_event_type_time_idx",
            )
        ]
