# Combatless Campaign Contract

This document describes the implemented source-file, database, and campaign
initialization contract for the combatless AI Dungeon Master.

## Design Rules

- `DndSession` is one long-running campaign.
- Scenario templates are immutable, versioned, and retained after newer releases
  are activated.
- Runtime entities point to the exact template version from which they were
  instantiated.
- Stable UUIDs identify authored definitions across scenario versions. Database
  primary keys identify synchronized templates and campaign instances.
- Every authored and runtime entity can use `relationships_json` to connect to
  locations, NPCs, quests, and world lore.
- Typed foreign keys such as `parent_location` and `current_location` are useful
  projections of relationship data, not a replacement for relationships.
- `state_json` describes what an entity is like now. Historical changes belong
  in `WorldEvent` records.
- Rules tools own authoritative state transitions. The AI decides intent and
  fictional applicability, while tools validate campaign ownership, input shape,
  state invariants, and audit records.
- Combat remains outside the current scope.
- Player characters share player-safe knowledge through the Fairy of Knowledge,
  so the system does not maintain independent player-character knowledge state.
- The system does not store pairwise character distance or perceptibility state.
  Character interaction is guaranteed at the same current location and is
  otherwise adjudicated by the AI from the established fiction and nested
  location hierarchy.

## Visibility

Authored `definition_json` and mutable `state_json` use this envelope:

```json
{
  "public_info": {
    "summary": "Current truth that is safe for the DM to reveal."
  },
  "dm_only": {
    "summary": "Current hidden truth available only to the DM."
  }
}
```

`public_info` means reveal-safe, not necessarily already narrated. The system
does not maintain a second ledger of revealed facts or model whether players
remember them. `dm_only` must never be sent to a player-facing response.

An entity listed in `initially_known_entities_json` is known or introduced at
campaign creation. That never makes its `dm_only` branch public.

All player-safe information learned by any player character becomes shared
party knowledge through the Fairy of Knowledge. The Fairy never reveals or
shares `dm_only` information. Because this prototype intentionally has no
per-character knowledge ledger, shared knowledge is represented by the campaign
history and current public state rather than duplicated on each character.

When a campaign is created, each entity instance receives a copy of its
template's full visibility envelope. Later events and validated tools update the
instance state while the template remains unchanged.

## Scenario Layout

Scenario releases live at:

```text
scenario/<scenario_key>/v<positive integer>/
  init.txt
  locations/*.txt
  npcs/*.txt
  quests/*.txt
  worldlore/*.txt
```

The synchronizer selects the greatest numeric version unless a version is
explicitly supplied to validation or UUID-population functions. All four entity
folders and `init.txt` are required.

## Entity Files

Every entity file begins with a UUID. New files may temporarily use
`UUID: INSERT_UUID_HERE` until the UUID population command runs.

### Location

```text
UUID: <uuid>
NAME: <name>
INITIAL STATUS: hidden|active|destroyed

RELATIONSHIPS:
<relation> | <entity_type> | <uuid>

PUBLIC:
<reveal-safe authored truth>

DM_ONLY:
<hidden authored truth>
```

### NPC

```text
UUID: <uuid>
NAME: <name>
INITIAL STATUS: hidden|active|dead

RELATIONSHIPS:
<relation> | <entity_type> | <uuid>

PUBLIC:
<reveal-safe authored truth>

DM_ONLY:
<hidden authored truth>
```

### Quest

```text
UUID: <uuid>
NAME: <name>
INITIAL STATUS: hidden|available|active

RELATIONSHIPS:
<relation> | <entity_type> | <uuid>

CONDITIONS:
0 | <first completion condition>
1 | <second completion condition>

PUBLIC:
<reveal-safe authored truth>

DM_ONLY:
<hidden authored truth>
```

### World Lore

```text
UUID: <uuid>
NAME: <name>

RELATIONSHIPS:
<relation> | <entity_type> | <uuid>

PUBLIC:
<reveal-safe authored truth>

DM_ONLY:
<hidden authored truth>
```

World lore has no status. Empty relationship, public, or DM-only sections are
allowed, but an entity must contain some public or DM-only description.

