import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID

from django.conf import settings
from django.db import transaction
from openai import OpenAI

from .models import (
    LocationTemplate,
    NPCTemplate,
    QuestTemplate,
    Visibility,
    WorldLoreChunkTemplate,
)


VERSION_DIRECTORY_PATTERN = re.compile(r"^v(?P<version>[1-9]\d*)$")
RELATED_UUID_PATTERN = re.compile(
    r"^Related (?P<entity_type>NPC|Location|Quest|World Lore) UUID$"
)
REQUIRED_DEFINITION_FOLDERS = ("locations", "npcs", "quests", "worldlore")
DEFINITION_TYPES = {
    "locations": "location",
    "npcs": "npc",
    "quests": "quest",
    "worldlore": "world_lore",
}
TEMPLATE_MODELS = {
    "location": LocationTemplate,
    "npc": NPCTemplate,
    "quest": QuestTemplate,
    "world_lore": WorldLoreChunkTemplate,
}
logger = logging.getLogger(__name__)


class ScenarioLoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScenarioDefinition:
    definition_type: str
    definition_uuid: UUID
    version: int
    source_file: str
    name: str
    initially_known: bool
    public_info: str
    dm_only: str
    parent_location_uuid: Optional[UUID] = None
    initial_location_uuid: Optional[UUID] = None
    is_starting_location: bool = False
    initial_status: Optional[str] = None
    related_references: tuple[tuple[str, UUID], ...] = ()


@dataclass(frozen=True)
class ScenarioChunk:
    version: int
    definition_uuid: UUID
    source_file: str
    title: str
    section: str
    chunk_number: int
    visibility: str
    initially_known: bool
    content: str


@dataclass(frozen=True)
class ScenarioRelease:
    scenario_key: str
    version: int
    definitions: tuple[ScenarioDefinition, ...]
    lore_chunks: tuple[ScenarioChunk, ...]

    def definitions_of_type(self, definition_type):
        return tuple(
            definition
            for definition in self.definitions
            if definition.definition_type == definition_type
        )


@dataclass(frozen=True)
class ScenarioEmbeddingInput:
    key: str
    content: str


@dataclass(frozen=True)
class EmbeddedScenarioChunk:
    chunk: ScenarioChunk
    embedding: list[float]


def _scenario_path(scenario_dir, scenario_key):
    root = Path(scenario_dir or settings.SCENARIO_DIR)
    if root.name == scenario_key:
        return root
    return root / scenario_key


def _latest_release_directory(scenario_dir, scenario_key):
    scenario_path = _scenario_path(scenario_dir, scenario_key)
    if not scenario_path.is_dir():
        raise ScenarioLoreError(
            f"Scenario directory does not exist: {scenario_path}."
        )

    releases = []
    for path in scenario_path.iterdir():
        match = VERSION_DIRECTORY_PATTERN.fullmatch(path.name)
        if path.is_dir() and match:
            releases.append((int(match.group("version")), path))
    if not releases:
        raise ScenarioLoreError(
            f"Scenario {scenario_key!r} has no vN release directory."
        )
    return max(releases, key=lambda item: item[0])


def _parse_bool(value, field_name, path):
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ScenarioLoreError(
        f"{path.name} has invalid {field_name!r}; expected true or false."
    )


def _parse_uuid(value, field_name, path):
    try:
        return UUID(value.strip())
    except (TypeError, ValueError) as exc:
        raise ScenarioLoreError(
            f"{path.name} has invalid {field_name!r}: {value!r}."
        ) from exc


def _single_header(headers, key, path, required=True):
    values = headers.get(key, [])
    if not values:
        if required:
            raise ScenarioLoreError(f"{path.name} is missing {key!r}.")
        return None
    if len(values) != 1:
        raise ScenarioLoreError(f"{path.name} contains {key!r} more than once.")
    return values[0]


def _definition_sections(lines, path):
    try:
        public_index = lines.index("# Public Info")
        dm_index = lines.index("# DM Only")
    except ValueError as exc:
        raise ScenarioLoreError(
            f"{path.name} must contain '# Public Info' and '# DM Only'."
        ) from exc
    if public_index >= dm_index:
        raise ScenarioLoreError(
            f"{path.name} must place '# Public Info' before '# DM Only'."
        )

    headers = {}
    for line in lines[:public_index]:
        if not line.strip():
            continue
        if ":" not in line:
            raise ScenarioLoreError(
                f"{path.name} has an invalid header line: {line!r}."
            )
        key, value = line.split(":", 1)
        headers.setdefault(key.strip(), []).append(value.strip())

    public_info = "\n".join(lines[public_index + 1 : dm_index]).strip()
    dm_only = "\n".join(lines[dm_index + 1 :]).strip()
    if not public_info and not dm_only:
        raise ScenarioLoreError(f"{path.name} contains no scenario content.")
    return headers, public_info, dm_only


