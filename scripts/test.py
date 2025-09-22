import os
from dotenv import load_dotenv

from agent.agent import Agent, AgentConfig, MemoryStore

load_dotenv()

agent = Agent(
    memory=MemoryStore(),
    cfg=AgentConfig(
        name=os.getenv("AGENT_NAME", "Jarvis"),
        model_fast=os.getenv("MODEL_FAST", "gpt-4o-mini"),
        model_deep=os.getenv("MODEL_DEEP", "gpt-4o"),
        system_prompt=os.getenv(
            "SYSTEM_PROMPT", "You are a helpful, tool-using assistant."
        ),
        max_turns_deep=int(os.getenv("MAX_TURNS_DEEP", "6")),
    ),
)

thread_id = os.getenv("THREAD_ID", "cli")
mode = os.getenv("MODE", "fast")  # "fast" or "deep"

print(f"{agent.cfg.name} • mode={mode} • thread={thread_id}")
print("Type /exit to quit. (Set MODE=deep in .env to use deep mode.)")

while True:
    user = input("> ").strip()
    if not user:
        continue
    if user == "/exit":
        break

    if mode == "deep":
        reply = agent.run_deep(thread_id, user)
    else:
        reply = agent.run_fast(thread_id, user)

    print(reply)
