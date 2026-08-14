import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from uuid import UUID, uuid4

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
RELATION_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
UUID_LINE_PATTERN = re.compile(r"^UUID: (?P<uuid>.+)$")
UUID_PLACEHOLDER = "INSERT_UUID_HERE"
ENTITY_FOLDERS = {
    "locations": "location",
    "npcs": "npc",
    "quests": "quest",
    "worldlore": "world_lore",
}
ENTITY_TYPES = frozenset(ENTITY_FOLDERS.values())
KNOWN_ENTITY_KEYS = {
    "location": "locations",
    "npc": "npcs",
    "quest": "quests",
    "world_lore": "world_lore",
}
INITIAL_STATUS_VALUES = {
    "location": frozenset({"hidden", "active", "destroyed"}),
    "npc": frozenset({"hidden", "active", "dead"}),
    "quest": frozenset({"hidden", "available", "active", "finished"}),
}
REQUIRED_DEFINITION_FOLDERS = tuple(ENTITY_FOLDERS)
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
class SourceRelationship:
    relation: str
    target_type: str
    target_uuid: UUID


@dataclass(frozen=True)
class ScenarioDefinition:
    definition_type: str
    definition_uuid: UUID
    version: int
    source_file: str
    name: str
    initial_status: Optional[str]
    relationships: Tuple[SourceRelationship, ...]
    public_info: str
    dm_only: str
    match_text: str


@dataclass(frozen=True)
class ScenarioInitialization:
    starting_location_uuid: UUID
    main_quest_uuid: UUID
    opening: str
    known_entity_uuids: Tuple[UUID, ...]
    dm_only: str


@dataclass(frozen=True)
class ScenarioChunk:
    version: int
    definition_uuid: UUID
    source_file: str
    title: str
    section: str
    chunk_number: int
    visibility: str
    content: str


@dataclass(frozen=True)
class ScenarioRelease:
    scenario_key: str
    version: int
    release_dir: Path
    initialization: ScenarioInitialization
    definitions: Tuple[ScenarioDefinition, ...]
    lore_chunks: Tuple[ScenarioChunk, ...]

    def definitions_of_type(self, definition_type):
        return tuple(
            definition
            for definition in self.definitions
            if definition.definition_type == definition_type
        )

    def definition_by_uuid(self):
        return {
            definition.definition_uuid: definition
            for definition in self.definitions
        }


@dataclass(frozen=True)
class ScenarioEmbeddingInput:
    key: str
    content: str


@dataclass(frozen=True)
class EmbeddedScenarioChunk:
    chunk: ScenarioChunk
    embedding: list


def _scenario_path(scenario_dir, scenario_key):
    root = Path(scenario_dir or settings.SCENARIO_DIR)
    if root.name == scenario_key:
        return root
    return root / scenario_key


def _release_directories(scenario_dir, scenario_key):
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
    return sorted(releases, key=lambda item: item[0])


def _release_directory(scenario_dir, scenario_key, version=None):
    releases = _release_directories(scenario_dir, scenario_key)
    if version is None:
        return releases[-1]
    for release_version, release_dir in releases:
        if release_version == version:
            return release_version, release_dir
    raise ScenarioLoreError(
        f"Scenario {scenario_key!r} has no v{version} release directory."
    )


def _parse_uuid(value, label, source_name):
    try:
        return UUID(value.strip())
    except (TypeError, ValueError) as exc:
        raise ScenarioLoreError(
            f"{source_name} has invalid {label}: {value!r}."
        ) from exc


def _section_positions(lines, headings, source_name):
    positions = []
    for heading in headings:
        matches = [index for index, line in enumerate(lines) if line == heading]
        if len(matches) != 1:
            raise ScenarioLoreError(
                f"{source_name} must contain {heading!r} exactly once."
            )
        positions.append(matches[0])
    if positions != sorted(positions):
        raise ScenarioLoreError(
            f"{source_name} has sections in the wrong order."
        )
    return positions


def _nonempty_lines(lines):
    return [line.strip() for line in lines if line.strip()]


