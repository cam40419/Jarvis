"""Machine-runner entrypoint for an operator-provisioned native host mailbox."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from simon.creative_host import validate


def dispatch(
    payload: str, directory: Path, application: str, timeout: float = 45
) -> dict[str, Any]:
    request = json.loads(payload)
    identity = str(UUID(request["invocation_id"]))
    session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    if session["application"] != application:
        raise ValueError("Runner mailbox is bound to another application")
    if Path(session["workspace"]).resolve(strict=True) != Path.cwd().resolve():
        raise ValueError("Runner workspace does not match the host session")
    request.update(application=session["application"], expires_at=time.time() + timeout)
    validate(request, session)
    # The lock deliberately survives timeout: reconcile the host before another call.
    with (directory / "busy").open("x", encoding="utf-8") as lock:
        lock.write(identity)
    if (directory / (identity + ".claimed")).exists():
        raise ValueError("Invocation already dispatched")
    temporary = directory / (identity + ".request.tmp")
    temporary.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
    temporary.replace(directory / "request.json")
    deadline = time.monotonic() + timeout
    result = directory / (identity + ".result.json")
    while time.monotonic() < deadline:
        if result.exists():
            response: dict[str, Any] = json.loads(result.read_text(encoding="utf-8"))
            if not response.get("ok"):
                raise RuntimeError("Native operation failed; reconcile host session")
            (directory / "busy").unlink()
            return response
        time.sleep(0.1)
    raise TimeoutError("Native operation outcome unknown; reconcile host session")


def main() -> int:
    result = dispatch(sys.argv[2], Path(os.environ["SIMON_CREATIVE_MAILBOX"]), sys.argv[1])
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("Creative bridge failed; inspect the host session before retrying", file=sys.stderr)
        sys.exit(2)
