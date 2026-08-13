# Combatless Campaign Schema

This document is the schema contract for the first combatless AI Dungeon Master
prototype. It describes the intended database model before Django models and
migrations are added.

## Design rules

- `DndSession` is the long-running campaign. We keep its existing name.
- Database primary keys are auto-incrementing integers.
- A scenario source file has one stable `definition_uuid`. That UUID remains the
  same when the same definition appears in a later scenario version.
- Scenario source files reference other source definitions by `definition_uuid`.
  The synchronizer resolves those references to integer template IDs.
- Runtime tools, state, traces, and events reference integer instance IDs. A
  polymorphic JSON reference always includes both type and ID.
- Templates are immutable and retained. Instances point to their exact template
  version with `on_delete=PROTECT`.
- A null template foreign key means an NPC, location, or quest was generated at
  runtime. No separate `origin` field is needed.
- Current state lives on the campaign or entity instance. Historical changes live
  in `WorldEvent`.
- Only validated application tools may mutate state or create events.
- Combat, encounters, combatants, play sessions, and per-character knowledge are
  outside this prototype.

## Shared JSON contracts

### Visibility envelope

Every `state_json` uses this outer shape:

```json
{
  "public_info": {
    "summary": "Facts and description currently known to the party."
  },
  "dm_only": {
    "summary": "Hidden facts, intentions, and description available to the DM."
  }
}
```

Both values must be JSON objects. Their internal fields may vary by entity type,
but they are validated by type-specific Pydantic models. In mutable instance and
event state, `public_info` means party knowledge. Player-facing retrieval and
responses must never receive `dm_only`.

Template `definition_json` uses the same two branches for authored content. Its
`public_info` branch means player-safe information that may be revealed. It does
not become party knowledge unless the template is marked `initially_known` or a
later event reveals it.

### Runtime entity reference

```json
{
  "type": "npc",
  "id": 15
}
```

Allowed runtime types initially are `character`, `npc`, `location`, `quest`, and
`world_lore`. Names and filenames are display information, not identifiers.

## Scenario version selection

Scenario releases live at `scenario/<scenario_key>/v<integer>/`. The synchronizer
selects the greatest numeric version directory, so `v10` is newer than `v2`.
There is no manifest file.

The synchronizer must validate all four required folders before writing rows:

- `npcs/`
- `locations/`
- `quests/`
- `worldlore/`

Every `.txt` file must contain a valid definition UUID. A UUID may appear only
once within a version across all four folders. Reusing a UUID in a later version
is allowed only for the same scenario and definition type.

## Existing and extended tables

### `CharacterTemplate`

Existing reusable player-character definition.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `template_key` | `CharField(32)` | Unique legacy source identifier; never used by runtime tools |
| `character_template` | `JSONField` | Validated character definition |

### `DndSession`

The long-running campaign and top-level owner of mutable game state.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `user_id` | Foreign key to `User` | Required, `CASCADE` |
| `active` | `BooleanField` | Defaults to `true` |
| `scenario_key` | `CharField(64)` | Required, for example `whitesparrow` |
| `scenario_version` | `PositiveIntegerField` | Exact imported version used by this campaign |
| `turn_number` | `PositiveBigIntegerField` | Defaults to `0`; incremented transactionally when a turn starts |
| `current_location_id` | Foreign key to `LocationInstance` | Nullable, `SET_NULL`; current scene focus |
| `state_json` | `JSONField` | Required visibility envelope containing the short current campaign situation |
| `created_at` | `DateTimeField` | `auto_now_add` |
| `updated_at` | `DateTimeField` | `auto_now` |

Constraints and indexes:

- At most one active `DndSession` per user.
- Index `(scenario_key, scenario_version)`.

### `CharacterInstance`

One player character inside one campaign.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `dnd_session_id` | Foreign key to `DndSession` | Required, `CASCADE` |
| `name` | `CharField(80)` | Required display name |
| `template_json` | `JSONField` | Existing frozen starting character definition |
| `mechanics_json` | `JSONField` | Current formulaic state such as HP, gear quantities, conditions, and ability uses |
| `current_location_id` | Foreign key to `LocationInstance` | Nullable, `SET_NULL` |
| `state_json` | `JSONField` | Required visibility envelope for subjective current description |
| `created_at` | `DateTimeField` | `auto_now_add` |
| `updated_at` | `DateTimeField` | `auto_now` |

`mechanics_json` is validated deterministically and is not freeform LLM memory.
Its initial value is derived from `template_json` when the campaign is created.

### `WorldLoreChunkTemplate`

