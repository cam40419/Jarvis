from typing import Any, Dict, List, Optional
from enum import Enum
from pydantic import BaseModel


class ReasoningLevel(Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Verbosity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentRequest(BaseModel):
    model: str
    tools: Optional[Dict] = None
    instructions: Optional[str] = None
    input: List[Dict[str, Any]]
    reasoning: Optional[ReasoningLevel] = None
    temperature: float = 0.3
    text: Optional[Any] = None


class AgentConfig(BaseModel):
    name: str
    model_fast: str = "gpt-5-nano"
    model_deep: str = "gpt-5"
    default_reasoning_level: ReasoningLevel = ReasoningLevel.LOW
    system_prompt: str = "You are a helpful, tool-using assistant."
    max_turns_deep: int = 6
