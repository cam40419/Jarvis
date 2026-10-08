from pydantic import Field

from simon.domain.models import StrictModel


class LocalPath(StrictModel):
    root: str = Field(min_length=1, max_length=100)
    path: str = Field(default="", max_length=1000)


class LocalList(LocalPath):
    offset: int = Field(default=0, ge=0, le=20000)


class LocalSearch(LocalPath):
    query: str = Field(min_length=1, max_length=200)


class LocalRead(LocalPath):
    offset: int = Field(default=0, ge=0, le=2000000)
    limit: int = Field(default=12000, ge=1, le=24000)


class LocalWrite(LocalPath):
    content: str = Field(max_length=2000000)
    revision: str = Field(default="", max_length=64)


class LocalEdit(LocalPath):
    revision: str = Field(min_length=64, max_length=64)
    old_text: str = Field(min_length=1, max_length=100000)
    new_text: str = Field(max_length=100000)


class LocalMove(LocalPath):
    destination_root: str = Field(min_length=1, max_length=100)
    destination_path: str = Field(min_length=1, max_length=1000)
    revision: str = Field(min_length=64, max_length=64)


class LocalExtract(LocalPath):
    destination_root: str = Field(min_length=1, max_length=100)
    destination_path: str = Field(min_length=1, max_length=1000)


class LocalZip(LocalExtract):
    pass
