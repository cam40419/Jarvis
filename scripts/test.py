from agent.agent import Agent
from agent.models import AgentConfig
from agent.memory.store import MemoryStore
from agent.logger import Logger
from dotenv import load_dotenv
import os

load_dotenv()

dbUser = os.getenv("DB_USER")
dbPass = os.getenv("DB_PASSWORD")
dbName = os.getenv("DB_NAME")

dsn = {
    "host": "localhost",
    "user": dbUser,
    "password": dbPass,
    "database": dbName,
}

memory = MemoryStore(dsn)
cfg = AgentConfig()
logger = Logger(dsn)

agent = Agent(memory, cfg, logger)

while True:
    user_input = input("You: ").strip()
    if user_input.lower() in ("exit", "quit"):
        print("Goodbye!")
        break

    try:
        response = agent.chat(user_input)
        print(f"Agent: {response}\n")
    except Exception as e:
        print(f"[Error] {e}\n")