def _related_references(headers, path):
    references = []
    type_names = {
        "NPC": "npc",
        "Location": "location",
        "Quest": "quest",
        "World Lore": "world_lore",
    }
    for key, values in headers.items():
        match = RELATED_UUID_PATTERN.fullmatch(key)
        if not match:
            continue
        definition_type = type_names[match.group("entity_type")]
        for value in values:
            references.append(
                (definition_type, _parse_uuid(value, key, path))
            )
    return tuple(references)


def _parse_definition(path, definition_type, version, release_dir):
    lines = path.read_text(encoding="utf-8").splitlines()
    headers, public_info, dm_only = _definition_sections(lines, path)
    definition_uuid = _parse_uuid(
        _single_header(headers, "Definition UUID", path),
        "Definition UUID",
        path,
    )
    initially_known = _parse_bool(
        _single_header(headers, "Initially Known", path),
        "Initially Known",
        path,
    )
    source_file = path.relative_to(release_dir).as_posix()
    common = {
        "definition_type": definition_type,
        "definition_uuid": definition_uuid,
        "version": version,
        "source_file": source_file,
        "initially_known": initially_known,
        "public_info": public_info,
        "dm_only": dm_only,
        "related_references": _related_references(headers, path),
    }

    if definition_type == "location":
        parent_value = _single_header(
            headers,
            "Parent Location UUID",
            path,
            required=False,
        )
        return ScenarioDefinition(
            name=_single_header(headers, "Name", path),
            parent_location_uuid=(
                _parse_uuid(parent_value, "Parent Location UUID", path)
                if parent_value
                else None
            ),
            is_starting_location=_parse_bool(
                _single_header(headers, "Starting Location", path),
                "Starting Location",
                path,
            ),
            **common,
        )

    if definition_type == "npc":
        location_value = _single_header(
            headers,
            "Initial Location UUID",
            path,
            required=False,
        )
        return ScenarioDefinition(
            name=_single_header(headers, "Name", path),
            initial_location_uuid=(
                _parse_uuid(location_value, "Initial Location UUID", path)
                if location_value
                else None
            ),
            **common,
        )

    if definition_type == "quest":
        initial_status = _single_header(headers, "Initial Status", path)
        if initial_status not in QuestTemplate.InitialStatus.values:
            raise ScenarioLoreError(
                f"{path.name} has invalid Initial Status {initial_status!r}."
            )
        return ScenarioDefinition(
            name=_single_header(headers, "Title", path),
            initial_status=initial_status,
            **common,
        )

    return ScenarioDefinition(
        name=_single_header(headers, "Title", path),
        **common,
    )


def _content_chunks(content, default_section):
    chunks = []
    current_section = default_section
    current_lines = []
    for line in content.splitlines():
        if line.startswith("## "):
            if current_lines:
                chunks.append((current_section, "\n".join(current_lines).strip()))
            current_section = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        chunks.append((current_section, "\n".join(current_lines).strip()))
    return [(section, body) for section, body in chunks if body]


def _lore_chunks(definition):
    chunks = []
    chunk_number = 1
    for visibility, content, default_section in (
        (Visibility.PUBLIC_INFO, definition.public_info, "Public Info"),
        (Visibility.DM_ONLY, definition.dm_only, "DM Only"),
    ):
        for section, body in _content_chunks(content, default_section):
            chunks.append(
                ScenarioChunk(
                    version=definition.version,
                    definition_uuid=definition.definition_uuid,
                    source_file=definition.source_file,
                    title=definition.name,
                    section=section,
                    chunk_number=chunk_number,
                    visibility=visibility,
                    initially_known=(
                        definition.initially_known
                        and visibility == Visibility.PUBLIC_INFO
                    ),
                    content=f"{definition.name}\n{section}\n\n{body}",
                )
            )
            chunk_number += 1
    return tuple(chunks)


