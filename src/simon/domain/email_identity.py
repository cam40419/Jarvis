"""Verified recovery addresses and single-use, bounded email challenges."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from simon.domain.models import StrictModel, utc_now

EmailAddress = Annotated[
    str,
    Field(
        min_length=6,
        max_length=254,
        pattern=r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$",
    ),
]


class EmailCode(StrictModel):
    id: UUID
    actor_id: UUID
    workspace_id: UUID
    email: EmailAddress
    purpose: Literal["verify", "reset"]
    code_hash: str = Field(repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    attempts: int = Field(default=0, ge=0, le=5)
    consumed: bool = False


class RequestResetEmail(StrictModel):
    email: EmailAddress


class VerifyRecoveryEmail(RequestResetEmail):
    current_password: str = Field(min_length=1, max_length=128, repr=False)


class SubmitEmailCode(StrictModel):
    code: str = Field(pattern=r"^[0-9]{8}$", repr=False)


class ConfirmRecoveryEmail(SubmitEmailCode):
    challenge_id: UUID


class ResetPasswordWithEmail(RequestResetEmail, SubmitEmailCode):
    password: str = Field(min_length=15, max_length=128, repr=False)
    confirm_password: str = Field(min_length=15, max_length=128, repr=False)
