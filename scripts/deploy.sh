#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.production.env"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

echo "Checking for model changes without migrations..."
"${PYTHON_BIN}" "${ROOT_DIR}/manage.py" makemigrations --check --dry-run

set -a
source "${ENV_FILE}"
set +a

SERVER_HOST="${SERVER_HOST:-98.84.115.152}"
SERVER_USER="${SERVER_USER:-ubuntu}"
SERVER_APP_DIR="${SERVER_APP_DIR:-/opt/ai-dungeon-master}"

if [[ -z "${SSH_KEY_PATH:-}" ]]; then
  echo "SSH_KEY_PATH is required in .production.env" >&2
  exit 1
fi

SSH_KEY_PATH="${SSH_KEY_PATH/#\~/${HOME}}"
SSH_OPTS=(-i "${SSH_KEY_PATH}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
REMOTE="${SERVER_USER}@${SERVER_HOST}"

ssh "${SSH_OPTS[@]}" "${REMOTE}" "sudo mkdir -p '${SERVER_APP_DIR}' && sudo chown '${SERVER_USER}:${SERVER_USER}' '${SERVER_APP_DIR}'"

rsync -az --delete \
  --exclude ".git" \
  --exclude ".env" \
  --exclude ".production.env" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  --exclude "db.sqlite3" \
  --exclude "staticfiles" \
  -e "ssh ${SSH_OPTS[*]}" \
  "${ROOT_DIR}/" "${REMOTE}:${SERVER_APP_DIR}/"

scp "${SSH_OPTS[@]}" "${ENV_FILE}" "${REMOTE}:${SERVER_APP_DIR}/.env"

ssh "${SSH_OPTS[@]}" "${REMOTE}" "cd '${SERVER_APP_DIR}' && docker compose build web && docker compose run --rm web python manage.py makemigrations --check --dry-run && docker compose up -d db && docker compose run --rm web python manage.py migrate && docker compose up -d"
