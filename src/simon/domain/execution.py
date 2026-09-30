"""Isolation contracts; configured machines are pre-provisioned dedicated runners."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, model_validator

from simon.domain.models import StrictModel


class EnvironmentDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    kind: Literal["docker", "machine"]
    os: Literal["linux", "windows", "macos"] = "linux"
    capabilities: frozenset[str] = frozenset()
    enabled: bool = False
    container_image: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,255}$"
    )
    runner_url: str | None = None
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    cpu_limit: float = Field(default=2, gt=0, le=256)
    memory_mb: int = Field(default=2048, ge=128, le=1048576)
    gpu_devices: tuple[str, ...] = ()
    network: Literal["none", "bridge"] = "none"
    max_concurrency: int = Field(default=1, ge=1, le=128)
    container_uid: int = Field(default=1000, ge=1, le=2147483647)
    container_gid: int = Field(default=1000, ge=1, le=2147483647)

    @model_validator(mode="after")
    def validate_backend(self) -> EnvironmentDefinition:
        if self.kind == "docker":
            if not self.container_image or self.os != "linux":
                raise ValueError("Docker requires a Linux image; use a machine for other OSes")
            if self.runner_url or self.credential_env:
                raise ValueError("Docker definitions cannot contain machine credentials")
        else:
            if self.container_image or self.max_concurrency != 1:
                raise ValueError("A machine must have one exclusive lease and no container image")
            if not self.runner_url or not self.credential_env:
                raise ValueError("Machines require a runner URL and credential environment name")
            parsed = urlsplit(self.runner_url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme == "http"
                    and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                raise ValueError("Machine runners require HTTPS or loopback HTTP")
        if any(not device.isdigit() for device in self.gpu_devices):
            raise ValueError("GPU devices must be explicit numeric device indexes")
        return self


class EnvironmentRequest(StrictModel):
    workspace_id: UUID
    agent_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    task_id: UUID
    attempt_id: UUID
    capabilities: frozenset[str] = frozenset()
    os: Literal["linux", "windows", "macos"] | None = None


class EnvironmentPlan(StrictModel):
    environment_id: str
    kind: Literal["docker", "machine"]
    request: EnvironmentRequest
    workspace_path: Path
    network: Literal["none", "bridge"]
    cpu_limit: float
    memory_mb: int
    gpu_devices: tuple[str, ...]


class EnvironmentLease(StrictModel):
    id: UUID
    plan: EnvironmentPlan
    definition: EnvironmentDefinition
    fencing_token: int = Field(ge=1)
    status: Literal["allocating", "active", "executing", "releasing", "released", "unknown"]
    resource_handle: str | None = None
    created_at: datetime
    heartbeat_at: datetime


class ExecutionCommand(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1, max_length=256)
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_output_bytes: int = Field(default=65536, ge=1024, le=1048576)

    @model_validator(mode="after")
    def validate_argv(self) -> ExecutionCommand:
        if not self.argv[0] or any("\x00" in arg for arg in self.argv):
            raise ValueError("Command arguments require an executable and cannot contain NUL")
        if sum(len(arg.encode("utf-8")) for arg in self.argv) > 131072:
            raise ValueError("Command arguments exceed their byte limit")
        return self


class ExecutionResult(StrictModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


class ExecutionError(RuntimeError):
    """Resource execution failed; an uncertain lease remains reserved until cleanup."""


class ExecutionCapacityError(ExecutionError):
    """No allocation was dispatched because the resource is already reserved."""
