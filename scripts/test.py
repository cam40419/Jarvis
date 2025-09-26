#!/usr/bin/env python3
import os, sys, threading
from dotenv import load_dotenv

load_dotenv()

from agent.agent import Agent
from agent.models import AgentConfig, ReasoningLevel
from agent.memory import MemoryStore

DSN = {
    "host": os.getenv("DATABASE_HOST", "127.0.0.1"),
    "port": int(os.getenv("DATABASE_PORT", "3306")),
    "user": os.getenv("DATABASE_USER"),
    "password": os.getenv("DATABASE_PASSWORD"),
    "database": os.getenv("DATABASE_NAME", "jarvis"),
}


def main():
    mem = MemoryStore(DSN)
    cfg = AgentConfig(
        name="Jarvis",
        model_fast=os.getenv("MODEL_FAST", "gpt-4o-mini"),
        model_deep=os.getenv("MODEL_DEEP", "gpt-5"),
        system_prompt=os.getenv(
            "SYSTEM_PROMPT", "You are a helpful, tool-using assistant."
        ),
        default_reasoning_level=ReasoningLevel.LOW,
        max_turns_deep=6,
    )
    agent = Agent(memory=mem, cfg=cfg)

    agent.message(
        "Research cars that are competition for an audi rs3 and email your findings to me"
    )


if __name__ == "__main__":
    main()
