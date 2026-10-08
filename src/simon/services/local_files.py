"""Account-scoped local files and bounded ZIP operations; never executes file contents."""

import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError as PydanticError

from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.models import ActorContext, utc_now
from simon.services.canonical import digest
from simon.services.identity import ROLE_SCOPES
from simon.services.local_tool_schema import MODELS, READS
from simon.services.safe_files import open_regular_nofollow

if TYPE_CHECKING:
    from simon.services.connected import ConnectedService

MAX_FILE = 50 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MAX_MEMBERS = 2000
PROTECTED = {
    ".ssh",
    ".aws",
    ".azure",
    ".codex",
    ".git",
    ".internal",
    "node_modules",
    "venv",
    ".venv",
}
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def parts(value: str) -> tuple[str, ...]:
    value = value.replace("\\", "/")
    if value.startswith("/") or ":" in value or "\x00" in value:
        raise ValidationError("Use a relative path inside one of the listed local roots.")
    result = tuple(value.split("/")) if value else ()
    for part in result:
        low = part.casefold()
        if (
            not part
            or part in {".", ".."}
            or part.endswith((" ", "."))
            or any(ord(char) < 32 or char in '<>"|?*' for char in part)
            or part.split(".")[0].upper() in RESERVED
        ):
            raise ValidationError("Invalid local path component.")
        if (
            low in PROTECTED
            or low == ".env"
            or low.startswith(".env.")
            or low.startswith("client_secret")
            or low.startswith(".simon-")
            or low in {"credentials.json", "token.json"}
            or low.endswith((".pem", ".key", ".pfx", ".p12"))
        ):
            raise AuthorizationError("Credential and application-internal files are excluded.")
    return result


def reject_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise AuthorizationError(
                "Symbolic links and junctions are not supported for local access."
            )


def host_roots() -> dict[str, Path]:
    roots = {name: Path.home() / name.title() for name in ("desktop", "documents", "downloads")}
    if os.name == "nt":
        import winreg

        names = {
            "desktop": "Desktop",
            "documents": "Personal",
            "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
        }
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                for name, value in names.items():
                    try:
                        location, _ = winreg.QueryValueEx(key, value)
                        roots[name] = Path(os.path.expandvars(location))
                    except OSError:
                        continue
        except OSError:
            pass
    return roots


