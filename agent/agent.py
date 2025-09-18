# agent/agent.py
from openai import OpenAI
from pydantic import BaseModel
from memory import MemoryStore
import numpy as np
from typing import Optional, List

from utils.sem import cosine


class AgentConfig(BaseModel):
    name: str
    model_fast: str = "gpt-4o-mini"
    model_deep: str = "gpt-4o"
    system_prompt: str = "You are a helpful, tool-using assistant."
    max_turns_deep: int = 6
    current_thread_key: str


class Agent:
    def __init__(self, memory: MemoryStore, cfg: AgentConfig):
        self.client = OpenAI()
        self.memory = memory
        self.cfg = cfg

    def _messages_for(self, thread_id: str) -> List[dict]:
        """Pull prior turns (adapt to your MemoryStore)."""
        try:
            return self.memory.last_messages(thread_id, limit=12)
        except AttributeError:
            return []  # if you haven't implemented this yet

    def message(self):
        # Classify message here? --> Fast, slow, tools?
        print("IMPLEMENT")

    # Build relevant memory for a given message
    def build_context(self, thread_key: str, user_msg: str, subject: str) -> list[dict]:

        # System role message (configurable)
        system = {
            "role": "system",
            "content": self.cfg.system_prompt,
        }

        # Pull top facts for the subject
        facts = self.memory.top_facts(subject, limit=15)
        fact_lines = [f"{f['k']}: {f['v']}" for f in facts]
        profile = {
            "role": "system",
            "content": "Known user profile:\n" + "\n".join(fact_lines),
        }

        # Pull the messages associated with the current thread
        thread = self.memory.last_messages(thread_key, limit=12)

        # Pull the most relevant memory based on semantic search
        sem = self.memory.search(user_msg, k=5, thread_key=thread_key)
        sem_msg = (
            {"role": "system", "content": "Relevant prior info:\n- " + "\n- ".join(sem)}
            if sem
            else None
        )

        # Build the context (messages) with the collected information
        msgs = [
            system,
            profile,
            *([sem_msg] if sem_msg else []),
            *thread,
            {"role": "user", "content": user_msg},
        ]

        return msgs

    # This should track most of this through the class itself
    def should_start_new_thread(
        self,
        last_user_msg_emb: Optional[np.ndarray],
        new_msg_emb: np.ndarray,
        last_msg_ts: float,
        now_ts: float,
        gap_seconds: int = 48 * 3600,
        sim_threshold: float = 0.75,
        explicit_new: bool = False,
        channel_changed: bool = False,
    ) -> bool:
        if explicit_new or channel_changed:
            return True
        if now_ts - last_msg_ts > gap_seconds:
            return True
        if (
            last_user_msg_emb is not None
            and cosine(last_user_msg_emb, new_msg_emb) < sim_threshold
        ):
            return True
        return False