def _parse_relationship(line, source_name):
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != 3:
        raise ScenarioLoreError(
            f"{source_name} has invalid relationship line {line!r}; expected "
            "'relation | entity_type | UUID'."
        )
    relation, target_type, target_uuid = parts
    if not RELATION_NAME_PATTERN.fullmatch(relation):
        raise ScenarioLoreError(
            f"{source_name} has invalid relationship name {relation!r}."
        )
    if target_type not in ENTITY_TYPES:
        raise ScenarioLoreError(
            f"{source_name} has invalid relationship target type {target_type!r}."
        )
    return SourceRelationship(
        relation=relation,
        target_type=target_type,
        target_uuid=_parse_uuid(target_uuid, "relationship UUID", source_name),
    )


def _definition_type_for_path(path):
    try:
        return ENTITY_FOLDERS[path.parent.name]
    except KeyError as exc:
        raise ScenarioLoreError(
            f"Definition file {path} is not inside a supported entity folder."
        ) from exc


def _parse_definition_text(path, text, version, release_dir, allow_placeholder=False):
    definition_type = _definition_type_for_path(path)
    lines = text.splitlines()
    if not lines:
        raise ScenarioLoreError(f"{path.name} is empty.")

    uuid_match = UUID_LINE_PATTERN.fullmatch(lines[0])
    if uuid_match is None:
        raise ScenarioLoreError(
            f"{path.name} must begin with 'UUID: <uuid>'."
        )
    uuid_value = uuid_match.group("uuid")
    if uuid_value == UUID_PLACEHOLDER:
        if allow_placeholder:
            definition_uuid = UUID(int=0)
        else:
            raise ScenarioLoreError(
                f"{path.name} still contains UUID: {UUID_PLACEHOLDER}."
            )
    else:
        definition_uuid = _parse_uuid(uuid_value, "UUID", path.name)

    relationships_index, public_index, dm_index = _section_positions(
        lines,
        ("RELATIONSHIPS:", "PUBLIC:", "DM_ONLY:"),
        path.name,
    )
    header_lines = _nonempty_lines(lines[1:relationships_index])
    if not header_lines or not header_lines[0].startswith("NAME: "):
        raise ScenarioLoreError(
            f"{path.name} must put 'NAME: <name>' after its UUID."
        )
    if len([line for line in header_lines if line.startswith("NAME: ")]) != 1:
        raise ScenarioLoreError(f"{path.name} must contain exactly one NAME header.")
    name = header_lines[0].split(":", 1)[1].strip()
    if not name:
        raise ScenarioLoreError(f"{path.name} has an empty NAME.")

    initial_status = None
    extra_headers = header_lines[1:]
    if definition_type in INITIAL_STATUS_VALUES:
        if len(extra_headers) != 1 or not extra_headers[0].startswith(
            "INITIAL STATUS: "
        ):
            raise ScenarioLoreError(
                f"{path.name} requires exactly one INITIAL STATUS header."
            )
        initial_status = extra_headers[0].split(":", 1)[1].strip()
        if initial_status not in INITIAL_STATUS_VALUES[definition_type]:
            allowed = ", ".join(sorted(INITIAL_STATUS_VALUES[definition_type]))
            raise ScenarioLoreError(
                f"{path.name} has invalid INITIAL STATUS {initial_status!r}; "
                f"expected one of {allowed}."
            )
    elif extra_headers:
        raise ScenarioLoreError(
            f"{path.name} has unsupported headers: {', '.join(extra_headers)}."
        )

    relationships = tuple(
        _parse_relationship(line, path.name)
        for line in _nonempty_lines(lines[relationships_index + 1 : public_index])
    )
    public_info = "\n".join(lines[public_index + 1 : dm_index]).strip()
    dm_only = "\n".join(lines[dm_index + 1 :]).strip()
    if not public_info and not dm_only:
        raise ScenarioLoreError(f"{path.name} contains no entity description.")

    first_line_end = text.find("\n")
    match_text = "" if first_line_end < 0 else text[first_line_end + 1 :]
    return ScenarioDefinition(
        definition_type=definition_type,
        definition_uuid=definition_uuid,
        version=version,
        source_file=path.relative_to(release_dir).as_posix(),
        name=name,
        initial_status=initial_status,
        relationships=relationships,
        public_info=public_info,
        dm_only=dm_only,
        match_text=match_text,
    )