def _validate_release(definitions, scenario_key):
    definitions_by_uuid = {}
    starting_locations = []
    for definition in definitions:
        previous = definitions_by_uuid.get(definition.definition_uuid)
        if previous:
            raise ScenarioLoreError(
                f"Definition UUID {definition.definition_uuid} is used by both "
                f"{previous.source_file} and {definition.source_file}."
            )
        definitions_by_uuid[definition.definition_uuid] = definition
        if definition.definition_type == "location" and definition.is_starting_location:
            starting_locations.append(definition)

    if len(starting_locations) != 1:
        raise ScenarioLoreError(
            f"Scenario {scenario_key!r} must have exactly one starting location."
        )

    for definition in definitions:
        if definition.parent_location_uuid:
            target = definitions_by_uuid.get(definition.parent_location_uuid)
            if not target or target.definition_type != "location":
                raise ScenarioLoreError(
                    f"{definition.source_file} references an unknown parent location."
                )
        if definition.initial_location_uuid:
            target = definitions_by_uuid.get(definition.initial_location_uuid)
            if not target or target.definition_type != "location":
                raise ScenarioLoreError(
                    f"{definition.source_file} references an unknown initial location."
                )
        for expected_type, related_uuid in definition.related_references:
            target = definitions_by_uuid.get(related_uuid)
            if not target or target.definition_type != expected_type:
                raise ScenarioLoreError(
                    f"{definition.source_file} references an unknown {expected_type} "
                    f"definition {related_uuid}."
                )


def load_scenario_release(scenario_dir=None, scenario_key=None):
    scenario_key = scenario_key or settings.SCENARIO_KEY
    version, release_dir = _latest_release_directory(scenario_dir, scenario_key)
    definitions = []

    for folder_name in REQUIRED_DEFINITION_FOLDERS:
        folder = release_dir / folder_name
        if not folder.is_dir():
            raise ScenarioLoreError(
                f"Scenario {scenario_key!r} v{version} is missing {folder_name}/."
            )
        files = sorted(folder.glob("*.txt"))
        if not files:
            raise ScenarioLoreError(
                f"Scenario {scenario_key!r} v{version} has no definitions in "
                f"{folder_name}/."
            )
        definitions.extend(
            _parse_definition(
                path,
                DEFINITION_TYPES[folder_name],
                version,
                release_dir,
            )
            for path in files
        )

    _validate_release(definitions, scenario_key)
    lore_chunks = tuple(
        chunk
        for definition in definitions
        if definition.definition_type == "world_lore"
        for chunk in _lore_chunks(definition)
    )
    return ScenarioRelease(
        scenario_key=scenario_key,
        version=version,
        definitions=tuple(definitions),
        lore_chunks=lore_chunks,
    )


def load_scenario_chunks(scenario_dir=None, scenario_key=None):
    return list(
        load_scenario_release(
            scenario_dir=scenario_dir,
            scenario_key=scenario_key,
        ).lore_chunks
    )


def build_scenario_embedding_inputs(release):
    inputs = []
    for definition in release.definitions:
        if definition.definition_type == "world_lore":
            continue
        for visibility, content in (
            (Visibility.PUBLIC_INFO, definition.public_info),
            (Visibility.DM_ONLY, definition.dm_only),
        ):
            if content:
                inputs.append(
                    ScenarioEmbeddingInput(
                        key=(
                            f"{definition.definition_type}:"
                            f"{definition.definition_uuid}:{visibility}"
                        ),
                        content=f"{definition.name}\n\n{content}",
                    )
                )
    for chunk in release.lore_chunks:
        inputs.append(
            ScenarioEmbeddingInput(
                key=(
                    f"world_lore:{chunk.definition_uuid}:"
                    f"{chunk.chunk_number}:{chunk.visibility}"
                ),
                content=chunk.content,
            )
        )
    return inputs


def embed_scenario_inputs(inputs, client=None, batch_size=100):
    if batch_size < 1:
        raise ScenarioLoreError("Embedding batch size must be at least 1.")
    if not settings.OPENAI_API_KEY and client is None:
        raise ScenarioLoreError("OPENAI_API_KEY is required to build scenario lore.")

    client = client or OpenAI(api_key=settings.OPENAI_API_KEY)
    embeddings = {}
    for start in range(0, len(inputs), batch_size):
        batch = inputs[start : start + batch_size]
        request = {
            "model": settings.OPENAI_EMBEDDING_MODEL,
            "input": [item.content for item in batch],
        }
        if settings.OPENAI_EMBEDDING_MODEL.startswith("text-embedding-3"):
            request["dimensions"] = settings.EMBEDDING_DIMENSIONS

        try:
            response = client.embeddings.create(**request)
        except Exception as exc:
            logger.exception("Unable to embed the scenario corpus")
            raise ScenarioLoreError("The scenario embedding request failed.") from exc

        results = sorted(response.data, key=lambda item: item.index)
        if len(results) != len(batch):
            raise ScenarioLoreError("The embedding response was incomplete.")
        for item, result in zip(batch, results):
            embedding = list(result.embedding)
            if len(embedding) != settings.EMBEDDING_DIMENSIONS:
                raise ScenarioLoreError(
                    f"Expected {settings.EMBEDDING_DIMENSIONS} embedding dimensions, "
                    f"received {len(embedding)}."
                )
            embeddings[item.key] = embedding
    return embeddings


