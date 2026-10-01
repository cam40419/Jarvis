"""Optional, billable OpenAI image/transcription tools for an owned Docker lease.

Credentials and model IDs come only from operator configuration. Network calls
are single-attempt; outputs use exclusive controller-owned workspace filenames.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import struct
import wave
from collections.abc import Callable, Mapping
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import Field
from pydantic import ValidationError as SchemaError

from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.execution import EnvironmentLease
from simon.domain.models import ActorContext, StrictModel
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)
from simon.services.local_files import parts, reject_links

IMAGE_ENDPOINT = "https://api.openai.com/v1/images/generations"
AUDIO_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_AUDIO_BYTES = 24_000_000
MAX_TRANSCRIPT_BYTES = 1_000_000
SCOPES = frozenset({"jobs:read", "jobs:write"})
CAPABILITIES = frozenset({"workspace.write"})


class GenerateImage(StrictModel):
    prompt: str = Field(min_length=1, max_length=4000)
    size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "low"


class TranscribeAudio(StrictModel):
    input: str = Field(min_length=1, max_length=255)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def generative_tool_definitions(
    *,
    image_model: str | None = None,
    transcription_model: str | None = None,
    credential_env: str = "SIMON_OPENAI_API_KEY",
    enabled: bool = False,
) -> tuple[ToolDefinition, ...]:
    """No model default is assumed. Operators must choose models with API access."""
    rows = (
        (
            "generative.image_generate",
            image_model,
            IMAGE_ENDPOINT,
            GenerateImage,
            "Generate one PNG in this task's workspace using a configured GPT Image model.",
        ),
        (
            "generative.audio_transcribe",
            transcription_model,
            AUDIO_ENDPOINT,
            TranscribeAudio,
            "Transcribe a root-level PCM WAV in this task's workspace, "
            "up to ten minutes and 24 MB.",
        ),
    )
    return tuple(
        ToolDefinition(
            id=identifier,
            description=description
            + (" This calls a paid external service. Tool charges are outside the model budget."),
            categories=frozenset({"image" if schema is GenerateImage else "audio"}),
            capabilities=frozenset(
                {
                    "image.generate" if schema is GenerateImage else "audio.transcribe",
                }
            ),
            transport="generative",
            input_schema=schema.model_json_schema(),
            enabled=enabled,
            configured=bool(model),
            required_scopes=SCOPES,
            environment_capabilities=CAPABILITIES,
            side_effect=True,
            action_policy="write",
            endpoint=endpoint,
            credential_env=credential_env,
            settings={"model": model or "", "network": True},
        )
        for identifier, model, endpoint, schema, description in rows
    )


def generative_configuration_reason(definition: ToolDefinition) -> str | None:
    expected = {
        "generative.image_generate": IMAGE_ENDPOINT,
        "generative.audio_transcribe": AUDIO_ENDPOINT,
    }.get(definition.id)
    if expected is None or definition.transport != "generative":
        return "Unknown generative tool"
    if definition.endpoint != expected:
        return "Generative tools require their fixed OpenAI endpoint"
    model = definition.settings.get("model")
    if (
        not isinstance(model, str)
        or not model.strip()
        or len(model) > 200
        or any(ord(char) < 33 or ord(char) > 126 for char in model)
    ):
        return "Configure an exact provider model ID"
    if not definition.credential_env:
        return "Configure a server credential environment variable"
    if (
        not definition.side_effect
        or definition.action_policy != "write"
        or definition.settings.get("network") is not True
        or not definition.required_scopes >= SCOPES
        or not definition.environment_capabilities >= CAPABILITIES
    ):
        return "Generative tools require write, network and workspace permissions"
    return None


def _open_nofollow(path: Path) -> int:
    """Open only the final regular leaf; mount ancestors are trusted and prechecked."""
    if os.name != "nt":
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("This platform cannot safely open an active workspace input")
        return os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0))
    # O_NOFOLLOW is unavailable on Windows. OPEN_REPARSE_POINT opens the link
    # itself, so its attributes can be rejected before any content is read.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError("Workspace input could not be opened")
    try:
        info = AttributeTag()
        if not kernel.GetFileInformationByHandleEx(
            handle,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise OSError("Workspace input attributes could not be read")
        if info.attributes & (0x400 | 0x10):
            raise OSError("Workspace input must not be redirected or a directory")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return descriptor


def _read_audio(workspace: Path, request: TranscribeAudio) -> bytes:
    names = parts(request.input)
    if len(names) != 1 or Path(names[0]).suffix.lower() != ".wav":
        raise ValidationError("Use a root-level WAV copied into this task's workspace")
    path = workspace / names[0]
    reject_links(path)
    try:
        with os.fdopen(_open_nofollow(path), "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_AUDIO_BYTES:
                raise ValidationError("Audio must be a regular WAV no larger than 24 MB")
            data = source.read(MAX_AUDIO_BYTES + 1)
            after = os.fstat(source.fileno())
        if len(data) > MAX_AUDIO_BYTES or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
        ) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValidationError("Audio changed during reading")
        reject_links(path)
    except OSError:
        raise ValidationError(
            "Audio input is unavailable or redirects outside the workspace",
        ) from None
    if request.expected_sha256 and hashlib.sha256(data).hexdigest() != request.expected_sha256:
        raise ValidationError("Audio changed; use the current imported file hash")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (
                audio.getcomptype() != "NONE"
                or audio.getnchannels() not in {1, 2}
                or audio.getsampwidth() not in {1, 2, 3, 4}
                or not 8000 <= audio.getframerate() <= 96000
                or not 0 < audio.getnframes() <= audio.getframerate() * 600
            ):
                raise ValidationError("Use uncompressed mono/stereo PCM WAV of up to ten minutes")
            expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
            if len(audio.readframes(audio.getnframes())) != expected:
                raise ValidationError("Audio WAV is truncated")
    except (wave.Error, EOFError):
        raise ValidationError("Audio must be a valid PCM WAV file") from None
    return data


class GenerativeToolTransport:
    def __init__(
        self,
        lease: EnvironmentLease,
        *,
        actor: ActorContext,
        run_id: UUID,
        revalidate: Callable[[], ActorContext],
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 120,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 300:
            raise ValueError("Generative timeout must be between zero and 300 seconds")
        self.lease, self.actor, self.run_id = lease.model_copy(deep=True), actor, run_id
        self.revalidate, self.timeout_seconds = revalidate, timeout_seconds
        self._transport = transport
        self._environ = os.environ if environ is None else environ

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        reason = generative_configuration_reason(definition)
        if reason:
            raise ToolCatalogError(reason)
        assignment = self.lease.plan.request
        if (
            context.actor_id != self.actor.actor_id
            or context.household_id != self.actor.household_id
            or context.household_id != assignment.workspace_id
            or context.run_id != self.run_id
            or context.agent_id != assignment.agent_id
            or context.authorized_action != "write"
            or definition.id not in context.allowed_tool_ids
            or not definition.required_scopes <= context.scopes
        ):
            raise AuthorizationError("Generative tool is outside this assignment's grant")
        if (
            not definition.enabled
            or not definition.configured
            or self.lease.status != "active"
            or self.lease.definition.kind != "docker"
            or not self.lease.definition.capabilities >= CAPABILITIES
            or not context.environment_capabilities >= CAPABILITIES
        ):
            raise ToolCatalogError(
                "Generative tools require an enabled, writable Docker assignment",
            )
        secret = self._environ.get(definition.credential_env or "", "").strip()
        if not secret:
            raise ToolCatalogError("Generative provider credential is unavailable")

        def check() -> None:
            current = self.revalidate()
            if (current.actor_id, current.household_id) != (
                self.actor.actor_id,
                self.actor.household_id,
            ) or not definition.required_scopes <= (
                current.scopes & self.actor.scopes & context.scopes
            ):
                raise AuthorizationError("Generative tool account permissions changed")

        check()
        workspace = self.lease.plan.workspace_path
        reject_links(workspace)
        try:
            request = (
                GenerateImage.model_validate(arguments)
                if definition.id == "generative.image_generate"
                else TranscribeAudio.model_validate(arguments)
            )
        except SchemaError:
            raise ValidationError("Invalid generative tool arguments") from None
        suffix = ".png" if isinstance(request, GenerateImage) else ".txt"
        destination = workspace / f"generated-{context.invocation_id}{suffix}"
        reject_links(destination)
        if destination.exists():
            raise ValidationError(
                "This invocation already has an output; inspect it before retrying",
            )
        model = str(definition.settings["model"])
        kwargs: dict[str, Any]
        if isinstance(request, GenerateImage):
            kwargs = {
                "json": {
                    "model": model,
                    "prompt": request.prompt,
                    "size": request.size,
                    "quality": request.quality,
                    "output_format": "png",
                    "n": 1,
                }
            }
            limit = ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 65536
        else:
            audio = _read_audio(workspace, request)
            kwargs = {
                "data": {"model": model, "response_format": "json"},
                "files": {"file": ("audio.wav", audio, "audio/wav")},
            }
            limit = MAX_TRANSCRIPT_BYTES + 65536
        check()  # A slow file read must not send audio after cancellation/access revocation.
        deadline = monotonic() + self.timeout_seconds
        headers = {
            "Authorization": f"Bearer {secret}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Client-Request-Id": str(context.invocation_id),
        }
        try:
            with (
                httpx.Client(
                    transport=self._transport,
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    definition.endpoint or "",
                    headers=headers,
                    **kwargs,
                ) as response,
            ):
                if not 200 <= response.status_code < 300:
                    raise ToolExecutionError(
                        "Generative provider rejected the request",
                        unknown=response.status_code not in {400, 401, 403, 404, 422, 429},
                    )
                if response.headers.get("Content-Type", "").partition(";")[0] != "application/json":
                    raise ValueError("Provider response was not JSON")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > limit or monotonic() > deadline:
                        raise ValueError("Provider response exceeded its bounds")
                result = json.loads(bytes(raw).decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("Provider result must be an object")
            if isinstance(request, GenerateImage):
                records = result.get("data")
                if not isinstance(records, list) or len(records) != 1:
                    raise ValueError("Expected exactly one generated image")
                encoded = records[0].get("b64_json") if isinstance(records[0], dict) else None
                if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                    raise ValueError("Generated image encoding is invalid or oversized")
                output = base64.b64decode(encoded, validate=True)
                dimensions = tuple(int(value) for value in request.size.split("x"))
                if (
                    not 24 <= len(output) <= MAX_IMAGE_BYTES
                    or not output.startswith(b"\x89PNG\r\n\x1a\n")
                    or output[12:16] != b"IHDR"
                    or struct.unpack(">II", output[16:24]) != dimensions
                ):
                    raise ValueError("Generated PNG does not match the requested size")
            else:
                text = result.get("text")
                if not isinstance(text, str) or len(text) > MAX_TRANSCRIPT_BYTES:
                    raise ValueError("Transcription text is invalid or oversized")
                output = text.encode("utf-8")
                if len(output) > MAX_TRANSCRIPT_BYTES:
                    raise ValueError("Transcription text exceeds byte limit")
            check()
            reject_links(destination)
            with destination.open("xb") as target:
                target.write(output)
                target.flush()
                os.fsync(target.fileno())
        except ToolExecutionError:
            raise
        except Exception:
            # A paid request may have succeeded before a response/auth/local I/O failure.
            # Never silently repeat it or claim a provider charge was avoided.
            raise ToolExecutionError(
                "Generative request outcome could not be completed; inspect before retrying",
                unknown=True,
            ) from None
        return {
            "path": destination.name,
            "size": len(output),
            "sha256": hashlib.sha256(output).hexdigest(),
            "media_type": "image/png" if isinstance(request, GenerateImage) else "text/plain",
            "provider": "openai",
            "model": model,
            "external_cost_usd": None,
            "cost_note": "Provider tool charges are separate from the run's model budget.",
            **(
                {
                    "text": output.decode("utf-8")[:24000],
                    "truncated": len(output.decode("utf-8")) > 24000,
                }
                if isinstance(request, TranscribeAudio)
                else {}
            ),
        }