def parse_definition_file(
    path,
    version=None,
    release_dir=None,
    allow_placeholder=False,
):
    path = Path(path)
    release_dir = Path(release_dir or path.parent.parent)
    if version is None:
        match = VERSION_DIRECTORY_PATTERN.fullmatch(release_dir.name)
        if match is None:
            raise ScenarioLoreError(
                f"Cannot infer a version from {release_dir.name!r}."
            )
        version = int(match.group("version"))
    return _parse_definition_text(
        path,
        path.read_text(encoding="utf-8"),
        version,
        release_dir,
        allow_placeholder=allow_placeholder,
    )


def _single_uuid_section(lines, start, end, heading, source_name):
    values = _nonempty_lines(lines[start + 1 : end])
    if len(values) != 1:
        raise ScenarioLoreError(
            f"{source_name} must contain exactly one UUID under {heading}."
        )
    return _parse_uuid(values[0], heading, source_name)


def _parse_init_text(text, source_name="init.txt"):
    lines = text.splitlines()
    headings = (
        "STARTING LOCATION:",
        "MAIN QUEST:",
        "OPENING:",
        "KNOWN ENTITIES:",
        "DM_ONLY:",
    )
    start_index, quest_index, opening_index, known_index, dm_index = (
        _section_positions(lines, headings, source_name)
    )
    if _nonempty_lines(lines[:start_index]):
        raise ScenarioLoreError(
            f"{source_name} cannot contain content before STARTING LOCATION:."
        )
    starting_location_uuid = _single_uuid_section(
        lines,
        start_index,
        quest_index,
        "STARTING LOCATION",
        source_name,
    )
    main_quest_uuid = _single_uuid_section(
        lines,
        quest_index,
        opening_index,
        "MAIN QUEST",
        source_name,
    )
    opening = "\n".join(lines[opening_index + 1 : known_index]).strip()
    if not opening:
        raise ScenarioLoreError(f"{source_name} has an empty OPENING section.")
    known_entity_uuids = tuple(
        _parse_uuid(value, "KNOWN ENTITIES UUID", source_name)
        for value in _nonempty_lines(lines[known_index + 1 : dm_index])
    )
    if len(known_entity_uuids) != len(set(known_entity_uuids)):
        raise ScenarioLoreError(f"{source_name} contains duplicate known entities.")
    dm_only = "\n".join(lines[dm_index + 1 :]).strip()
    return ScenarioInitialization(
        starting_location_uuid=starting_location_uuid,
        main_quest_uuid=main_quest_uuid,
        opening=opening,
        known_entity_uuids=known_entity_uuids,
        dm_only=dm_only,
    )


def parse_init_file(path):
    path = Path(path)
    return _parse_init_text(path.read_text(encoding="utf-8"), path.name)


def render_init_file(
    *,
    starting_location_uuid,
    main_quest_uuid,
    opening,
    known_entity_uuids=(),
    dm_only="",
):
    starting_location_uuid = _parse_uuid(
        str(starting_location_uuid),
        "STARTING LOCATION",
        "init.txt",
    )
    main_quest_uuid = _parse_uuid(
        str(main_quest_uuid),
        "MAIN QUEST",
        "init.txt",
    )
    known_entity_uuids = tuple(
        _parse_uuid(str(value), "KNOWN ENTITIES UUID", "init.txt")
        for value in known_entity_uuids
    )
    if len(known_entity_uuids) != len(set(known_entity_uuids)):
        raise ScenarioLoreError("init.txt contains duplicate known entities.")
    if not opening.strip():
        raise ScenarioLoreError("init.txt requires opening text.")
    known_entities = "\n".join(str(value) for value in known_entity_uuids)
    return (
        "STARTING LOCATION:\n\n"
        f"{starting_location_uuid}\n\n"
        "MAIN QUEST:\n\n"
        f"{main_quest_uuid}\n\n"
        "OPENING:\n\n"
        f"{opening.strip()}\n\n"
        "KNOWN ENTITIES:\n\n"
        f"{known_entities}\n\n"
        "DM_ONLY:\n\n"
        f"{dm_only.strip()}\n"
    )


def create_init_file(
    *,
    release_dir,
    starting_location_uuid,
    main_quest_uuid,
    opening,
    known_entity_uuids=(),
    dm_only="",
):
    target = Path(release_dir) / "init.txt"
    if target.exists():
        raise ScenarioLoreError(f"Initialization file already exists: {target}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_init_file(
            starting_location_uuid=starting_location_uuid,
            main_quest_uuid=main_quest_uuid,
            opening=opening,
            known_entity_uuids=known_entity_uuids,
            dm_only=dm_only,
        ),
        encoding="utf-8",
    )
    return target


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
        (Visibility.PUBLIC_INFO, definition.public_info, "Public"),
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
                    content=f"{definition.name}\n{section}\n\n{body}",
                )
            )
            chunk_number += 1
    return tuple(chunks)


