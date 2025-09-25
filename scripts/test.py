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

    # Ensure a conversation (subject/channel are optional in your Agent; adapt if needed)
    conv_id = agent._ensure_conversation()
    mem.set_current_thread(conv_id)

    print("Type a request. Examples:")
    print("  research AI agents for SMBs and email the findings to Alex Chen")
    print("  draft an intro email to alex@example.com about our demo\n")
    print("Ctrl+C to quit.\n")

    while True:
        try:
            text = input("> ").strip()
            if not text:
                continue
            # Stream the fast reply; any tool calls will be handled inline
            for delta in agent.message(text):
                print(delta, end="", flush=True)
            print()
        except KeyboardInterrupt:
            print("\nbye")
            break


if __name__ == "__main__":
    main()