Every quest has at least one condition. Condition order is explicit, zero-based,
and contiguous through the final step. Condition text is public because it is
not authored under `DM_ONLY`.

Relationship names use lowercase snake case. Allowed target types are
`location`, `npc`, `quest`, and `world_lore`. Each target UUID must resolve to an
entity of the declared type in the same release. Examples include `contained_in`,
`located_in`, `involves`, and `about`.

A location may have at most one `contained_in` relationship. An NPC may have at
most one `located_in` relationship. Location containment cycles are rejected.

## Initialization File

`init.txt` intentionally has no UUID:

```text
STARTING LOCATION:
<location uuid>

MAIN QUEST:
<quest uuid>

OPENING:
<canned opening presented to everyone>

KNOWN ENTITIES:
<uuid>
<uuid>

DM_ONLY:
<private initialization instructions>
```

The starting location must resolve to a location and the main quest must resolve
to a quest. Known entities may be any authored entity type, use one UUID per
line, and cannot contain duplicates.

## UUID Population

UUID population processes only entity files whose first line is exactly
`UUID: INSERT_UUID_HERE`.

For each placeholder, it compares the complete file text after the UUID line
against earlier releases of the same scenario and entity type:

- One exact match reuses that earlier definition UUID.
- No exact match generates a new UUID4.
- Matches with conflicting UUIDs fail.
- UUID reuse across different entity types fails.
- The complete release is parsed and validated before any file is written.

This makes the operation atomic and preserves stable identities for unchanged
definitions copied into a new version.

## Relationship JSON

Synchronized templates store resolved integer template IDs:

```json
[
  {
    "relation": "located_in",
    "target": {"type": "location", "id": 12}
  }
]
```

Campaign instances store the same shape with runtime instance IDs. Runtime
targets may additionally use the `character` type.

Character relationships do not encode `engaged`, `near`, `far`, or perceptibility
values. Each character's `current_location` is authoritative. Characters at the
same current location can interact. When current locations differ, the AI uses
the nested location hierarchy and established fiction to determine whether an
interaction is possible.

Mutable narrative relationships are directional and live on the source
character or NPC inside both visibility branches of `state_json`:

```json
{
  "public_info": {
    "relationships": {
      "character": {},
      "npc": {"31": {"summary": "A trusted acquaintance."}},
      "location": {}
    }
  },
  "dm_only": {
    "relationships": {
      "character": {},
      "npc": {"31": {"summary": "Some doubt remains."}},
      "location": {}
    }
  }
}
```

The relationship tool receives a source, target, and complete replacement
objects for the public and DM-only entries. It derives the campaign from the
current agent run, validates that both endpoints belong to it, and atomically
replaces both entries. An empty object removes that visibility entry; two empty
objects remove the relationship. Repeating the current values is a successful
no-op. The AI does not supply a campaign ID, operation, or audit reason. The
generic state-patch tools cannot modify the reserved `relationships` branch.

## Campaign Model

`DndSession` owns the campaign and includes:

- `status`: `active`, `completed`, or `abandoned`.
- `scenario_key` and `scenario_version`: the exact synchronized release.
- `opening_text`: the `OPENING` section copied from `init.txt`.
- `initially_known_entities_json`: UUID lists grouped under `locations`, `npcs`,
  `quests`, and `world_lore`.
- `current_location`: the current scene focus.
- `main_quest`: the runtime quest selected by `init.txt`.
- `state_json`: current campaign-level public and DM-only state.

There can be at most one active campaign per user.

## Templates and Instances

All entity templates contain stable source identity, scenario version, active
release state, source path, authored definition data, relationships, metadata,
and embeddings. Entity instances contain mutable names or titles, status where
applicable, current state, and runtime relationships. Each quest template owns
ordered condition templates containing a string. Each quest instance owns one
condition instance per template condition plus a `steps_completed` counter.
Condition instances begin as `not_finished`; completion changes them to
`finished` and records a public `finish_text` describing what happened.

Statuses are deliberately small:

| Entity | Statuses |
| --- | --- |
| Campaign | `active`, `completed`, `abandoned` |
| Location | `hidden`, `active`, `destroyed` |
| NPC | `hidden`, `active`, `dead` |
| Quest | `hidden`, `available`, `active`, `finished` |
| Item | `active`, `consumed`, `destroyed` |