def _relationship_targets(definition, relation_name):
    return tuple(
        relationship
        for relationship in definition.relationships
        if relationship.relation == relation_name
    )


def _validate_location_cycles(definitions_by_uuid):
    parents = {}
    for definition in definitions_by_uuid.values():
        if definition.definition_type != "location":
            continue
        contained_in = _relationship_targets(definition, "contained_in")
        if contained_in:
            parents[definition.definition_uuid] = contained_in[0].target_uuid

    for location_uuid in parents:
        visited = set()
        current = location_uuid
        while current in parents:
            if current in visited:
                raise ScenarioLoreError(
                    "Location containment relationships form a cycle."
                )
            visited.add(current)
            current = parents[current]


def _validate_release(release):
    definitions_by_uuid = {}
    match_sources = {}
    for definition in release.definitions:
        previous = definitions_by_uuid.get(definition.definition_uuid)
        if previous:
            raise ScenarioLoreError(
                f"Definition UUID {definition.definition_uuid} is used by both "
                f"{previous.source_file} and {definition.source_file}."
            )
        definitions_by_uuid[definition.definition_uuid] = definition
        previous_match = match_sources.get(definition.match_text)
        if previous_match:
            raise ScenarioLoreError(
                f"{definition.source_file} exactly duplicates {previous_match}."
            )
        match_sources[definition.match_text] = definition.source_file

    starting_location = definitions_by_uuid.get(
        release.initialization.starting_location_uuid
    )
    if not starting_location or starting_location.definition_type != "location":
        raise ScenarioLoreError(
            "init.txt STARTING LOCATION must reference a location in this release."
        )
    main_quest = definitions_by_uuid.get(release.initialization.main_quest_uuid)
    if not main_quest or main_quest.definition_type != "quest":
        raise ScenarioLoreError(
            "init.txt MAIN QUEST must reference a quest in this release."
        )
    for known_uuid in release.initialization.known_entity_uuids:
        if known_uuid not in definitions_by_uuid:
            raise ScenarioLoreError(
                f"init.txt references unknown known entity {known_uuid}."
            )

    for definition in release.definitions:
        for relationship in definition.relationships:
            target = definitions_by_uuid.get(relationship.target_uuid)
            if not target or target.definition_type != relationship.target_type:
                raise ScenarioLoreError(
                    f"{definition.source_file} references unknown "
                    f"{relationship.target_type} {relationship.target_uuid}."
                )
        if definition.definition_type == "location":
            contained_in = _relationship_targets(definition, "contained_in")
            if len(contained_in) > 1 or any(
                relationship.target_type != "location"
                for relationship in contained_in
            ):
                raise ScenarioLoreError(
                    f"{definition.source_file} may contain at most one "
                    "contained_in location relationship."
                )
        if definition.definition_type == "npc":
            located_in = _relationship_targets(definition, "located_in")
            if len(located_in) > 1 or any(
                relationship.target_type != "location" for relationship in located_in
            ):
                raise ScenarioLoreError(
                    f"{definition.source_file} may contain at most one "
                    "located_in location relationship."
                )

    _validate_location_cycles(definitions_by_uuid)
    return release


def load_scenario_release(
    scenario_dir=None,
    scenario_key=None,
    version=None,
    file_text_overrides=None,
):
    scenario_key = scenario_key or settings.SCENARIO_KEY
    version, release_dir = _release_directory(
        scenario_dir,
        scenario_key,
        version=version,
    )
    file_text_overrides = {
        Path(path): text for path, text in (file_text_overrides or {}).items()
    }
    init_path = release_dir / "init.txt"
    if not init_path.is_file():
        raise ScenarioLoreError(
            f"Scenario {scenario_key!r} v{version} is missing init.txt."
        )
    init_text = file_text_overrides.get(
        init_path,
        init_path.read_text(encoding="utf-8"),
    )
    initialization = _parse_init_text(init_text)
    definitions = []
    for folder_name, definition_type in ENTITY_FOLDERS.items():
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
        for path in files:
            definition = _parse_definition_text(
                path,
                file_text_overrides.get(
                    path,
                    path.read_text(encoding="utf-8"),
                ),
                version,
                release_dir,
            )
            if definition.definition_type != definition_type:
                raise ScenarioLoreError(
                    f"{definition.source_file} has the wrong entity type."
                )
            definitions.append(definition)

    lore_chunks = tuple(
        chunk
        for definition in definitions
        if definition.definition_type == "world_lore"
        for chunk in _lore_chunks(definition)
    )
    return _validate_release(
        ScenarioRelease(
            scenario_key=scenario_key,
            version=version,
            release_dir=release_dir,
            initialization=initialization,
            definitions=tuple(definitions),
            lore_chunks=lore_chunks,
        )
    )


