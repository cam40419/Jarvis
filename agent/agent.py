# agent/agent.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from openai import OpenAI
from openai.types.responses.response import Response

from agent.models import AgentConfig
from agent.memory.store import MemoryStore
from agent.tool_registry import ToolRegistry
from agent.prompt import SYSTEM_PROMPT
from agent.logger import Logger, LogType

MAX_TOOL_STEPS = 6


class Agent:
    def __init__(self, memory: MemoryStore, cfg: AgentConfig, logger: Logger):
        load_dotenv()
        self.client = OpenAI(api_key=os.getenv("OPENAI_SECRET", ""))
        self.memory = memory
        self.cfg = cfg
        # Initialize tool registry and load all tools from tools directory
        self.tools = ToolRegistry(memory=memory, cfg=cfg)
        self.tools.load_package("agent.tools")

        self.logger = logger

    # Public entry point
    def chat(self, user_text: str, *, type: str = "chat") -> Dict[str, Any]:
        conv_id = self.memory.ensure_conversation(type)
        self.memory.store_message(conv_id, role="user", content=user_text)
        final = self.run_query(conv_id)
        return {"conversation_id": conv_id, "text": final}

    # Core run loop
    def run_query(self, conv_id: int) -> str:

        # Get the tool definitions
        tools_for_api = self.tools.as_openai_tools()

        # Initial model call with conversation history
        input_items = self._build_input_items(conv_id)
        resp = self.create_response(input_items=input_items, tools=tools_for_api)

        # Steps are 0 indexed
        for step in range(MAX_TOOL_STEPS):
            self.logger.log(LogType.INFO, "Executing new step", {"stepnum": step})

            # Extract the response items
            assistant_text, tool_calls = self._extract_text_and_tools(resp)

            # No more tool calls -> finalize
            if not tool_calls:
                final_text = (assistant_text or "").strip() or "(no content returned)"
                self._persist_final(conv_id, final_text)
                return final_text

            # Execute all requested tools
            tool_outputs_items = self._execute_tool_calls(conv_id, tool_calls)

            # Feed the outputs by appending them to *input* on a NEW responses.create
            resp = self.create_response(
                previous_response_id=resp.id,
                input_items=tool_outputs_items,  # only the new outputs; prev ctx is linked
                tools=tools_for_api,
            )

        # Step cap reached
        msg = "I hit my step limit. I saved intermediate results—ask me to continue."
        self._persist_final(conv_id, msg)
        return msg

    # Responses API wrappers
    def create_response(
        self,
        *,
        input_items: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        previous_response_id: Optional[str] = None,
    ) -> Response:
        kwargs: Dict[str, Any] = {
            "model": self.cfg.model_smart,
        }

        if previous_response_id:
            kwargs["previous_response_id"] = previous_response_id

        if input_items:
            kwargs["input"] = input_items

        custom_tools = tools or []

        hosted_tools = [
            {"type": "web_search"},
        ]

        kwargs["tools"] = custom_tools + hosted_tools
        kwargs["tool_choice"] = "auto"

        self.logger.log(LogType.DEBUG, "Creating openai response", kwargs)

        return self.client.responses.create(**kwargs)

    # Extract assistant text + tool calls
    def _extract_text_and_tools(
        self, resp: Response
    ) -> Tuple[str, List[Dict[str, str]]]:

        # The basic text response
        assistant_text: str = getattr(resp, "output_text", "") or ""

        # Any tool calls requested by the agent
        tool_calls: List[Dict[str, str]] = []

        # Handle all items
        for item in resp.output:
            if item.type == "function_call":
                tool_calls.append(
                    {
                        "id": item.id,
                        "call_id": item.call_id,
                        "name": item.name,
                        "arguments": item.arguments,
                    }
                )
            elif item.type == "message":
                # We rely on output_text for convenience; no inference needed here. (for now)
                pass

        return assistant_text, tool_calls

    # Tool execution -> input items
    def _execute_tool_calls(
        self, conv_id: int, tool_calls: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []

        for tc in tool_calls:
            name = tc.get("name") or ""
            raw = tc.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except Exception:
                args = {}

            result, ok = self.tools.execute(name, args)
            self.logger.log(LogType.DEBUG, "Tool call executed", args)

            # Append as a function_call_output item
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": tc.get("call_id") or "",
                    "output": self._coerce_output_to_string(result),
                }
            )

        return items

    @staticmethod
    def _coerce_output_to_string(obj: Any) -> str:
        if obj is None:
            return ""
        if isinstance(obj, str):
            return obj
        try:
            return json.dumps(obj, ensure_ascii=False)
        except Exception:
            return str(obj)

    # Message construction
    def _build_input_items(self, conv_id: int) -> List[Dict[str, Any]]:
        history = self.memory.get_conversation(conv_id, limit=self.cfg.history_turns)

        items: List[Dict[str, Any]] = [
            {"type": "message", "role": "system", "content": SYSTEM_PROMPT(self.cfg)}
        ]

        for m in history:
            role = m.get("role", "user")
            content = m.get("content", "")
            items.append({"type": "message", "role": role, "content": content})

        return items

    def _persist_final(self, conv_id: int, text: str) -> None:
        self.memory.store_message(conv_id, role="assistant", content=text)
