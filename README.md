# ai_dungeon_master
AI dungeon master for dungeons and dragon

## Current scope

The current prototype focuses on a combatless Dungeon Master with persistent
campaign state, retrieval, tool use, and structured outputs. Combat is planned as
a future feature.

## Local development

From the project directory, start the local PostgreSQL database:

```bash
docker compose -f docker-compose.local.yml up -d db
```

Activate the virtual environment, apply migrations, and start Django:

```bash
source .venv/bin/activate
python3 manage.py migrate
python3 manage.py runserver
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

To stop the local PostgreSQL container:

```bash
docker compose -f docker-compose.local.yml down
```

## Scenario lore

The versioned source corpus lives at `scenario/<scenario_key>/vN/`. The
synchronizer selects the greatest numeric version and validates the required
`init.txt`, `locations/`, `npcs/`, `quests/`, and `worldlore/` sources.
Definition UUIDs are stable across releases and source relationships are resolved
to template IDs during synchronization.

Validate source files without changing the database:

```bash
python3 manage.py validate_scenario
```

Create an entity file with `UUID: INSERT_UUID_HERE`:

```bash
python3 manage.py create_scenario_definition \
  scenario/whitesparrow/v2 npc new_npc.txt "New NPC" \
  --initial-status hidden
```

Populate placeholders by exact text match against earlier releases, generating a
new UUID4 when no match exists:

```bash
python3 manage.py populate_scenario_uuids --scenario-version 2
```

Build or refresh the canonical scenario templates locally with:

```bash
python3 manage.py sync_scenario_lore
```

Synchronize and embed the normalized character abilities and item catalog with:

```bash
python3 manage.py sync_gameplay_templates
```

On the production server, run the corresponding commands through Docker:

```bash
docker compose run --rm web python manage.py sync_scenario_lore
docker compose run --rm web python manage.py sync_gameplay_templates
```

The scenario command embeds public and private source content in batches and atomically
activates the location, NPC, quest, and world-lore templates for that release.
Previous versions remain available but inactive. Creating a game performs no
OpenAI request: it instantiates the active release, resolves runtime relationships,
copies the opening and known-entity initialization, activates the main quest, and
pins the campaign to that exact version. Quests advance through authored,
zero-based conditions in order. Completing the final main-quest condition
completes the campaign and makes it read-only.

The complete source grammar, status values, visibility semantics, relationship
shape, and initialization behavior are documented in
`docs/combatless_campaign_schema.md`.

Whitesparrow and *The Night Blade* contain adapted CC BY 4.0 material. Their
required attribution and modification notice are in
`scenario/whitesparrow/ATTRIBUTION.txt`; that scenario material is not covered by
the repository's MIT license.