def validate_scenario_release(scenario_dir=None, scenario_key=None, version=None):
    return load_scenario_release(
        scenario_dir=scenario_dir,
        scenario_key=scenario_key,
        version=version,
    )


def load_scenario_chunks(scenario_dir=None, scenario_key=None, version=None):
    return list(
        load_scenario_release(
            scenario_dir=scenario_dir,
            scenario_key=scenario_key,
            version=version,
        ).lore_chunks
    )


def grouped_known_entities(release):
    definitions_by_uuid = release.definition_by_uuid()
    grouped = {
        "locations": [],
        "npcs": [],
        "quests": [],
        "world_lore": [],
    }
    for definition_uuid in release.initialization.known_entity_uuids:
        definition = definitions_by_uuid[definition_uuid]
        grouped[KNOWN_ENTITY_KEYS[definition.definition_type]].append(
            str(definition_uuid)
        )
    return grouped


def _render_relationships(relationships):
    return "\n".join(
        f"{relationship.relation} | {relationship.target_type} | "
        f"{relationship.target_uuid}"
        for relationship in relationships
    )


def render_definition_file(
    *,
    definition_type,
    name,
    initial_status=None,
    relationships=(),
    public_info="",
    dm_only="",
    definition_uuid=UUID_PLACEHOLDER,
):
    if definition_type not in ENTITY_TYPES:
        raise ScenarioLoreError(f"Unsupported entity type {definition_type!r}.")
    if definition_type in INITIAL_STATUS_VALUES:
        if initial_status not in INITIAL_STATUS_VALUES[definition_type]:
            raise ScenarioLoreError(
                f"{definition_type} requires a valid initial status."
            )
    elif initial_status is not None:
        raise ScenarioLoreError("World lore does not have an initial status.")
    if isinstance(definition_uuid, UUID):
        definition_uuid = str(definition_uuid)
    header = [f"UUID: {definition_uuid}", f"NAME: {name}"]
    if initial_status is not None:
        header.append(f"INITIAL STATUS: {initial_status}")
    return (
        "\n".join(header)
        + "\n\nRELATIONSHIPS:\n\n"
        + _render_relationships(tuple(relationships))
        + "\n\nPUBLIC:\n\n"
        + public_info.strip()
        + "\n\nDM_ONLY:\n\n"
        + dm_only.strip()
        + "\n"
    )


def create_definition_file(
    *,
    release_dir,
    definition_type,
    filename,
    name,
    initial_status=None,
    relationships=(),
    public_info="",
    dm_only="",
):
    release_dir = Path(release_dir)
    folder_name = next(
        folder
        for folder, candidate_type in ENTITY_FOLDERS.items()
        if candidate_type == definition_type
    )
    if Path(filename).name != filename or not filename.endswith(".txt"):
        raise ScenarioLoreError("Definition filename must be a plain .txt filename.")
    target = release_dir / folder_name / filename
    if target.exists():
        raise ScenarioLoreError(f"Definition file already exists: {target}.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_definition_file(
            definition_type=definition_type,
            name=name,
            initial_status=initial_status,
            relationships=relationships,
            public_info=public_info,
            dm_only=dm_only,
        ),
        encoding="utf-8",
    )
    return target


def _definition_uuid_value(text, source_name, allow_placeholder=False):
    lines = text.splitlines()
    if not lines:
        raise ScenarioLoreError(f"{source_name} is empty.")
    match = UUID_LINE_PATTERN.fullmatch(lines[0])
    if match is None:
        raise ScenarioLoreError(
            f"{source_name} must begin with 'UUID: <uuid>'."
        )
    value = match.group("uuid")
    if value == UUID_PLACEHOLDER:
        if allow_placeholder:
            return value
        raise ScenarioLoreError(
            f"{source_name} still contains UUID: {UUID_PLACEHOLDER}."
        )
    return _parse_uuid(value, "UUID", source_name)


