# agent/agent.py
from __future__ import annotations
import os, re, threading
from typing import Tuple, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from agent.models import AgentConfig, AgentRequest, ReasoningLevel
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

    def message(self, user_text: str):
        convo = self._ensure_conversation()
        self.memory.set_current_thread(convo)
        self.memory.append_user(convo, user_text)

        # Decide if a deep job is needed
        level, queue_deep, _parallel_unused = self.reasoning(user_text)

        deep_req = None
        if queue_deep:
            deep_req = self.build_request(user_text, level, mode="deep_exec")

        # Build a short confirmation request (NO tools)
        confirm_req = self.build_request(
            user_text, ReasoningLevel.NONE, mode="quick_reply"
        )

        # Start the deep job first so the confirm can truthfully say "working on it now"
        if deep_req:
            task_id = self.memory.create_task(
                convo, title=self._derive_task_title(user_text)
            )
            threading.Thread(
                target=self._run_deep_job, args=(deep_req, task_id), daemon=True
            ).start()

        # Return a single quick confirmation (non-stream for reliability on Windows)
        text = self._confirm_text(confirm_req)
        if text:
            self.memory.append_assistant(convo, text)
            yield text

    # =============== REQUEST BUILDING ===============
    def build_request(
        self, user_text: str, reasoning_level: ReasoningLevel, mode: str
    ) -> AgentRequest:
        system_msg = self.cfg.system_prompt
        facts_block, thread_summary, anchors = self._bootstrap_blocks()
        recent = self.memory.last_messages(self.current_conversation_id, limit=8)

        msgs = [{"role": "system", "content": system_msg}]
        if facts_block:
            msgs.append({"role": "system", "content": facts_block})
        if thread_summary:
            msgs.append(
                {"role": "system", "content": "Thread summary:\n" + thread_summary}
            )
        if anchors:
            msgs.append(
                {
                    "role": "system",
                    "content": "Relevant prior info:\n- " + "\n- ".join(anchors),
                }
            )
        msgs.extend(recent)
        msgs.append({"role": "user", "content": user_text})

        if mode == "quick_reply":
            instructions = (
                "Text only response"
                "Do NOT mention limitations. Do NOT call tools or execute actions in this message."
            )
            model = self.cfg.model_fast
            temp = 0.3
            reasoning = ReasoningLevel.NONE
        else:  # deep_exec
            instructions = (
                "Plan and EXECUTE the requested task. Use tools as needed to complete it. "
                "Save links via artifacts_save_links and files via artifacts_save_files. "
                "For email: draft with email_draft, then send with email_send_local "
                "(set dry_run=false only if explicitly asked). "
                "Return a concise follow-up suitable for this thread."
            )
            model = self.cfg.model_deep
            temp = 0.2
            reasoning = reasoning_level

        return AgentRequest(
            model=model,
            instructions=instructions,
            input=msgs,
            reasoning=reasoning,
            temperature=temp,
            text=None,
        )

    # =============== QUICK CONFIRM (NO TOOLS) ===============
    def _confirm_text(self, request: AgentRequest) -> str:
        kwargs = self._request_kwargs(request)  # no tools here
        resp = self.client.responses.create(**kwargs)
        return (resp.output_text or "").strip()

    # =============== DEEP JOB (TOOLS ENABLED, BACKGROUND) ===============
    def _run_deep_job(self, request: AgentRequest, task_id: str) -> None:
        convo = self.current_conversation_id
        final_parts: List[str] = []
        kwargs = self._request_kwargs(request)
        kwargs["tools"] = self.tools.schemas()
        kwargs["tool_choice"] = "auto"

        self.memory.update_task(convo, task_id, status="running", stage="planning")

        try:
            with self.client.responses.stream(**kwargs) as stream:
                for event in stream:
                    et = getattr(event, "type", "") or ""

                    # text deltas
                    if "output_text.delta" in et:
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            final_parts.append(delta)
                        continue

                    if "response.completed" in et:
                        break

                # finalize
                try:
                    _ = stream.get_final_response()
                except Exception:
                    pass

            deep_text = "".join(final_parts).strip()
            if deep_text:
                self.memory.append_assistant(convo, deep_text)

            self.memory.update_task(convo, task_id, status="succeeded", stage="done")

        except Exception as e:
            self.memory.update_task(convo, task_id, status="failed", stage=str(e)[:200])
            self.memory.append_assistant(convo, f"(Background task failed: {e})")

    # =============== REASONING (WHEN TO KICK DEEP) ===============
    def reasoning(self, message: str) -> Tuple[ReasoningLevel, bool, bool]:
        # Simple bias: action-like requests always queue deep
        if self._is_action_intent(message):
            return (ReasoningLevel.MEDIUM, True, False)
        score = self._complexity_score(message.strip())
        T1, T2, T3 = 25, 55, 75
        if score < T1:
            return (ReasoningLevel.NONE, False, False)
        if score < T2:
            return (ReasoningLevel.LOW, False, False)
        if score < T3:
            return (ReasoningLevel.MEDIUM, True, False)
        return (ReasoningLevel.HIGH, True, True)

    def _is_action_intent(self, text: str) -> bool:
        t = text.lower()
        if re.search(
            r"\b(send|email|draft|schedule|create|add|book|invite|store|save|upload|lookup|find)\b",
            t,
        ):
            return True
        if re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", t, re.I):
            return True
        return False

    def _complexity_score(self, text: str) -> int:
        score, n = 0, len(text)
        if n > 300:
            score += 10
        if n > 800:
            score += 20
        if re.search(r"\b(and then|also|in addition|next,|after that)\b", text, re.I):
            score += 15
        if re.search(
            r"\b(plan|roadmap|architecture|design|trade[- ]?offs?|evaluation|benchmark)\b",
            text,
            re.I,
        ):
            score += 15
        if re.search(
            r"\b(latest|today|price|cost|law|regulation|schedule|news|release)\b",
            text,
            re.I,
        ):
            score += 20
        if re.search(
            r"\b(calendar|invite|email|meeting|create event|schedule)\b", text, re.I
        ):
            score += 15
        if len(re.findall(r"\b(it|this|that|they|them|those)\b", text, re.I)) >= 4:
            score += 10
        if re.search(r"\bprivacy|security|pii|policy|compliance\b", text, re.I):
            score += 10
        return min(score, 100)

    # =============== CONTEXT HELPERS ===============
    def _ensure_conversation(self) -> int:
        if self.current_conversation_id is None:
            self.current_conversation_id = self.memory.create_conversation(
                subject_id=getattr(self.cfg, "subject_id", "user:default"),
                channel=getattr(self.cfg, "channel", "cli"),
            )
        return self.current_conversation_id

    def _bootstrap_blocks(self) -> tuple[str, str, List[str]]:
        boot = getattr(self, "_boot", None)
        if boot is not None:
            return boot["facts"], boot["summary"], boot["anchors"]

        facts = (
            self.memory.top_facts(
                getattr(self.cfg, "subject_id", "user:default"), limit=15
            )
            or []
        )
        facts_block = (
            "Known user profile:\n" + "\n".join(f"{f['k']}: {f['v']}" for f in facts)
            if facts
            else ""
        )

        summary = (
            getattr(self.memory, "latest_thread_summary", lambda *_: "")(
                self.current_conversation_id
            )
            or ""
        )
        anchors = (
            self.memory.search(
                "Key preferences and prior decisions",
                k=5,
                thread_key=self.current_conversation_id,
            )
            or []
        )

        self._boot = {"facts": facts_block, "summary": summary, "anchors": anchors}
        return facts_block, summary, anchors

    # =============== MODEL CAPS HELPERS ===============
    def _model_caps(self, model: str) -> dict:
        caps = {"supports_temperature": False, "supports_reasoning": False}
        if model in {"gpt-4o", "gpt-4o-mini"}:
            caps["supports_temperature"] = True
        if model in {"gpt-5"}:
            caps["supports_reasoning"] = True
        return caps

    def _request_kwargs(self, request: AgentRequest) -> dict:
        caps = self._model_caps(request.model)
        kwargs = {
            "model": request.model,
            "instructions": request.instructions,
            "input": request.input,
        }
        if caps["supports_temperature"] and request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if caps["supports_reasoning"] and request.reasoning == ReasoningLevel.HIGH:
            kwargs["reasoning"] = {"effort": "high"}
        return kwargs

    def _derive_task_title(self, user_text: str) -> str:
        # Keep it short; you can get fancy later
        return (user_text[:80] + "…") if len(user_text) > 80 else user_text
