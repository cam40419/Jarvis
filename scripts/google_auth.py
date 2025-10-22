import os
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# Scopes your agent needs (full read/write to Calendar)
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Where your secrets/tokens live
SECRETS_DIR = Path("secret")
CREDENTIALS_PATH = SECRETS_DIR / "google_credentials.json"
TOKEN_PATH = SECRETS_DIR / "google_token.json"


def main():
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    creds = None

    # Reuse token if present
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    # If no valid creds, either refresh or run OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:

            # silent refresh using the saved refresh token
            creds.refresh(Request())
        else:

            # First-time auth: opens browser, asks for consent, returns refresh token
            # `prompt='consent'` helps ensure a refresh token on the first run
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=8080, prompt="consent")

        # Persist tokens for future runs
        TOKEN_PATH.write_text(creds.to_json())

    # Done: we just wanted to authorize & store/refresh the token
    print(f"Token stored at: {TOKEN_PATH.resolve()}")


if __name__ == "__main__":
    main()
