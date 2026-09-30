"""Bounded, immutable artifact publication on a trusted local filesystem.

The caller owns authorization; descriptors must come from an authorized saved run.
Paths are derived only from UUIDs, never the display name or model-supplied paths.
Publication renames a complete directory atomically, keeping bytes and metadata
together. Storage permissions must prevent untrusted processes modifying the root.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from simon.domain.artifacts import Artifact, ArtifactError
from simon.domain.models import utc_now

DEFAULT_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_MAX_METADATA_BYTES = 16384


class ArtifactStore:
    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES) -> None:
        if type(max_bytes) is not int or not 0 < max_bytes <= 10 * 1024 * 1024:
            raise ValueError("Artifact size limit must be between one byte and ten MiB")
        self.root = root.expanduser().absolute()
        self.max_bytes = max_bytes
        self._check_path(self.root)

    def _check_path(self, path: Path) -> None:
        if not path.is_relative_to(self.root):
            raise ArtifactError("Artifact path escapes the configured storage root")
        # lstat also catches Windows junctions/reparse points that is_symlink misses.
        for component in (*reversed(path.parents), path):
            try:
                info = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ArtifactError("Artifact storage cannot contain filesystem redirects")
        # Keep normalization lexical: resolving handles while another publisher
        # renames a directory can report its old staging name on Windows. Reparse
        # and symlink checks above reject redirects without that publication race.
        if Path(os.path.normpath(path)) != path:
            raise ArtifactError("Artifact path contains noncanonical components")

    def _directory(self, artifact: Artifact) -> Path:
        path = self.root.joinpath(
            str(artifact.workspace_id), str(artifact.actor_id), str(artifact.run_id),
            str(artifact.task_id), str(artifact.id),
        )
        self._check_path(path)
        return path

    @staticmethod
    def _identity(task_id: UUID, digest: str, name: str, media_type: str) -> UUID:
        return uuid5(task_id, f"{digest}\n{name}\n{media_type}")

    def _read_file(self, path: Path, limit: int) -> bytes:
        self._check_path(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ArtifactError("Artifact storage contains an invalid or oversized file")
                content = source.read(limit + 1)
                if len(content) > limit:
                    raise ArtifactError("Artifact file exceeds the configured size limit")
                self._check_path(path)
                return content
        except OSError:
            raise ArtifactError("Artifact storage could not be read") from None

    def _stored_metadata(self, directory: Path) -> Artifact:
        try:
            return Artifact.model_validate_json(
                self._read_file(directory / "metadata.json", _MAX_METADATA_BYTES)
            )
        except ValidationError:
            raise ArtifactError("Artifact metadata is invalid") from None

    def read(self, artifact: Artifact, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_bytes if max_bytes is None else max_bytes
        if type(limit) is not int or not 0 < limit <= self.max_bytes:
            raise ArtifactError("Artifact read limit exceeds the configured storage limit")
        if artifact.size > limit:
            raise ArtifactError("Artifact exceeds the requested read limit")
        if artifact.id != self._identity(
            artifact.task_id, artifact.sha256, artifact.name, artifact.media_type
        ):
            raise ArtifactError("Artifact identity does not match its metadata")
        directory = self._directory(artifact)
        stored = self._stored_metadata(directory)
        if stored != artifact:
            raise ArtifactError("Artifact metadata differs from the authorized reference")
        content = self._read_file(directory / "content", limit)
        if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ArtifactError("Artifact content failed its integrity check")
        return content

    def _write_file(self, path: Path, data: bytes) -> None:
        self._check_path(path)
        with path.open("xb") as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())

    def publish_text(
        self,
        *,
        workspace_id: UUID,
        actor_id: UUID,
        run_id: UUID,
        task_id: UUID,
        text: str,
        name: str = "answer.txt",
        media_type: str = "text/plain",
    ) -> Artifact:
        if not isinstance(text, str):
            raise ArtifactError("Artifact text must be valid UTF-8")
        if len(text) > self.max_bytes:
            raise ArtifactError("Artifact exceeds the configured size limit")
        try:
            content = text.encode("utf-8")
        except (AttributeError, UnicodeError):
            raise ArtifactError("Artifact text must be valid UTF-8") from None
        if len(content) > self.max_bytes:
            raise ArtifactError("Artifact exceeds the configured size limit")
        digest = hashlib.sha256(content).hexdigest()
        try:
            artifact = Artifact(
                id=self._identity(task_id, digest, name, media_type), workspace_id=workspace_id,
                actor_id=actor_id, run_id=run_id, task_id=task_id, name=name,
                media_type=media_type, size=len(content), sha256=digest, created_at=utc_now(),
            )
        except (ValidationError, TypeError, AttributeError):
            raise ArtifactError("Artifact metadata is invalid") from None
        directory = self._directory(artifact)
        staging = directory.parent / f".publishing-{uuid4()}"
        try:
            directory.parent.mkdir(parents=True, exist_ok=True)
            self._check_path(directory.parent)
            if directory.exists():
                existing = self._stored_metadata(directory)
                if existing.model_copy(update={"created_at": artifact.created_at}) != artifact:
                    raise ArtifactError("Existing artifact has different metadata")
                self.read(existing)
                return existing
            staging.mkdir(exist_ok=False)
            self._check_path(staging)
            self._write_file(staging / "content", content)
            self._write_file(staging / "metadata.json", artifact.model_dump_json().encode("utf-8"))
            try:
                staging.rename(directory)
            except OSError:
                # A concurrent identical publication may have won the atomic rename.
                if not directory.exists():
                    raise
                existing = self._stored_metadata(directory)
                if existing.model_copy(update={"created_at": artifact.created_at}) != artifact:
                    raise ArtifactError("Existing artifact has different metadata") from None
                self.read(existing)
                return existing
            self.read(artifact)
            return artifact
        except OSError:
            raise ArtifactError("Artifact storage could not be written") from None
        finally:
            # Remove only this call's known temporary files, never user directories.
            self._check_path(staging)
            if staging.exists():
                for filename in ("content", "metadata.json"):
                    temporary = staging / filename
                    self._check_path(temporary)
                    temporary.unlink(missing_ok=True)
                staging.rmdir()