def _replace_uuid_line(text, definition_uuid):
    lines = text.splitlines(keepends=True)
    line_ending = "\n"
    if lines and lines[0].endswith("\r\n"):
        line_ending = "\r\n"
    lines[0] = f"UUID: {definition_uuid}{line_ending}"
    return "".join(lines)


def populate_scenario_uuids(
    scenario_dir=None,
    scenario_key=None,
    version=None,
):
    scenario_key = scenario_key or settings.SCENARIO_KEY
    version, release_dir = _release_directory(
        scenario_dir,
        scenario_key,
        version=version,
    )
    previous_releases = [
        (previous_version, previous_dir)
        for previous_version, previous_dir in _release_directories(
            scenario_dir,
            scenario_key,
        )
        if previous_version < version
    ]
    prior_matches = {definition_type: {} for definition_type in ENTITY_TYPES}
    prior_uuid_types = {}
    for previous_version, previous_dir in previous_releases:
        for folder_name, definition_type in ENTITY_FOLDERS.items():
            for path in sorted((previous_dir / folder_name).glob("*.txt")):
                text = path.read_text(encoding="utf-8")
                definition_uuid = _definition_uuid_value(text, path.name)
                previous_type = prior_uuid_types.get(definition_uuid)
                if previous_type and previous_type != definition_type:
                    raise ScenarioLoreError(
                        f"UUID {definition_uuid} changes entity type in v"
                        f"{previous_version}."
                    )
                prior_uuid_types[definition_uuid] = definition_type
                first_line_end = text.find("\n")
                match_text = "" if first_line_end < 0 else text[first_line_end + 1 :]
                prior_matches[definition_type].setdefault(match_text, set()).add(
                    definition_uuid
                )

    overrides = {}
    assigned = {}
    current_match_sources = {}
    for folder_name, definition_type in ENTITY_FOLDERS.items():
        folder = release_dir / folder_name
        if not folder.is_dir():
            raise ScenarioLoreError(
                f"Scenario {scenario_key!r} v{version} is missing {folder_name}/."
            )
        for path in sorted(folder.glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            uuid_value = _definition_uuid_value(
                text,
                path.name,
                allow_placeholder=True,
            )
            first_line_end = text.find("\n")
            match_text = "" if first_line_end < 0 else text[first_line_end + 1 :]
            duplicate = current_match_sources.get(match_text)
            if duplicate:
                raise ScenarioLoreError(
                    f"{path.relative_to(release_dir)} exactly duplicates {duplicate}."
                )
            current_match_sources[match_text] = path.relative_to(release_dir)

            if uuid_value == UUID_PLACEHOLDER:
                candidates = prior_matches[definition_type].get(match_text, set())
                if len(candidates) > 1:
                    raise ScenarioLoreError(
                        f"{path.name} matches earlier definitions with conflicting "
                        "UUIDs."
                    )
                definition_uuid = next(iter(candidates)) if candidates else uuid4()
                overrides[path] = _replace_uuid_line(text, definition_uuid)
            else:
                definition_uuid = uuid_value
            previous_path = assigned.get(definition_uuid)
            if previous_path:
                raise ScenarioLoreError(
                    f"UUID {definition_uuid} is used by both {previous_path} and "
                    f"{path.relative_to(release_dir)}."
                )
            assigned[definition_uuid] = path.relative_to(release_dir)
            previous_type = prior_uuid_types.get(definition_uuid)
            if previous_type and previous_type != definition_type:
                raise ScenarioLoreError(
                    f"UUID {definition_uuid} changes from {previous_type} to "
                    f"{definition_type}."
                )

    load_scenario_release(
        scenario_dir=scenario_dir,
        scenario_key=scenario_key,
        version=version,
        file_text_overrides=overrides,
    )
    for path, text in overrides.items():
        path.write_text(text, encoding="utf-8")
    return {
        path.relative_to(release_dir).as_posix(): str(
            _definition_uuid_value(text, path.name)
        )
        for path, text in overrides.items()
    }


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


def _existing_template(model, release, definition_uuid, chunk_number=None):
    lookup = {
        "scenario_key": release.scenario_key,
        "version": release.version,
        "definition_uuid": definition_uuid,
    }
    if chunk_number is not None:
        lookup["chunk_number"] = chunk_number
    return model.objects.filter(**lookup).first()


def _compare_immutable(template, values, source_file):
    for field_name, value in values.items():
        current = getattr(template, field_name)
        current_id_name = f"{field_name}_id"
        if hasattr(template, current_id_name):
            current = getattr(template, current_id_name)
            value = value.id if value else None
        if current != value:
            raise ScenarioLoreError(
                "Scenario release content changed without a version increase: "
                f"{source_file}."
            )


def _relationship_template_targets(relationship, templates_by_definition):
    targets = templates_by_definition.get(
        (relationship.target_type, relationship.target_uuid),
        (),
    )
    if not targets:
        raise ScenarioLoreError(
            f"Unable to resolve relationship target {relationship.target_uuid}."
        )
    return targets


def _resolved_template_relationships(definition, templates_by_definition):
    relationships = []
    for relationship in definition.relationships:
        for target in _relationship_template_targets(
            relationship,
            templates_by_definition,
        ):
            relationships.append(
                {
                    "relation": relationship.relation,
                    "target": {
                        "type": relationship.target_type,
                        "id": target.id,
                    },
                }
            )
    return relationships


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
    embeddings = embed_scenario_inputs(
        build_scenario_embedding_inputs(release),
        client=client,
        batch_size=batch_size,
    )
    activated_ids = {model: [] for model in TEMPLATE_MODELS.values()}
    templates_by_definition = {}
    created_templates = set()

    with transaction.atomic():
        _verify_uuid_history(release)

        for definition in release.definitions_of_type("location"):
            template = _existing_template(
                LocationTemplate,
                release,
                definition.definition_uuid,
            )
            if template is None:
                template = LocationTemplate.objects.create(
                    definition_uuid=definition.definition_uuid,
                    scenario_key=release.scenario_key,
                    version=release.version,
                    active=False,
                    source_file=definition.source_file,
                    name=definition.name,
                    initial_status=definition.initial_status,
                    definition_json=_definition_state(definition),
                    public_embedding=embeddings.get(
                        _embedding_key(definition, Visibility.PUBLIC_INFO)
                    ),
                    dm_embedding=embeddings.get(
                        _embedding_key(definition, Visibility.DM_ONLY)
                    ),
                )
                created_templates.add((LocationTemplate, template.id))
            templates_by_definition[("location", definition.definition_uuid)] = (
                template,
            )
            activated_ids[LocationTemplate].append(template.id)

        for definition in release.definitions_of_type("npc"):
            template = _existing_template(
                NPCTemplate,
                release,
                definition.definition_uuid,
            )
            if template is None:
                template = NPCTemplate.objects.create(
                    definition_uuid=definition.definition_uuid,
                    scenario_key=release.scenario_key,
                    version=release.version,
                    active=False,
                    source_file=definition.source_file,
                    name=definition.name,
                    initial_status=definition.initial_status,
                    definition_json=_definition_state(definition),
                    public_embedding=embeddings.get(
                        _embedding_key(definition, Visibility.PUBLIC_INFO)
                    ),
                    dm_embedding=embeddings.get(
                        _embedding_key(definition, Visibility.DM_ONLY)
                    ),
                )
                created_templates.add((NPCTemplate, template.id))
            templates_by_definition[("npc", definition.definition_uuid)] = (
                template,
            )
            activated_ids[NPCTemplate].append(template.id)

        for definition in release.definitions_of_type("quest"):
            template = _existing_template(
                QuestTemplate,
                release,
                definition.definition_uuid,
            )
            if template is None:
                template = QuestTemplate.objects.create(
                    definition_uuid=definition.definition_uuid,
                    scenario_key=release.scenario_key,
                    version=release.version,
                    active=False,
                    source_file=definition.source_file,
                    title=definition.name,
                    initial_status=definition.initial_status,
                    definition_json=_definition_state(definition),
                    public_embedding=embeddings.get(
                        _embedding_key(definition, Visibility.PUBLIC_INFO)
                    ),
                    dm_embedding=embeddings.get(
                        _embedding_key(definition, Visibility.DM_ONLY)
                    ),
                )
                created_templates.add((QuestTemplate, template.id))
            templates_by_definition[("quest", definition.definition_uuid)] = (
                template,
            )
            activated_ids[QuestTemplate].append(template.id)

        lore_definitions = {
            definition.definition_uuid: definition
            for definition in release.definitions_of_type("world_lore")
        }
        lore_templates_by_uuid = {}
        for chunk in release.lore_chunks:
            definition = lore_definitions[chunk.definition_uuid]
            template = _existing_template(
                WorldLoreChunkTemplate,
                release,
                chunk.definition_uuid,
                chunk_number=chunk.chunk_number,
            )
            if template is None:
                embedding_key = (
                    f"world_lore:{chunk.definition_uuid}:"
                    f"{chunk.chunk_number}:{chunk.visibility}"
                )
                template = WorldLoreChunkTemplate.objects.create(
                    definition_uuid=chunk.definition_uuid,
                    scenario_key=release.scenario_key,
                    version=release.version,
                    active=False,
                    source_file=chunk.source_file,
                    title=chunk.title,
                    section=chunk.section,
                    chunk_number=chunk.chunk_number,
                    visibility=chunk.visibility,
                    content=chunk.content,
                    embedding=embeddings[embedding_key],
                )
                created_templates.add((WorldLoreChunkTemplate, template.id))
            lore_templates_by_uuid.setdefault(chunk.definition_uuid, []).append(
                template
            )
            activated_ids[WorldLoreChunkTemplate].append(template.id)
        for definition_uuid, templates in lore_templates_by_uuid.items():
            templates_by_definition[("world_lore", definition_uuid)] = tuple(
                templates
            )

        for definition in release.definitions:
            templates = templates_by_definition[
                (definition.definition_type, definition.definition_uuid)
            ]
            relationships = _resolved_template_relationships(
                definition,
                templates_by_definition,
            )
            parent_template = None
            initial_location_template = None
            if definition.definition_type == "location":
                contained_in = _relationship_targets(definition, "contained_in")
                if contained_in:
                    parent_template = _relationship_template_targets(
                        contained_in[0],
                        templates_by_definition,
                    )[0]
            if definition.definition_type == "npc":
                located_in = _relationship_targets(definition, "located_in")
                if located_in:
                    initial_location_template = _relationship_template_targets(
                        located_in[0],
                        templates_by_definition,
                    )[0]

            for template in templates:
                if definition.definition_type == "location":
                    values = {
                        "source_file": definition.source_file,
                        "name": definition.name,
                        "parent_template": parent_template,
                        "initial_status": definition.initial_status,
                        "definition_json": _definition_state(definition),
                        "relationships_json": relationships,
                        "metadata_json": {},
                    }
                elif definition.definition_type == "npc":
                    values = {
                        "source_file": definition.source_file,
                        "name": definition.name,
                        "initial_location_template": initial_location_template,
                        "initial_status": definition.initial_status,
                        "definition_json": _definition_state(definition),
                        "relationships_json": relationships,
                        "metadata_json": {},
                    }
                elif definition.definition_type == "quest":
                    values = {
                        "source_file": definition.source_file,
                        "title": definition.name,
                        "initial_status": definition.initial_status,
                        "definition_json": _definition_state(definition),
                        "relationships_json": relationships,
                        "metadata_json": {},
                    }
                else:
                    chunk = next(
                        candidate
                        for candidate in release.lore_chunks
                        if candidate.definition_uuid == definition.definition_uuid
                        and candidate.chunk_number == template.chunk_number
                    )
                    values = {
                        "source_file": chunk.source_file,
                        "title": chunk.title,
                        "section": chunk.section,
                        "visibility": chunk.visibility,
                        "content": chunk.content,
                        "relationships_json": relationships,
                        "metadata_json": {},
                    }
                if (type(template), template.id) in created_templates:
                    for field_name, value in values.items():
                        setattr(template, field_name, value)
                    template.save(update_fields=list(values))
                else:
                    _compare_immutable(template, values, definition.source_file)

        for model, ids in activated_ids.items():
            model.objects.filter(scenario_key=release.scenario_key).update(active=False)
            model.objects.filter(id__in=ids).update(active=True)

    return release.version, sum(len(ids) for ids in activated_ids.values())


def sync_world_lore_chunk_templates(*args, **kwargs):
    return sync_scenario_templates(*args, **kwargs)