One embedded chunk from a versioned world-lore source file.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `definition_uuid` | `UUIDField` | Stable UUID of the source document |
| `scenario_key` | `CharField(64)` | Required |
| `version` | `PositiveIntegerField` | Derived from the `vN` directory |
| `active` | `BooleanField` | True only for the currently synchronized version |
| `source_file` | `CharField(255)` | Path relative to the version directory |
| `title` | `CharField(200)` | Document title |
| `section` | `CharField(200)` | Chunk section heading |
| `chunk_number` | `PositiveIntegerField` | Stable order within the source document and version |
| `visibility` | `CharField(16)` | `public_info` or `dm_only` |
| `initially_known` | `BooleanField` | Whether a public chunk is party knowledge at campaign creation |
| `content` | `TextField` | Text embedded for this chunk |
| `metadata_json` | `JSONField` | Source and resolved related-template metadata |
| `embedding` | `VectorField` | Required vector |
| `created_at` | `DateTimeField` | `auto_now_add` |

Constraints and indexes:

- Unique `(scenario_key, version, definition_uuid, chunk_number)`.
- Index `(scenario_key, active, visibility)`.
- `initially_known` must be false when `visibility = dm_only`.

### `WorldLore`

Campaign association with one exact immutable lore-template chunk. Content and
embeddings remain on the protected template row and are not copied.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key and runtime lore ID |
| `dnd_session_id` | Foreign key to `DndSession` | Required, `CASCADE` |
| `template_id` | Foreign key to `WorldLoreChunkTemplate` | Required, `PROTECT` |
| `created_at` | `DateTimeField` | `auto_now_add` |

Constraint: unique `(dnd_session_id, template_id)`.

## Entity templates and instances

### `NPCTemplate`

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `definition_uuid` | `UUIDField` | Stable source definition UUID |
| `scenario_key` | `CharField(64)` | Required |
| `version` | `PositiveIntegerField` | Derived from directory |
| `active` | `BooleanField` | Current synchronized version |
| `source_file` | `CharField(255)` | Relative source path |
| `name` | `CharField(120)` | Authored display name |
| `initial_location_template_id` | Foreign key to `LocationTemplate` | Nullable, `PROTECT` |
| `initially_known` | `BooleanField` | Whether authored public information begins as party knowledge |
| `definition_json` | `JSONField` | Authored `public_info` and `dm_only` content |
| `metadata_json` | `JSONField` | Additional validated source metadata |
| `public_embedding` | `VectorField` | Nullable when public content is empty |
| `dm_embedding` | `VectorField` | Nullable when hidden content is empty |
| `created_at` | `DateTimeField` | `auto_now_add` |

Constraint: unique `(scenario_key, version, definition_uuid)`.

### `NPCInstance`

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key and runtime NPC ID |
| `dnd_session_id` | Foreign key to `DndSession` | Required, `CASCADE` |
| `template_id` | Foreign key to `NPCTemplate` | Nullable, `PROTECT`; null means generated |
| `name` | `CharField(120)` | Required mutable display name |
| `current_location_id` | Foreign key to `LocationInstance` | Nullable, `SET_NULL` |
| `status` | `CharField(16)` | `active`, `missing`, `dead`, or `departed` |
| `state_json` | `JSONField` | Required current visibility envelope |
| `created_at` | `DateTimeField` | `auto_now_add` |
| `updated_at` | `DateTimeField` | `auto_now` |

Constraints and indexes:

- A campaign may contain at most one instance of a non-null template.
- Index `(dnd_session_id, current_location_id, status)`.

### `LocationTemplate`

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `definition_uuid` | `UUIDField` | Stable source definition UUID |
| `scenario_key` | `CharField(64)` | Required |
| `version` | `PositiveIntegerField` | Derived from directory |
| `active` | `BooleanField` | Current synchronized version |
| `source_file` | `CharField(255)` | Relative source path |
| `name` | `CharField(160)` | Authored display name |
| `parent_template_id` | Self foreign key | Nullable, `PROTECT` |
| `is_starting_location` | `BooleanField` | Exactly one per scenario version |
| `initially_known` | `BooleanField` | Whether authored public information begins as party knowledge |
| `definition_json` | `JSONField` | Authored `public_info` and `dm_only` content |
| `metadata_json` | `JSONField` | Additional validated source metadata |
| `public_embedding` | `VectorField` | Nullable when public content is empty |
| `dm_embedding` | `VectorField` | Nullable when hidden content is empty |
| `created_at` | `DateTimeField` | `auto_now_add` |

Constraint: unique `(scenario_key, version, definition_uuid)`.

