# agent/agent.py
from openai import OpenAI
from pydantic import BaseModel
from memory import MemoryStore
import numpy as np
from typing import Optional, List

from utils.sem import cosine


class AgentConfig(BaseModel):
    name: str
    model_fast: str = "gpt-5-nano"
    model_deep: str = "gpt-5"
    default_reasoning_level: str = "medium"
    system_prompt: str = "You are a helpful, tool-using assistant."
    max_turns_deep: int = 6
    current_thread_key: str


class Agent:
    def __init__(self, memory: MemoryStore, cfg: AgentConfig):
        self.client = OpenAI()
        self.memory = memory
        self.cfg = cfg

    def _messages_for(self, thread_id: str) -> List[dict]:
        """Pull prior turns (adapt to MemoryStore)."""
        try:
            return self.memory.last_messages(thread_id, limit=12)
        except AttributeError:
            return []  # if you haven't implemented this yet

    def message(self, message: str):
        # Classify message here? --> Fast, slow, tools?

        # Determine reasoning level
        r = self.reasoning()

        # Split here --> Quick response and queue long response.
        print("IMPLEMENT")

    # Build relevant memory for a given message
    def build_context(self, message: str, reasoning_level: str):
        # System message and instructions
        s = self.system()

        # Get conversation
        c = self.conversation()

    def system():
        print("IMPLEMENT ME")

    def conversation(self):
        if self.should_start_new_conversation():
            print("IMPLEMENT ME")
        else:
            print("IMPLEMENT ME")

    def should_start_new_conversation() -> bool:
        print("IMPLEMENT ME")
        # Determine if a new conversation shoiuld be started
        # Based on session change (or semantic difference??)

    def reasoning() -> str:
        print("IMPLEMENT ME")
        # Determine the reasoning level needed for the current request message
        # Possibly a lightweight model to quickly determine this
