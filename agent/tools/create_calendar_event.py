# create_calendar_event.py
from typing import Any, Dict, Tuple, List, Optional
import os
from datetime import datetime
from agent.clients.google_calendar_client import get_calendar_service

SCHEMA = {
    "name": "create_calendar_event",
    "description": "Create a Google Calendar event. The timezone is always set automatically to the local system timezone; do NOT ask the user for timezone or confirmation.",
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Event title."},
            "description": {
                "type": "string",
                "description": "Event description (HTML allowed).",
            },
            "location": {"type": "string", "description": "Event location."},
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary').",
                "default": "primary",
            },
            "start": {
                "type": "object",
                "description": "RFC3339 date-time (e.g., 2025-10-22T14:00:00). Timezone is automatically applied; do not ask for it.",
                "properties": {
                    "date": {"type": "string"},
                    "dateTime": {"type": "string"},
                },
            },
            "end": {
                "type": "object",
                "description": "RFC3339 date-time (e.g., 2025-10-22T14:00:00). Timezone is automatically applied; do not ask for it.",
                "properties": {
                    "date": {"type": "string"},
                    "dateTime": {"type": "string"},
                },
            },
            "make_google_meet": {
                "type": "boolean",
                "description": "If true, attach a Google Meet link.",
                "default": False,
            },
            "send_updates": {
                "type": "string",
                "description": "Whether to send updates to attendees.",
                "enum": ["all", "externalOnly", "none"],
                "default": "all",
            },
            "token_path": {
                "type": "string",
                "description": "Path to token.json. Defaults to secret/token.json or GOOGLE_TOKEN_PATH env.",
            },
        },
        "required": ["summary", "start", "end"],
        "additionalProperties": False,
    },
    "strict": True,
}


def get_local_timezone_name() -> str:
    """Return system local timezone name or 'America/New_York' fallback."""
    try:
        tzinfo = datetime.now().astimezone().tzinfo
        if hasattr(tzinfo, "key"):
            return tzinfo.key  # zoneinfo
        name = str(tzinfo)
        if "/" in name:
            return name
    except Exception:
        pass
    return "America/New_York"


def execute(args: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    try:
        summary = args["summary"]
        start = args["start"]
        end = args["end"]

        description = args.get("description")
        location = args.get("location")
        calendar_id = args.get("calendar_id", "primary")
        make_meet = bool(args.get("make_google_meet", False))
        send_updates = args.get("send_updates", "all")
        token_path = args.get("token_path")

        # Always apply local timezone
        local_tz = get_local_timezone_name()
        start["timeZone"] = local_tz
        end["timeZone"] = local_tz

        event = {"summary": summary, "start": start, "end": end}
        if description:
            event["description"] = description
        if location:
            event["location"] = location

        if make_meet:
            event["conferenceData"] = {
                "createRequest": {
                    "requestId": f"req-{summary[:30]}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        service = get_calendar_service(token_path)
        created = (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=event,
                sendUpdates=send_updates,
                supportsAttachments=False,
                conferenceDataVersion=1 if make_meet else None,
            )
            .execute()
        )

        normalized = {
            "id": created.get("id"),
            "htmlLink": created.get("htmlLink"),
            "hangoutLink": created.get("hangoutLink"),
            "summary": created.get("summary"),
            "start": created.get("start"),
            "end": created.get("end"),
            "status": created.get("status"),
        }
        return (normalized, True)

    except KeyError as e:
        return ({"error": f"Missing required field: {e.args[0]}"}, False)
    except Exception as e:
        return ({"error": f"create_calendar_event failed: {e}"}, False)