### `LocationInstance`

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key and runtime location ID |
| `dnd_session_id` | Foreign key to `DndSession` | Required, `CASCADE` |
| `template_id` | Foreign key to `LocationTemplate` | Nullable, `PROTECT`; null means generated |
| `name` | `CharField(160)` | Required mutable display name |
| `parent_location_id` | Self foreign key | Nullable, `SET_NULL` |
| `status` | `CharField(16)` | `active`, `inaccessible`, `destroyed`, or `unknown` |
| `state_json` | `JSONField` | Required current visibility envelope |
| `created_at` | `DateTimeField` | `auto_now_add` |
| `updated_at` | `DateTimeField` | `auto_now` |

Constraints and indexes:

- A campaign may contain at most one instance of a non-null template.
- Index `(dnd_session_id, status)`.

### `QuestTemplate`

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `definition_uuid` | `UUIDField` | Stable source definition UUID |
| `scenario_key` | `CharField(64)` | Required |
| `version` | `PositiveIntegerField` | Derived from directory |
| `active` | `BooleanField` | Current synchronized version |
| `source_file` | `CharField(255)` | Relative source path |
| `title` | `CharField(200)` | Authored display title |
| `initial_status` | `CharField(16)` | `hidden`, `available`, or `active` |
| `initially_known` | `BooleanField` | Whether authored public information begins as party knowledge |
| `definition_json` | `JSONField` | Authored `public_info` and `dm_only` content |
| `related_templates_json` | `JSONField` | Resolved typed integer template IDs |
| `metadata_json` | `JSONField` | Additional validated source metadata |
| `public_embedding` | `VectorField` | Nullable when public content is empty |
| `dm_embedding` | `VectorField` | Nullable when hidden content is empty |
| `created_at` | `DateTimeField` | `auto_now_add` |

Constraint: unique `(scenario_key, version, definition_uuid)`.

### `QuestInstance`

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key and runtime quest ID |
| `dnd_session_id` | Foreign key to `DndSession` | Required, `CASCADE` |
| `template_id` | Foreign key to `QuestTemplate` | Nullable, `PROTECT`; null means generated |
| `title` | `CharField(200)` | Required mutable display title |
| `status` | `CharField(16)` | `hidden`, `available`, `active`, `completed`, or `failed` |
| `current_stage` | `PositiveIntegerField` | Defaults to `0` |
| `related_entities_json` | `JSONField` | Typed runtime entity IDs |
| `state_json` | `JSONField` | Required current visibility envelope |
| `created_at` | `DateTimeField` | `auto_now_add` |
| `updated_at` | `DateTimeField` | `auto_now` |

Constraints and indexes:

- A campaign may contain at most one instance of a non-null template.
- Index `(dnd_session_id, status)`.

## Turns, state history, and tracing

### `CampaignTurn`

One player input and the complete DM workflow that answers it. Questions are
ordinary turns. A turn changes durable world state only when it has one or more
WorldEvents.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `dnd_session_id` | Foreign key to `DndSession` | Required, `CASCADE` |
| `turn_number` | `PositiveBigIntegerField` | Required immutable sequence number |
| `player_input` | `TextField` | Required |
| `dm_response_json` | `JSONField` | Nullable until complete; validated structured DM response |
| `status` | `CharField(16)` | `pending`, `running`, `completed`, or `failed` |
| `created_at` | `DateTimeField` | `auto_now_add` |
| `completed_at` | `DateTimeField` | Nullable |

Constraint: unique `(dnd_session_id, turn_number)`.

### `AgentRun`

One execution of the LangGraph DM graph for a turn. LangGraph and LangSmith do
not create this application row automatically; orchestration code creates and
updates it and may attach an external trace ID.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `campaign_turn_id` | Foreign key to `CampaignTurn` | Required, `CASCADE` |
| `run_number` | `PositiveIntegerField` | Starts at `1` for each turn |
| `status` | `CharField(16)` | `running`, `completed`, or `failed` |
| `model` | `CharField(100)` | Exact model used |
| `model_settings_json` | `JSONField` | Reasoning and output settings |
| `input_json` | `JSONField` | Normalized graph input and context budget |
| `output_json` | `JSONField` | Nullable validated graph output |
| `trace_json` | `JSONField` | Sanitized local graph/model-call trace |
| `provider_trace_id` | `CharField(255)` | Nullable LangSmith or provider trace ID |
| `retry_count` | `PositiveIntegerField` | Defaults to `0` |
| `validation_failure_count` | `PositiveIntegerField` | Defaults to `0` |
| `input_tokens` | `PositiveIntegerField` | Nullable |
| `output_tokens` | `PositiveIntegerField` | Nullable |
| `estimated_cost_usd` | `DecimalField(12, 6)` | Nullable |
| `error_json` | `JSONField` | Nullable structured failure |
| `started_at` | `DateTimeField` | Required |
| `completed_at` | `DateTimeField` | Nullable |
| `latency_ms` | `PositiveIntegerField` | Nullable |