class LocalFileService:
    def __init__(self, connected: "ConnectedService") -> None:
        self.connected = connected
        self.store = connected.store

    def authorize(self, actor: ActorContext, *, write: bool = False) -> None:
        if not self.connected.settings.local_files_enabled:
            raise AuthorizationError("Local file tools are disabled.")
        member = self.connected.identity.membership(actor.actor_id, actor.workspace_id)
        scope = "jobs:write" if write else "jobs:read"
        if scope not in actor.scopes or scope not in ROLE_SCOPES[member.role]:
            raise AuthorizationError("Local file access is unavailable.")

    def workspace(self, actor: ActorContext) -> Path:
        return (
            self.connected.settings.local_files_dir.absolute()
            / str(actor.workspace_id)
            / str(actor.actor_id)
        )

    def roots(self, actor: ActorContext) -> dict[str, Path]:
        self.authorize(actor)
        result = {"workspace": self.workspace(actor)}
        settings = self.connected.settings
        if actor.actor_id == (settings.local_files_actor_id or settings.account_admin_actor_id):
            configured = settings.local_file_roots or host_roots()
            result.update(
                {
                    name: path.absolute()
                    for name, path in configured.items()
                    if name != "workspace" and path.is_dir()
                }
            )
        return result

    def path(self, actor: ActorContext, root: str, value: str) -> Path:
        roots = self.roots(actor)
        if root in roots:
            base = roots[root]
        else:
            raise AuthorizationError("Unknown local root. Use local_files_roots first.")
        candidate = base.joinpath(*parts(value))
        reject_links(candidate)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(base.resolve()):
            raise AuthorizationError("Path is outside the selected folder.")
        # Host roots must never open Simon's credentials, runtime databases or source checkout.
        if root != "workspace":
            server = Path(__file__).resolve().parents[3]
            if resolved.is_relative_to(server) or resolved.is_relative_to(
                self.connected.settings.local_files_dir.resolve()
            ):
                raise AuthorizationError("Simon server files are excluded from host folder access.")
        return resolved

    @staticmethod
    def blob(path: Path, limit: int = MAX_FILE) -> bytes:
        reject_links(path)
        try:
            with open_regular_nofollow(path) as stream:
                before = os.fstat(stream.fileno())
                if before.st_size > limit:
                    raise ValidationError(f"File exceeds the {limit // 1024 // 1024} MB limit.")
                content = stream.read(limit + 1)
                after = os.fstat(stream.fileno())
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValidationError("File changed while reading. Read its latest revision.")
        except OSError:
            raise ValidationError("File unavailable. List the containing folder again.") from None
        if len(content) > limit:
            raise ValidationError(f"File exceeds the {limit // 1024 // 1024} MB limit.")
        return content

    def info(self, path: Path, root: str, value: str) -> dict[str, Any]:
        data = path.stat()
        return {
            "root": root,
            "path": value,
            "name": path.name,
            "kind": "folder" if path.is_dir() else "file",
            "bytes": data.st_size,
            "modified_at": data.st_mtime,
        }

    def listing(self, actor: ActorContext, request: Any) -> dict[str, Any]:
        parent = self.path(actor, request.root, request.path)
        if not parent.exists() and request.root == "workspace":
            return {"files": [], "next_offset": None}
        if not parent.is_dir():
            raise ValidationError("Folder not found.")
        entries = []
        for index, child in enumerate(parent.iterdir()):
            if index >= 20000:
                raise ValidationError("Folder exceeds 20,000 entries. Choose a smaller folder.")
            value = "/".join(filter(None, (request.path, child.name)))
            try:
                self.path(actor, request.root, value)
                entries.append(self.info(child, request.root, value))
            except (AuthorizationError, ValidationError, OSError):
                continue
        entries.sort(key=lambda item: (item["kind"] != "folder", item["name"].casefold()))
        end = request.offset + 100
        return {
            "files": entries[request.offset : end],
            "total": len(entries),
            "next_offset": end if end < len(entries) else None,
        }

    def search(self, actor: ActorContext, request: Any) -> dict[str, Any]:
        parent = self.path(actor, request.root, request.path)
        if not parent.is_dir():
            raise ValidationError("Folder not found.")
        results: list[dict[str, Any]] = []
        stack = [(parent, request.path)]
        count, started = 0, monotonic()
        while stack:
            directory, prefix = stack.pop()
            try:
                children = directory.iterdir()
                for child in children:
                    count += 1
                    if count > 20000 or monotonic() - started > 5 or len(results) >= 100:
                        return {"files": results, "truncated": True}
                    value = "/".join(filter(None, (prefix, child.name)))
                    try:
                        self.path(actor, request.root, value)
                        if request.query.casefold() in child.name.casefold():
                            results.append(self.info(child, request.root, value))
                        if child.is_dir():
                            stack.append((child, value))
                    except (AuthorizationError, ValidationError, OSError):
                        continue
            except OSError:
                continue
        return {"files": results, "truncated": False}

    def read(self, actor: ActorContext, request: Any) -> dict[str, Any]:
        path = self.path(actor, request.root, request.path)
        content = self.blob(path, 2 * 1024 * 1024)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError:
            raise ValidationError(
                "This is not UTF-8 text. ZIPs use local_zip_inspect/extract."
            ) from None
        if "\x00" in text:
            raise ValidationError("Binary content cannot be displayed as text.")
        end = request.offset + request.limit
        return {
            **self.info(path, request.root, request.path),
            "revision": revision(content),
            "text": text[request.offset : end],
            "characters": len(text),
            "next_offset": end if end < len(text) else None,
        }

    def publish(
        self, actor: ActorContext, root: str, value: str, content: bytes, expected: str = ""
    ) -> dict[str, Any]:
        path = self.path(actor, root, value)
        if not value or len(content) > MAX_FILE:
            raise ValidationError("Choose a file path and keep uploads under 50 MB.")
        if expected:
            if revision(self.blob(path)) != expected:
                raise ValidationError("File changed. Read the latest revision before editing.")
        elif path.exists():
            raise ValidationError(
                "Destination already exists. Read its revision before replacing it."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path(actor, root, value)
        descriptor, temporary = tempfile.mkstemp(prefix=".simon-", dir=path.parent)
        temp = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self.path(actor, root, value)
            if expected:
                if revision(self.blob(path)) != expected:
                    raise ValidationError("File changed before saving. Read it again.")
                # Preserve the exact prior bytes privately before replacing.
                backup = self.workspace(actor) / ".internal" / "versions" / expected
                reject_links(backup)
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    backup.write_bytes(self.blob(path))
                os.replace(temp, path)
            else:
                os.link(temp, path)  # Atomic create-only publication; no clobber.
        finally:
            temp.unlink(missing_ok=True)
        return {
            "status": "succeeded",
            "root": root,
            "path": value,
            "revision": revision(content),
            "bytes": len(content),
        }

    def members(self, archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValidationError("ZIP exceeds 2,000 entries.")
        total, seen = 0, set()
        for member in members:
            name = member.filename.rstrip("/")
            components = parts(name)
            if not components:
                raise ValidationError("ZIP contains an empty path.")
            key = "/".join(components).casefold()
            if key in seen:
                raise ValidationError("ZIP contains duplicate or case-colliding paths.")
            seen.add(key)
            mode = member.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1:
                raise ValidationError("Encrypted ZIPs, links and special entries are unsupported.")
            total += member.file_size
            if (
                total > MAX_EXPANDED
                or member.file_size > MAX_FILE
                or member.file_size > max(1024 * 1024, member.compress_size * 200)
            ):
                raise ValidationError("ZIP exceeds extraction size or compression-ratio limits.")
        file_names = {
            m.filename.rstrip("/").replace("\\", "/").casefold() for m in members if not m.is_dir()
        }
        for member in members:
            components = parts(member.filename.rstrip("/"))
            if any(
                "/".join(components[:i]).casefold() in file_names for i in range(1, len(components))
            ):
                raise ValidationError("ZIP contains conflicting file and directory paths.")
        return members

    def inspect(self, actor: ActorContext, request: Any) -> dict[str, Any]:
        content = self.blob(self.path(actor, request.root, request.path))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = self.members(archive)
            end = request.offset + 100
            return {
                "entries": [
                    {
                        "path": m.filename,
                        "bytes": m.file_size,
                        "kind": "folder" if m.is_dir() else "file",
                    }
                    for m in members[request.offset : end]
                ],
                "total": len(members),
                "expanded_bytes": sum(m.file_size for m in members),
                "next_offset": end if end < len(members) else None,
            }

    def extract(
        self, actor: ActorContext, request: Any, check: Callable[[], ActorContext]
    ) -> dict[str, Any]:
        content = self.blob(self.path(actor, request.root, request.path))
        destination = self.path(actor, request.destination_root, request.destination_path)
        if not request.destination_path or destination.exists():
            raise ValidationError("Extract into a new folder; the destination must not exist.")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = self.members(archive)  # Validate every member before writing anything.
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(prefix=".simon-unzip-", dir=destination.parent)
            ).resolve()
            try:
                total = 0
                for index, member in enumerate(members):
                    if index % 25 == 0:
                        check()
                    target = staging.joinpath(*parts(member.filename.rstrip("/")))
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("xb") as output:
                        size = 0
                        while block := source.read(1024 * 1024):
                            size += len(block)
                            total += len(block)
                            if size > MAX_FILE or total > MAX_EXPANDED:
                                raise ValidationError("ZIP expands beyond the extraction limit.")
                            output.write(block)
                check()
                self.path(actor, request.destination_root, request.destination_path)
                if destination.exists():
                    raise ValidationError("Destination appeared during extraction.")
                staging.rename(destination)
            finally:
                if (
                    staging.exists()
                    and staging.parent == destination.parent.resolve()
                    and staging.name.startswith(".simon-unzip-")
                ):
                    shutil.rmtree(staging)
        return {
            "status": "succeeded",
            "root": request.destination_root,
            "path": request.destination_path,
            "entries": len(members),
            "bytes": total,
        }

    def zip(
        self, actor: ActorContext, request: Any, check: Callable[[], ActorContext]
    ) -> dict[str, Any]:
        source = self.path(actor, request.root, request.path)
        if not source.exists():
            raise ValidationError("Source does not exist.")
        destination = self.path(actor, request.destination_root, request.destination_path)
        if source.is_dir() and destination.is_relative_to(source):
            raise ValidationError("Create the ZIP outside the source folder.")
        buffer = io.BytesIO()
        total, count = 0, 0
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            pending = [source]
            while pending:
                item = pending.pop()
                count += 1
                if count > MAX_MEMBERS:
                    raise ValidationError("Folder exceeds 2,000 ZIP entries.")
                check()
                relative = item.relative_to(source.parent if source.is_file() else source)
                value = (
                    "/".join(filter(None, (request.path, relative.as_posix())))
                    if source.is_dir()
                    else request.path
                )
                if item != source:
                    self.path(actor, request.root, value)
                if item.is_dir():
                    for child in item.iterdir():
                        pending.append(child)
                        if count + len(pending) > MAX_MEMBERS:
                            raise ValidationError("Folder exceeds 2,000 ZIP entries.")
                    if item != source:
                        archive.writestr(relative.as_posix() + "/", b"")
                else:
                    content = self.blob(item)
                    total += len(content)
                    if total > MAX_EXPANDED:
                        raise ValidationError("Source exceeds 100 MB uncompressed.")
                    archive.writestr(relative.as_posix(), content)
                if buffer.tell() > MAX_FILE:
                    raise ValidationError("ZIP exceeds 50 MB.")
        check()
        return self.publish(
            actor, request.destination_root, request.destination_path, buffer.getvalue()
        )

    def action(
        self,
        actor: ActorContext,
        name: str,
        request: Any,
        key: str,
        check: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        check()
        if name == "local_files_roots":
            return {
                "roots": [
                    {"root": root, "path": str(path)} for root, path in self.roots(actor).items()
                ],
                "note": "Files are on Simon's server.",
            }
        if name == "local_files_list":
            return self.listing(actor, request)
        if name == "local_files_search":
            return self.search(actor, request)
        if name == "local_file_read":
            return self.read(actor, request)
        if name == "local_zip_inspect":
            return self.inspect(actor, request)
        if name == "local_file_write":
            return self.publish(
                actor, request.root, request.path, request.content.encode(), request.revision
            )
        if name == "local_file_edit":
            path = self.path(actor, request.root, request.path)
            raw = self.blob(path, 2 * 1024 * 1024)
            if revision(raw) != request.revision:
                raise ValidationError("File changed. Read its latest revision.")
            text = raw.decode("utf-8-sig")
            if text.count(request.old_text) != 1:
                raise ValidationError(
                    "Old text must occur exactly once. Read a more specific passage."
                )
            output = text.replace(request.old_text, request.new_text, 1).encode("utf-8")
            if raw.startswith(b"\xef\xbb\xbf"):
                output = b"\xef\xbb\xbf" + output
            return self.publish(actor, request.root, request.path, output, request.revision)
        if name == "local_folder_create":
            path = self.path(actor, request.root, request.path)
            path.mkdir(parents=True, exist_ok=True)
            return {"status": "succeeded", "root": request.root, "path": request.path}
        if name == "local_file_move":
            source = self.path(actor, request.root, request.path)
            if revision(self.blob(source)) != request.revision:
                raise ValidationError("File changed. Read its latest revision.")
            target = self.path(actor, request.destination_root, request.destination_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(source, target)
            source.unlink()
            return {
                "status": "succeeded",
                "root": request.destination_root,
                "path": request.destination_path,
                "revision": request.revision,
            }
        if name == "local_zip_extract":
            return self.extract(actor, request, check)
        if name == "local_zip_create":
            return self.zip(actor, request, check)
        raise ValidationError("Unknown local file operation.")

    def run(
        self,
        actor: ActorContext,
        name: str,
        values: dict[str, Any],
        key: str,
        check: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        self.authorize(actor, write=name not in READS)
        if name != "local_files_roots" and name not in MODELS:
            raise ValidationError("Unknown local file operation.")
        try:
            request = MODELS[name].model_validate(values) if name in MODELS else None
        except PydanticError:
            raise ValidationError("Invalid local file arguments.") from None
        if name == "local_files_roots" and values:
            raise ValidationError("No arguments expected.")

        def checked() -> ActorContext:
            current = check()
            if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
                raise AuthorizationError("Local file access changed.")
            self.authorize(current, write=name not in READS)
            return current

        def operation() -> dict[str, Any]:
            current = checked()
            result = self.action(current, name, request, key, checked)
            checked()
            if name not in READS:
                self.connected.audit.record(
                    event_type="local_file.operation",
                    actor=current,
                    resource_type="local_file",
                    resource_id=name,
                    payload={"operation": name, "status": result.get("status", "unknown")},
                )
            return result

        try:
            checked()
            if name in READS or name in {"local_file_import_drive", "local_file_export_drive"}:
                return operation()
            # Serialize local mutations across API/worker processes and deduplicate tool retries.
            with self.store.transaction(actor.workspace_id):
                result, _ = self.store.execute_once(
                    f"local-files:{actor.workspace_id}:{actor.actor_id}",
                    key,
                    digest({"name": name, "values": values}),
                    operation,
                )
                checked()
                return result
        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError):
            raise ValidationError(
                "Invalid or unsupported ZIP archive; no extraction was published."
            ) from None
        except UnicodeError:
            raise ValidationError("File is not valid UTF-8 text.") from None
        except OSError:
            raise ValidationError(
                "Local file operation failed. Check the folder and destination before retrying."
            ) from None

    def execute(
        self,
        actor: ActorContext,
        run_id: UUID,
        name: str,
        arguments: str,
        revalidate: Callable[[], ActorContext],
    ) -> str:
        def check() -> ActorContext:
            current = revalidate()
            if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
                raise AuthorizationError("Local file access changed.")
            attempt = self.store.attempt(run_id)
            if (
                not attempt
                or attempt.status != "pending"
                or attempt.expires_at <= utc_now()
                or attempt.run.actor_id != actor.actor_id
                or attempt.workspace_id != actor.workspace_id
                or name not in attempt.run.capability_manifest
            ):
                raise AuthorizationError("Active local file request not found.")
            thread = self.connected.conversations.get(current, attempt.run.thread_id)
            if thread.visibility != "personal" or (name not in READS and attempt.run.parent_run_id):
                raise AuthorizationError(
                    "Use a fresh request in a private conversation for local edits."
                )
            if name not in self.connected.available(current):
                raise AuthorizationError("Local tool access changed.")
            return current

        values = json.loads(arguments)
        key = f"{run_id}:{name}:{digest(values)}"
        return json.dumps(self.run(check(), name, values, key, check), ensure_ascii=False)
