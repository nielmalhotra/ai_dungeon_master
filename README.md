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
`locations/`, `npcs/`, `quests/`, and `worldlore/` folders. Definition UUIDs are
stable across releases and relationships in source files are resolved to template
IDs during synchronization.

Build or refresh the canonical scenario templates locally with:

```bash
python3 manage.py sync_scenario_lore
```

On the production server, run the same command through Docker:

```bash
docker compose run --rm web python manage.py sync_scenario_lore
```

The command embeds public and private source content in batches and atomically
activates the location, NPC, quest, and world-lore templates for that release.
Previous versions remain available but inactive. Creating a game performs no
OpenAI request: it instantiates the active release, resolves runtime relationships,
and pins the campaign to that exact version.

Whitesparrow and *The Night Blade* contain adapted CC BY 4.0 material. Their
required attribution and modification notice are in
`scenario/whitesparrow/ATTRIBUTION.txt`; that scenario material is not covered by
the repository's MIT license.
