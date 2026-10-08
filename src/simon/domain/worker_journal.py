"""Event categories emitted by independent bounded workers."""

from typing import Literal

JournalKind = Literal["context", "model_response", "candidate", "evidence", "review"]
