"""Import a downloaded Google web OAuth client into the local .env without printing secrets."""

import argparse
import json
import os
import re
from io import StringIO
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values


def configure(client_file: Path, env_file: Path) -> None:
    client = json.loads(client_file.read_text(encoding="utf-8"))["web"]
    values = {
        "SIMON_GOOGLE_CLIENT_ID": client["client_id"],
        "SIMON_GOOGLE_CLIENT_SECRET": client["client_secret"],
    }
    if any(
        not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", v) for v in values.values()
    ):
        raise ValueError("invalid Google web client fields")
    text = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    parsed = dotenv_values(stream=StringIO(text), interpolate=False)
    existing = parsed.get("SIMON_GOOGLE_TOKEN_KEY", parsed.get("JARVIS_GOOGLE_TOKEN_KEY"))
    key = existing or Fernet.generate_key().decode()
    Fernet(key.encode())  # Preserve and validate the original encryption key on reconfiguration.
    values["SIMON_GOOGLE_TOKEN_KEY"] = key
    lines = [line for line in text.splitlines() if line.split("=", 1)[0].strip() not in values]
    lines.extend(f"{name}={value}" for name, value in values.items())
    # Write complete configuration to a new private temp file, then replace atomically.
    import tempfile

    descriptor, temporary = tempfile.mkstemp(prefix=".simon-google-", dir=env_file.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines) + "\n")
        os.replace(temporary, env_file)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        configure(args.client_file, args.env_file)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(
            1,
            "Could not import Google configuration. Use a downloaded Web application client JSON "
            "and a writable .env file.\n",
        )
    print("Google settings saved locally. Restart Simon, then open Connections > Connect Google.")
    print("Keep .env private and backed up; its key is needed to read Google credentials.")


if __name__ == "__main__":
    main()
