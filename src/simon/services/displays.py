"""Persistent, device-authenticated idle displays and slideshow assets."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO, Literal, cast
from uuid import UUID

from simon.domain.displays import (
    ConfigureDisplay,
    CreateDisplay,
    DisplayConfiguration,
    DisplayDevice,
    DisplayImage,
)
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, utc_now
from simon.services.conversations import ConversationService
from simon.services.identity import token_hash

MEDIA_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_IMAGE_BYTES = 15 * 1024 * 1024


class DisplayService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.manifest = self.data_dir / "displays.json"
        self.images_dir = self.data_dir / "images"
        self._lock = threading.RLock()

    def _load(self) -> dict[UUID, DisplayDevice]:
        if not self.manifest.exists():
            return {}
        try:
            values = json.loads(self.manifest.read_text(encoding="utf-8"))
            return {device.id: device for device in map(DisplayDevice.model_validate, values)}
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("display state is unreadable") from error

    def _save(self, devices: dict[UUID, DisplayDevice]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [d.model_dump(mode="json") for d in devices.values()],
            indent=2,
            sort_keys=True,
        )
        temporary = self.manifest.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.manifest)

    @staticmethod
    def _manage(actor: ActorContext) -> None:
        ConversationService.authorize(actor, "identity:manage")

    def create(self, actor: ActorContext, request: CreateDisplay) -> tuple[DisplayDevice, str]:
        self._manage(actor)
        token = secrets.token_urlsafe(48)
        device = DisplayDevice(
            household_id=actor.household_id,
            created_by=actor.actor_id,
            name=request.name.strip(),
            token_hash=token_hash(token),
        )
        with self._lock:
            devices = self._load()
            devices[device.id] = device
            self._save(devices)
        return device, token

    def list(self, actor: ActorContext) -> tuple[DisplayDevice, ...]:
        ConversationService.authorize(actor, "home:read")
        with self._lock:
            devices = self._load()
        return tuple(d for d in devices.values() if d.household_id == actor.household_id)

    def get(self, actor: ActorContext, display_id: UUID) -> DisplayDevice:
        ConversationService.authorize(actor, "home:read")
        with self._lock:
            device = self._load().get(display_id)
        if not device or device.household_id != actor.household_id:
            raise NotFoundError("display not found")
        return device

    def authenticate(self, display_id: UUID, token: str, *, touch: bool = False) -> DisplayDevice:
        with self._lock:
            devices = self._load()
            device = devices.get(display_id)
            if not device or not token or not hmac.compare_digest(
                device.token_hash.encode(), token_hash(token).encode()
            ):
                raise AuthorizationError("display token is invalid")
            if touch and (
                device.last_seen_at is None
                or device.last_seen_at < utc_now() - timedelta(seconds=30)
            ):
                device = device.model_copy(update={"last_seen_at": utc_now()})
                devices[display_id] = device
                self._save(devices)
            return device

    def configure(
        self, actor: ActorContext, display_id: UUID, change: ConfigureDisplay
    ) -> DisplayDevice:
        self._manage(actor)
        with self._lock:
            devices = self._load()
            current = devices.get(display_id)
            if not current or current.household_id != actor.household_id:
                raise NotFoundError("display not found")
            values = current.configuration.model_dump()
            values.update(change.model_dump(exclude_none=True, exclude={"display_id"}))
            configuration = DisplayConfiguration.model_validate(values)
            updated = current.model_copy(
                update={
                    "configuration": configuration,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            devices[display_id] = updated
            self._save(devices)
            return updated

    def add_image(
        self,
        actor: ActorContext,
        display_id: UUID,
        filename: str,
        media_type: str,
        stream: BinaryIO,
    ) -> DisplayImage:
        self._manage(actor)
        if media_type not in MEDIA_SIGNATURES:
            raise ValidationError("Use a JPEG, PNG, WebP, or GIF image.")
        content = stream.read(MAX_IMAGE_BYTES + 1)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ValidationError("Images must be between 1 byte and 15 MB.")
        signatures = MEDIA_SIGNATURES[media_type]
        if not any(content.startswith(signature) for signature in signatures):
            raise ValidationError("The image content does not match its media type.")
        if media_type == "image/webp" and content[8:12] != b"WEBP":
            raise ValidationError("The image content does not match its media type.")
        safe_name = Path(filename).name[:180] or "image" + EXTENSIONS[media_type]
        digest = hashlib.sha256(content).hexdigest()
        with self._lock:
            devices = self._load()
            current = devices.get(display_id)
            if not current or current.household_id != actor.household_id:
                raise NotFoundError("display not found")
            duplicate = next((image for image in current.images if image.sha256 == digest), None)
            if duplicate:
                return duplicate
            image_media_type = cast(
                "Literal['image/jpeg', 'image/png', 'image/webp', 'image/gif']", media_type
            )
            image = DisplayImage(
                filename=safe_name,
                media_type=image_media_type,
                byte_count=len(content),
                sha256=digest,
            )
            target_dir = self.images_dir / str(actor.household_id) / str(display_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / (str(image.id) + EXTENSIONS[media_type])
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(content)
            os.replace(temporary, target)
            updated = current.model_copy(
                update={
                    "images": (*current.images, image),
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            devices[display_id] = updated
            self._save(devices)
            return image

    def image_path(self, device: DisplayDevice, image_id: UUID) -> tuple[Path, DisplayImage]:
        image = next((value for value in device.images if value.id == image_id), None)
        if not image:
            raise NotFoundError("display image not found")
        path = (
            self.images_dir
            / str(device.household_id)
            / str(device.id)
            / (str(image.id) + EXTENSIONS[image.media_type])
        )
        if not path.is_file():
            raise NotFoundError("display image file not found")
        return path, image

    def delete_image(self, actor: ActorContext, display_id: UUID, image_id: UUID) -> None:
        self._manage(actor)
        with self._lock:
            devices = self._load()
            current = devices.get(display_id)
            if not current or current.household_id != actor.household_id:
                raise NotFoundError("display not found")
            image = next((value for value in current.images if value.id == image_id), None)
            if not image:
                raise NotFoundError("display image not found")
            path, _ = self.image_path(current, image_id)
            updated = current.model_copy(
                update={
                    "images": tuple(value for value in current.images if value.id != image_id),
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            devices[display_id] = updated
            self._save(devices)
            path.unlink(missing_ok=True)
