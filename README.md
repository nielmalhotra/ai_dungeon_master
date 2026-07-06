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