World lore is synchronized as public and DM-only chunks. `WorldLore` associates
those immutable chunks with a campaign and stores resolved runtime
relationships.

### Abilities

`AbilityTemplate` normalizes the name, embedded description, combat category,
resolution, deterministic effect, maximum uses, and recharge rule authored in a
character template. `AbilityInstance` belongs to one character and stores its
remaining uses and mutable visibility state. Combat abilities remain stored but
cannot be invoked by the combatless ability tool.

Every active ability owned by a player character is supplied directly to the DM
context. Its embedding supports semantic lookup but never determines whether the
DM remembers an owned ability.

### Items

`ItemTemplate` contains a stable key, public and DM-only definition data,
deterministic mechanics, and separate public and DM-only embeddings. Every unit
of an item is one `ItemInstance`; displayed quantity is the count of matching
active instances.

An active item has either a typed generic owner (`character` or `npc`) or a
current location. Mutable `state_json` uses the standard visibility envelope, so
facts such as a torch becoming wet can change without modifying the template.
Consumed and destroyed instances are retained for audit history.

### Roll Mechanics

Characters and NPCs store attributes, trained skills, strong saves, and active
`modifiers_json`. Ability checks add the chosen attribute and a `+2` trained-skill
bonus when applicable. Saving throws add the attribute and `+2` when it is a
strong save. The rules engine calculates these values; the AI never supplies a
final modifier.

Distinct advantage and disadvantage sources stack. Duplicate sources do not.
The AI may add validated situational advantage or disadvantage, but flat bonuses
and penalties may only originate in an ability, item, or system definition.
Temporary modifiers declare their roll scope and duration and are consumed or
expired by the rules engine. Rest restores maximum HP and all limited-use
abilities and removes every non-permanent modifier.

## Campaign Creation

Campaign creation is transactional and performs no embedding request:

1. Require exactly three distinct named character templates.
2. Lock the user and reject a second active campaign.
3. Load one complete active synchronized release.
4. Reparse that exact source release and verify it matches active templates.
5. Create the `DndSession` with opening, known entities, and initialization
   DM-only instructions.
6. Instantiate every location, NPC, quest, and world-lore chunk.
7. Instantiate every quest condition in authored order.
8. Copy template definition state and convert template relationships to runtime
   relationships.
9. Force the configured main quest to `active`.
10. Place all selected characters at the configured starting location.
11. Instantiate their normalized abilities and one item row per unit of starting
    gear.
12. Set the session's current location and main quest.

Quest status advances one state at a time from `hidden` to `available` to
`active`; it cannot regress or fail. A status transition may occur without
completing a condition. Conditions may only be completed on an active quest and
must be completed in exact zero-based order. Completing the final condition sets
the quest to `finished`. Finishing the session's main quest also sets the
campaign to `completed`. Completed and abandoned campaigns reject further
mutations; a completed campaign is displayed read-only.

## Turn and Audit Tables

The existing combatless turn architecture remains unchanged:

- `CampaignTurn` records one player input and structured DM response.
- `AgentRun` records model settings, traces, token use, failures, and cost.
- `RetrievedContextRecord` records retrieved public or DM-only context.
- `ToolCallRecord` records validated tool calls and results.
- `WorldEvent` records durable historical changes and related runtime entities.

Only validated application tools may mutate authoritative state or emit world
events.

Implemented deterministic tools cover raw dice, checks, saves, contests,
temporary roll modifiers, character and NPC movement, non-combat ability use,
rest, item transfer/placement/consumption/state, visibility-state patches,
directional relationship replacement, and ordered quest advancement. Movement
feasibility outside exact-location interaction is adjudicated by the AI;
movement tools enforce identity, campaign ownership, and the resulting
authoritative state change.

`advance_quest` accepts a quest ID and at least one of an optional next `state`
or optional zero-based `step_completed`. A completed step requires non-empty
`finish_text`. The transaction locks the campaign, quest, and condition; checks
campaign ownership, status, and exact order; updates the condition and counter;
and performs final quest and campaign completion when applicable. The resulting
world event stores condition text and `finish_text` in public state.
