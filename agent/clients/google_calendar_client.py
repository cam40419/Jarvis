# calendar_client.py
import os
from pathlib import Path
from typing import Optional
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEFAULT_TOKEN = Path(os.getenv("GOOGLE_TOKEN_PATH"))


def get_calendar_service(token_path: Optional[str] = None):
    token_file = Path(token_path or DEFAULT_TOKEN)
    if not token_file.exists():
        raise FileNotFoundError(
            f"token.json not found at {token_file}. Run your auth bootstrap first."
        )

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed token
            token_file.write_text(creds.to_json())
        else:
            raise RuntimeError("Stored credentials are invalid and cannot refresh.")
    return build("calendar", "v3", credentials=creds)