Constraint: unique `(campaign_turn_id, run_number)`.

### `RetrievedContextRecord`

One candidate returned by retrieval. This record shows what was found, filtered,
reranked, and actually included in the prompt. It does not claim that the model
internally relied on the item.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `agent_run_id` | Foreign key to `AgentRun` | Required, `CASCADE` |
| `query` | `TextField` | Retrieval query used |
| `source_type` | `CharField(32)` | `world_lore`, `npc`, `location`, `quest`, or `world_event` |
| `source_id` | `BigIntegerField` | Runtime row ID; interpreted with `source_type` |
| `visibility` | `CharField(16)` | `public_info` or `dm_only` |
| `content_snapshot` | `TextField` | Exact retrieved text at run time |
| `similarity_score` | `FloatField` | Nullable raw retrieval score |
| `initial_rank` | `PositiveIntegerField` | Nullable |
| `rerank_score` | `FloatField` | Nullable |
| `final_rank` | `PositiveIntegerField` | Nullable |
| `included_in_prompt` | `BooleanField` | Required |
| `exclusion_reason` | `CharField(100)` | Nullable |
| `prompt_position` | `PositiveIntegerField` | Nullable |
| `token_count` | `PositiveIntegerField` | Nullable |
| `created_at` | `DateTimeField` | `auto_now_add` |

Indexes: `(agent_run_id, included_in_prompt)` and `(source_type, source_id)`.

### `ToolCallRecord`

One validated application tool invocation made during an agent run.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key |
| `agent_run_id` | Foreign key to `AgentRun` | Required, `CASCADE` |
| `call_id` | `CharField(255)` | Provider/graph call ID used for idempotency |
| `sequence_number` | `PositiveIntegerField` | Invocation order within the run |
| `tool_name` | `CharField(100)` | Required |
| `arguments_json` | `JSONField` | Validated arguments |
| `result_json` | `JSONField` | Nullable validated result |
| `status` | `CharField(16)` | `running`, `completed`, or `failed` |
| `error_json` | `JSONField` | Nullable structured failure |
| `started_at` | `DateTimeField` | Required |
| `completed_at` | `DateTimeField` | Nullable |
| `latency_ms` | `PositiveIntegerField` | Nullable |

Constraints:

- Unique `(agent_run_id, call_id)` for retry safety.
- Unique `(agent_run_id, sequence_number)`.

### `WorldEvent`

Append-only record of a durable campaign change or newly revealed fact. A recall
question that changes nothing creates no event.

| Field | Type | Rules |
| --- | --- | --- |
| `id` | `BigAutoField` | Primary key and runtime event ID |
| `campaign_turn_id` | Foreign key to `CampaignTurn` | Required, `CASCADE` |
| `tool_call_id` | Foreign key to `ToolCallRecord` | Nullable, `SET_NULL` |
| `sequence_number` | `PositiveIntegerField` | Event order within the turn |
| `event_type` | `CharField(64)` | Typed value such as `npc_created`, `npc_moved`, or `quest_advanced` |
| `related_entities_json` | `JSONField` | Validated list of typed runtime entity IDs |
| `state_json` | `JSONField` | Required visibility envelope describing what changed |
| `importance` | `PositiveSmallIntegerField` | `1` through `5`; controls memory retrieval priority |
| `public_embedding` | `VectorField` | Nullable when public content is empty or not worth embedding |
| `dm_embedding` | `VectorField` | Nullable when hidden content is empty or not worth embedding |
| `created_at` | `DateTimeField` | `auto_now_add` |

Constraints and indexes:

- Unique `(campaign_turn_id, sequence_number)`.
- Index `(event_type, created_at)`.
- The application verifies that every related entity belongs to the event's
  `DndSession` and that a linked tool call belongs to the same turn.

## Campaign creation order

Campaign creation follows this dependency order:

1. Create the `DndSession` pinned to one scenario key and version.
2. Create `LocationInstance` rows, then resolve parent locations.
3. Create `NPCInstance` rows and resolve their initial locations.
4. Create `QuestInstance` rows and translate related template IDs to runtime
   instance IDs.
5. Create `WorldLore` associations.
6. Create the selected `CharacterInstance` rows and their current mechanics.
7. Set the campaign's starting location and initial `state_json`.

No embedding request occurs during campaign creation.
