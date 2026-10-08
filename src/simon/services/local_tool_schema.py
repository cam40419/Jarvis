from typing import Any

from pydantic import BaseModel

from simon.domain.local_files import (
    LocalEdit,
    LocalExtract,
    LocalList,
    LocalMove,
    LocalPath,
    LocalRead,
    LocalSearch,
    LocalWrite,
    LocalZip,
)

MODELS: dict[str, type[BaseModel]] = {
    "local_files_list": LocalList,
    "local_files_search": LocalSearch,
    "local_file_read": LocalRead,
    "local_file_write": LocalWrite,
    "local_file_edit": LocalEdit,
    "local_file_move": LocalMove,
    "local_folder_create": LocalPath,
    "local_zip_inspect": LocalList,
    "local_zip_extract": LocalExtract,
    "local_zip_create": LocalZip,
}
READS = {
    "local_files_roots",
    "local_files_list",
    "local_files_search",
    "local_file_read",
    "local_zip_inspect",
}
DESCRIPTIONS = {
    "local_files_roots": (
        "List this account's accessible folders on Simon's server. Resolve roots first."
    ),
    "local_files_list": ("List files in a local folder, 100 at a time; use next_offset for more."),
    "local_files_search": (
        "Find files/folders by name recursively under a local root/path; bounded search."
    ),
    "local_file_read": (
        "Read a page of UTF-8 text/code from a local file, with revision for editing."
    ),
    "local_file_write": (
        "Create UTF-8 text/code, or replace after reading its revision. Empty revision "
        "means create only."
    ),
    "local_file_edit": (
        "Replace one exact old_text occurrence using a freshly read revision; preserves "
        "other content."
    ),
    "local_file_move": (
        "Move or rename a local file with its current revision. Never overwrites the destination."
    ),
    "local_folder_create": ("Create a local folder, including parent folders."),
    "local_zip_inspect": (
        "Inspect ZIP member names and sizes without extraction, paginated 100 members."
    ),
    "local_zip_extract": (
        "Extract a ZIP into a NEW local folder. Preserves nested paths; rejects "
        "unsafe/oversized archives. No confirmation card."
    ),
    "local_zip_create": (
        "Package a local file/folder into a NEW ZIP at destination_root/destination_path."
    ),
}


def local_schema(name: str) -> tuple[str, dict[str, Any]]:
    properties = MODELS[name].model_json_schema()["properties"] if name in MODELS else {}
    for value in properties.values():
        value.pop("default", None)
        value.pop("title", None)
    return DESCRIPTIONS[name], properties