def embed_scenario_chunks(chunks, client=None, batch_size=100):
    inputs = [
        ScenarioEmbeddingInput(key=str(index), content=chunk.content)
        for index, chunk in enumerate(chunks)
    ]
    embeddings = embed_scenario_inputs(
        inputs,
        client=client,
        batch_size=batch_size,
    )
    return [
        EmbeddedScenarioChunk(chunk=chunk, embedding=embeddings[str(index)])
        for index, chunk in enumerate(chunks)
    ]


def _definition_state(definition):
    return {
        "public_info": (
            {"summary": definition.public_info} if definition.public_info else {}
        ),
        "dm_only": {"summary": definition.dm_only} if definition.dm_only else {},
    }


def _embedding_key(definition, visibility):
    return f"{definition.definition_type}:{definition.definition_uuid}:{visibility}"


def _immutable_template(model, lookup, values, compared_fields):
    template = model.objects.filter(**lookup).first()
    if template is None:
        return model.objects.create(**lookup, **values), True
    for field_name in compared_fields:
        if getattr(template, field_name) != values[field_name]:
            raise ScenarioLoreError(
                f"Scenario release content changed without a version increase: "
                f"{values['source_file']}."
            )
    return template, False


def _verify_uuid_history(release):
    for definition in release.definitions:
        for definition_type, model in TEMPLATE_MODELS.items():
            if definition_type == definition.definition_type:
                continue
            if model.objects.filter(
                scenario_key=release.scenario_key,
                definition_uuid=definition.definition_uuid,
            ).exists():
                raise ScenarioLoreError(
                    f"Definition UUID {definition.definition_uuid} changed type in "
                    f"scenario {release.scenario_key!r}."
                )


def _resolved_references(definition, templates_by_uuid):
    return [
        {
            "type": definition_type,
            "id": templates_by_uuid[related_uuid].id,
        }
        for definition_type, related_uuid in definition.related_references
    ]


