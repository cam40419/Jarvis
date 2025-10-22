from typing import Any, Dict, Tuple

SCHEMA = {
    "name": "ping",
    "description": "Health check tool",
    "parameters": {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    },
}


def execute(args: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    return {"pong": True, "echo": args.get("message")}, True
