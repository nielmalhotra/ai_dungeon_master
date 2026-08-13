# ai_dungeon_master
AI dungeon master for dungeons and dragon

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

The versioned source corpus lives in `scenario/`. Every lore file must begin with
`Version MajorNumber.MinorNumber`, and every file in one release must match the
version in `scenario/main.txt`. `main.txt` contains the player-visible premise;
the other lore files contain private game-master knowledge.

Build or refresh the canonical scenario templates locally with:

```bash
python3 manage.py sync_scenario_lore
```

On the production server, run the same command through Docker:

```bash
docker compose run --rm web python manage.py sync_scenario_lore
```

The command semantically chunks the files, embeds them in batches, and atomically
activates the resulting `world_lore_chunk_template` rows. Previous versions remain
available but inactive. Creating a game copies the active rows into `world_lore`,
so it performs no OpenAI request and existing sessions retain immutable snapshots.
The attribution file is retained with the source but is not embedded as game lore.
