import logging
import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction
from openai import OpenAI

from .models import WorldLoreChunkTemplate


VERSION_PATTERN = re.compile(r"^Version (?P<version>\d+\.\d+)$")
HEADING_PATTERN = re.compile(r"^#{1,2} (?P<title>.+)$")
EXCLUDED_FILES = {"ATTRIBUTION.txt"}
logger = logging.getLogger(__name__)


class ScenarioLoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScenarioChunk:
    version: str
    source_file: str
    section: str
    chunk_number: int
    content: str
    metadata: dict


@dataclass(frozen=True)
class EmbeddedScenarioChunk:
    chunk: ScenarioChunk
    embedding: list[float]


def _scenario_files(scenario_dir):
    files = [
        path
        for path in Path(scenario_dir).glob("*.txt")
        if path.name not in EXCLUDED_FILES
    ]
    return sorted(files, key=lambda path: (path.name != "main.txt", path.name))


def _parse_document(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ScenarioLoreError(f"{path.name} is empty.")

    version_match = VERSION_PATTERN.fullmatch(lines[0].strip())
    if version_match is None:
        raise ScenarioLoreError(
            f"{path.name} must begin with 'Version MajorNumber.MinorNumber'."
        )

    document_title = path.stem.replace("_", " ").title()
    sections = []
    current_title = document_title
    current_lines = []

    for line in lines[1:]:
        heading_match = HEADING_PATTERN.fullmatch(line.strip())
        if heading_match:
            if current_lines:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = heading_match.group("title").strip()
            current_lines = []
            continue
        current_lines.append(line.rstrip())

    if current_lines:
        sections.append((current_title, "\n".join(current_lines).strip()))

    sections = [(title, body) for title, body in sections if body]
    if not sections:
        raise ScenarioLoreError(f"{path.name} contains no lore.")

    return version_match.group("version"), document_title, sections


def load_scenario_chunks(scenario_dir=None):
    scenario_dir = Path(scenario_dir or settings.SCENARIO_DIR)
    files = _scenario_files(scenario_dir)
    if not files or files[0].name != "main.txt":
        raise ScenarioLoreError("The scenario must contain main.txt.")

    chunks = []
    expected_version = None
    for path in files:
        version, document_title, sections = _parse_document(path)
        if expected_version is None:
            expected_version = version
        elif version != expected_version:
            raise ScenarioLoreError(
                f"{path.name} is Version {version}; expected Version {expected_version}."
            )

        for chunk_number, (section, body) in enumerate(sections, start=1):
            content = f"{document_title}\n{section}\n\n{body}"
            chunks.append(
                ScenarioChunk(
                    version=version,
                    source_file=path.name,
                    section=section,
                    chunk_number=chunk_number,
                    content=content,
                    metadata={
                        "document_title": document_title,
                        "section": section,
                        "visibility": "player" if path.name == "main.txt" else "game_master",
                    },
                )
            )

    return chunks


def embed_scenario_chunks(chunks, client=None, batch_size=100):
    if batch_size < 1:
        raise ScenarioLoreError("Embedding batch size must be at least 1.")
    if not settings.OPENAI_API_KEY and client is None:
        raise ScenarioLoreError("OPENAI_API_KEY is required to build scenario lore.")

    client = client or OpenAI(api_key=settings.OPENAI_API_KEY)
    embedded_chunks = []

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        request = {
            "model": settings.OPENAI_EMBEDDING_MODEL,
            "input": [chunk.content for chunk in batch],
        }
        if settings.OPENAI_EMBEDDING_MODEL.startswith("text-embedding-3"):
            request["dimensions"] = settings.EMBEDDING_DIMENSIONS

        try:
            response = client.embeddings.create(**request)
        except Exception as exc:
            logger.exception("Unable to embed the scenario corpus")
            raise ScenarioLoreError("The scenario embedding request failed.") from exc

        embeddings = sorted(response.data, key=lambda item: item.index)
        if len(embeddings) != len(batch):
            raise ScenarioLoreError("The embedding response was incomplete.")

        for chunk, result in zip(batch, embeddings):
            embedding = list(result.embedding)
            if len(embedding) != settings.EMBEDDING_DIMENSIONS:
                raise ScenarioLoreError(
                    f"Expected {settings.EMBEDDING_DIMENSIONS} embedding dimensions, "
                    f"received {len(embedding)}."
                )
            embedded_chunks.append(
                EmbeddedScenarioChunk(chunk=chunk, embedding=embedding)
            )

    return embedded_chunks


def sync_world_lore_chunk_templates(
    scenario_dir=None,
    scenario_key=None,
    client=None,
    batch_size=100,
):
    scenario_key = scenario_key or settings.SCENARIO_KEY
    chunks = load_scenario_chunks(scenario_dir)
    embedded_chunks = embed_scenario_chunks(
        chunks,
        client=client,
        batch_size=batch_size,
    )
    version = chunks[0].version

    templates = [
        WorldLoreChunkTemplate(
            scenario_key=scenario_key,
            version=item.chunk.version,
            active=True,
            source_file=item.chunk.source_file,
            section=item.chunk.section,
            chunk_number=item.chunk.chunk_number,
            content=item.chunk.content,
            metadata=item.chunk.metadata,
            embedding=item.embedding,
        )
        for item in embedded_chunks
    ]

    with transaction.atomic():
        WorldLoreChunkTemplate.objects.filter(scenario_key=scenario_key).update(
            active=False
        )
        WorldLoreChunkTemplate.objects.filter(
            scenario_key=scenario_key,
            version=version,
        ).delete()
        WorldLoreChunkTemplate.objects.bulk_create(templates)

    return version, len(templates)
