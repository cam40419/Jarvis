# agent/agent.py
from __future__ import annotations
import os, json
from typing import Any, Dict, List

from dotenv import load_dotenv
from openai import OpenAI

from agent.models import AgentConfig, AgentRequest
from agent.memory import MemoryStore
from agent.toolRegistry import ToolRegistry


ROUTER_JSON_FORMAT = {
    "type": "json_schema",
    "name": "RouterDecision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "quick_message": {"type": "string"},
            "needs_clarification": {"type": "boolean"},
            "clarifying_questions": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "can_complete_now": {"type": "boolean"},
            "work_plan": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One line per step/tool to run in order.",
            },
            "run_deep": {
                "type": "boolean",
                "description": "True if a deep/tool run should start after this quick response.",
            },
        },
        "required": [
            "quick_message",
            "needs_clarification",
            "clarifying_questions",
            "can_complete_now",
            "work_plan",
            "run_deep",
        ],
        "additionalProperties": False,
    },
}


class Agent:
    def __init__(self, memory: MemoryStore, cfg: AgentConfig):
        load_dotenv()
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        self.memory: MemoryStore = memory
        self.cfg: AgentConfig = cfg
        self.tools = ToolRegistry(memory=memory)

    def message(self, message: str):
        input = self.build_input(message)

        router = self.client.responses.create(
            model=self.cfg.model_fast,
            instructions=(
                "You are a quick router. Produce a very short confirmation or "
                "a very short clarification. Decide if tools are needed now."
            ),
            input=input,
            text={"format": ROUTER_JSON_FORMAT},
        )
        decision = json.loads(router.output_text)

        print(decision)

    def build_input(self, message: str):
        input: List[Dict[str, Any]] = []

        # Append facts, semantic memory, etc...

        # Get the current conversation
        if self.memory.current_conversation_id is not None:
            conv = self.memory.get_conversation()
        else:
            conv = self.memory.new_conversation("chat")

        # Append conversation
        for m in conv:
            role = m["role"]
            text = m["content"] or ""
            input.append(
                {
                    "role": role,
                    "content": [{"type": "input_text", "text": text}],
                }
            )

        # Append user message
        input.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": message}],
            }
        )

        return input