def sync_scenario_templates(
    scenario_dir=None,
    scenario_key=None,
    client=None,
    batch_size=100,
):
    release = load_scenario_release(
        scenario_dir=scenario_dir,
        scenario_key=scenario_key,
    )
    embedding_inputs = build_scenario_embedding_inputs(release)
    embeddings = embed_scenario_inputs(
        embedding_inputs,
        client=client,
        batch_size=batch_size,
    )

    activated_ids = {model: [] for model in TEMPLATE_MODELS.values()}
    templates_by_uuid = {}
    created_templates = set()

    with transaction.atomic():
        _verify_uuid_history(release)

        for definition in release.definitions_of_type("location"):
            values = {
                "active": False,
                "source_file": definition.source_file,
                "name": definition.name,
                "parent_template": None,
                "is_starting_location": definition.is_starting_location,
                "initially_known": definition.initially_known,
                "definition_json": _definition_state(definition),
                "metadata_json": {},
                "public_embedding": embeddings.get(
                    _embedding_key(definition, Visibility.PUBLIC_INFO)
                ),
                "dm_embedding": embeddings.get(
                    _embedding_key(definition, Visibility.DM_ONLY)
                ),
            }
            template, created = _immutable_template(
                LocationTemplate,
                {
                    "scenario_key": release.scenario_key,
                    "version": release.version,
                    "definition_uuid": definition.definition_uuid,
                },
                values,
                (
                    "source_file",
                    "name",
                    "is_starting_location",
                    "initially_known",
                    "definition_json",
                    "metadata_json",
                ),
            )
            templates_by_uuid[definition.definition_uuid] = template
            activated_ids[LocationTemplate].append(template.id)
            if created:
                created_templates.add((LocationTemplate, template.id))

        for definition in release.definitions_of_type("location"):
            template = templates_by_uuid[definition.definition_uuid]
            parent = (
                templates_by_uuid[definition.parent_location_uuid]
                if definition.parent_location_uuid
                else None
            )
            if (LocationTemplate, template.id) in created_templates:
                template.parent_template = parent
                template.save(update_fields=["parent_template"])
            elif template.parent_template_id != (parent.id if parent else None):
                raise ScenarioLoreError(
                    f"Scenario release content changed without a version increase: "
                    f"{definition.source_file}."
                )

        for definition in release.definitions_of_type("npc"):
            initial_location = (
                templates_by_uuid[definition.initial_location_uuid]
                if definition.initial_location_uuid
                else None
            )
            values = {
                "active": False,
                "source_file": definition.source_file,
                "name": definition.name,
                "initial_location_template": initial_location,
                "initially_known": definition.initially_known,
                "definition_json": _definition_state(definition),
                "metadata_json": {},
                "public_embedding": embeddings.get(
                    _embedding_key(definition, Visibility.PUBLIC_INFO)
                ),
                "dm_embedding": embeddings.get(
                    _embedding_key(definition, Visibility.DM_ONLY)
                ),
            }
            template, _ = _immutable_template(
                NPCTemplate,
                {
                    "scenario_key": release.scenario_key,
                    "version": release.version,
                    "definition_uuid": definition.definition_uuid,
                },
                values,
                (
                    "source_file",
                    "name",
                    "initial_location_template",
                    "initially_known",
                    "definition_json",
                    "metadata_json",
                ),
            )
            templates_by_uuid[definition.definition_uuid] = template
            activated_ids[NPCTemplate].append(template.id)

        quest_definitions = release.definitions_of_type("quest")
        for definition in quest_definitions:
            values = {
                "active": False,
                "source_file": definition.source_file,
                "title": definition.name,
                "initial_status": definition.initial_status,
                "initially_known": definition.initially_known,
                "definition_json": _definition_state(definition),
                "related_templates_json": [],
                "metadata_json": {},
                "public_embedding": embeddings.get(
                    _embedding_key(definition, Visibility.PUBLIC_INFO)
                ),
                "dm_embedding": embeddings.get(
                    _embedding_key(definition, Visibility.DM_ONLY)
                ),
            }
            template = QuestTemplate.objects.filter(
                scenario_key=release.scenario_key,
                version=release.version,
                definition_uuid=definition.definition_uuid,
            ).first()
            if template is None:
                template = QuestTemplate.objects.create(
                    scenario_key=release.scenario_key,
                    version=release.version,
                    definition_uuid=definition.definition_uuid,
                    **values,
                )
                created_templates.add((QuestTemplate, template.id))
            templates_by_uuid[definition.definition_uuid] = template
            activated_ids[QuestTemplate].append(template.id)

        for definition in quest_definitions:
            template = templates_by_uuid[definition.definition_uuid]
            related_templates = _resolved_references(
                definition,
                templates_by_uuid,
            )
            expected = {
                "source_file": definition.source_file,
                "title": definition.name,
                "initial_status": definition.initial_status,
                "initially_known": definition.initially_known,
                "definition_json": _definition_state(definition),
                "related_templates_json": related_templates,
                "metadata_json": {},
            }
            if (QuestTemplate, template.id) in created_templates:
                template.related_templates_json = related_templates
                template.save(update_fields=["related_templates_json"])
            else:
                for field_name, value in expected.items():
                    if getattr(template, field_name) != value:
                        raise ScenarioLoreError(
                            "Scenario release content changed without a version "
                            f"increase: {definition.source_file}."
                        )

        lore_definitions = {
            definition.definition_uuid: definition
            for definition in release.definitions_of_type("world_lore")
        }
        for chunk in release.lore_chunks:
            definition = lore_definitions[chunk.definition_uuid]
            related_templates = _resolved_references(
                definition,
                templates_by_uuid,
            )
            embedding_key = (
                f"world_lore:{chunk.definition_uuid}:"
                f"{chunk.chunk_number}:{chunk.visibility}"
            )
            values = {
                "active": False,
                "source_file": chunk.source_file,
                "title": chunk.title,
                "section": chunk.section,
                "visibility": chunk.visibility,
                "initially_known": chunk.initially_known,
                "content": chunk.content,
                "metadata_json": {"related_templates": related_templates},
                "embedding": embeddings[embedding_key],
            }
            template, _ = _immutable_template(
                WorldLoreChunkTemplate,
                {
                    "scenario_key": release.scenario_key,
                    "version": release.version,
                    "definition_uuid": chunk.definition_uuid,
                    "chunk_number": chunk.chunk_number,
                },
                values,
                (
                    "source_file",
                    "title",
                    "section",
                    "visibility",
                    "initially_known",
                    "content",
                    "metadata_json",
                ),
            )
            activated_ids[WorldLoreChunkTemplate].append(template.id)

        for model, ids in activated_ids.items():
            model.objects.filter(scenario_key=release.scenario_key).update(active=False)
            model.objects.filter(id__in=ids).update(active=True)

    return release.version, sum(len(ids) for ids in activated_ids.values())


def sync_world_lore_chunk_templates(*args, **kwargs):
    return sync_scenario_templates(*args, **kwargs)
