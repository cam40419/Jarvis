# agent/agent.py
from __future__ import annotations
import os
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI

from agent.models import AgentConfig
from agent.memory import MemoryStore
from agent.toolRegistry import ToolRegistry


class Agent:
    current_conversation_id: Optional[int] = None

    def __init__(self, memory: MemoryStore, cfg: AgentConfig):
        load_dotenv()
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self.memory: MemoryStore = memory
        self.cfg: AgentConfig = cfg
        self.tools = ToolRegistry(memory=memory)

    def message(self, message: str):
        context = self.build_context(message)

        # Build first request -> Quick response with tool calls and a query to pass to a deep model

    def build_context(self, message: str):
        print("IMPLEMENT")
        # Create messages object
        # Append Facts, semantic memory blocks, etc...
        # Append conversation messages
        # Append user message
