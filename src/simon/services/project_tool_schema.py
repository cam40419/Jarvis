from typing import Any

from pydantic import BaseModel

from simon.domain.project_files import (
    ProjectBind,
    ProjectCreate,
    ProjectFile,
    ProjectFileCreate,
    ProjectFileEdit,
    ProjectFileRename,
    ProjectFiles,
    ProjectSelect,
    ProjectSheetRead,
    ProjectSheetWrite,
    ProjectTrash,
    ProjectUnlink,
)

MODELS: dict[str, type[BaseModel]] = {
    "project_unlink_drive": ProjectUnlink,
    "project_drive_trash": ProjectTrash,
    "project_create": ProjectCreate,
    "project_link_drive": ProjectBind,
    "project_sync": ProjectSelect,
    "project_files_list": ProjectFiles,
    "project_file_read": ProjectFile,
    "project_file_create": ProjectFileCreate,
    "project_file_edit": ProjectFileEdit,
    "project_sheet_read": ProjectSheetRead,
    "project_sheet_write": ProjectSheetWrite,
    "project_file_rename": ProjectFileRename,
}
READS = {"project_list", "project_files_list", "project_file_read", "project_sheet_read"}
DESCRIPTIONS = {
    "project_unlink_drive": (
        "Remove the project's Drive association and stop automatic Drive sync. Preserves all files "
        "and the local project directory. Does not delete the project or Drive folder. "
        "Use project_list's current binding version. Relink later to resume."
    ),
    "project_drive_trash": (
        "Move an explicitly user-selected Drive file/folder to trash, "
        "including an old project folder. "
        "Folder contents are also trashed. Use drive_list_folder or project_files_list to resolve "
        "the exact item and its version as revision. Requires a project_id for "
        "the operation receipt. "
        "Account empty means the project's bound account; otherwise exact connected email/ID. "
        "Relink or unlink first if trashing the active project folder or an ancestor. "
        "Never trash My Drive itself, infer deletion from unlink/relink, or "
        "retry unknown outcomes. "
        "For an unambiguous user deletion request execute directly; no extra confirmation."
    ),
    "project_list": (
        "List Work projects and Drive state/version. Local folders use root project:<id>."
    ),
    "project_create": (
        "Create a Work project and automatically provision its Drive folder. "
        "Set account to a connected email or ID, or empty string for the default."
    ),
    "project_link_drive": (
        "Link a user-selected existing Drive folder using current binding version. "
        "Set account to its connected email or ID, or empty string for the default. "
        "Use folder_id=root for My Drive itself. Browse/find named folders with drive_list_folder; "
        "do not ask the user to supply IDs. Relinking does not move existing files."
    ),
    "project_sync": ("Provision the project folder and upload pending task outputs now."),
    "project_files_list": (
        "List live project folder files; null folder_id means project root. Page with "
        "next_page_token."
    ),
    "project_file_read": (
        "Read live text/code or Google Docs with revision and tab IDs; inspect other file metadata."
    ),
    "project_file_create": (
        "Create text/code, Google Doc, Google Sheet (CSV content), or folder in the "
        "project. No confirmation needed."
    ),
    "project_file_edit": (
        "Edit a file using its freshly read revision. Replace exactly one old_text "
        "occurrence, or append with empty old_text. Use tab_id for Docs. No "
        "confirmation needed."
    ),
    "project_sheet_read": (
        "Read a bounded A1 cell range and its revision, at most 100 rows / 2000 cells."
    ),
    "project_sheet_write": (
        "Write literal values in an explicit bounded A1 range using its last read "
        "revision. Concurrent changes are checked before writing. No confirmation "
        "needed."
    ),
    "project_file_rename": (
        "Rename using the current Drive version from file listing as revision."
    ),
}


def project_schema(name: str) -> tuple[str, dict[str, Any]]:
    properties = MODELS[name].model_json_schema()["properties"] if name in MODELS else {}
    properties.pop("idempotency_key", None)
    for value in properties.values():
        value.pop("default", None)
        value.pop("title", None)
    return DESCRIPTIONS[name], properties
