from typing import Any, Dict, List, Optional
from enum import Enum
from pydantic import BaseModel
from dataclasses import dataclass


@dataclass
class AgentConfig:
    model_smart: str = "gpt-5"
    max_tokens: int = 1200
    history_turns: int = 30
