#!/usr/bin/env python3
"""
Export TimeTree calendar events to an .ics file and optionally sync to Google Calendar.

Usage:
    python sync.py                          # interactive prompts
    python sync.py --output calendar.ics
    python sync.py --google-calendar        # also push to Google Calendar

Credentials via environment variables (recommended):
    TIMETREE_EMAIL=you@example.com
    TIMETREE_PASSWORD=yourpassword
    TIMETREE_CALENDAR_CODE=abc123          # optional, skip interactive selection

Google Calendar setup (one-time):
    1. Go to https://console.cloud.google.com
    2. Create a project → Enable "Google Calendar API"
    3. Create OAuth 2.0 credentials (Desktop app) → download as credentials.json
    4. Place credentials.json in the same directory as this script
    5. On first run, a browser window opens for authorization → token.json is saved
"""

import argparse
import os
import sys
from pathlib import Path

from timetree_exporter.api.auth import login, AuthenticationError
from timetree_exporter.api.calendar import TimeTreeCalendar
from timetree_exporter.utils import safe_getpass
from timetree_exporter.__main__ import (
    build_single_calendar,
    fetch_labels,
    write_calendar,
    select_calendar,
)


def resolve_credentials():
    email = os.environ.get("TIMETREE_EMAIL") or input("TimeTree email: ")
    password = os.environ.get("TIMETREE_PASSWORD") or safe_getpass("TimeTree password: ", echo_char="*")
    calendar_code = os.environ.get("TIMETREE_CALENDAR_CODE")
    return email, password, calendar_code


def export_ics(output: str) -> str:
    email, password, calendar_code = resolve_credentials()

    try:
        calendar_api, calendar_id, calendar_name = select_calendar(email, password, calendar_code)
    except AuthenticationError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Calendar selection error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching events from '{calendar_name}'...")
    events = calendar_api.get_events(calendar_id, calendar_name)
    print(f"  Found {len(events)} events")

    labels = fetch_labels(calendar_api, calendar_id)
    cal = build_single_calendar(events, labels)
    write_calendar(cal, output)
    print(f"Saved: {Path(output).resolve()}")
    return output


def sync_to_google_calendar(ics_path: str):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from icalendar import Calendar as ICalendar
        from datetime import datetime, date
    except ImportError:
        print(
            "Google Calendar dependencies missing. Install them with:\n"
            "  pip install google-api-python-client google-auth-oauthlib icalendar",
            file=sys.stderr,
        )
        sys.exit(1)

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    token_file = Path("token.json")
    creds_file = Path("credentials.json")

    if not creds_file.exists():
        print(
            "credentials.json not found.\n"
            "Download it from Google Cloud Console > APIs & Services > Credentials.\n"
            "See the docstring at the top of this script for full setup instructions.",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())

    service = build("calendar", "v3", credentials=creds)

    # Ask which calendar to import into
    calendars_result = service.calendarList().list().execute()
    calendars = [c for c in calendars_result.get("items", []) if c.get("accessRole") in ("owner", "writer")]

    print("\nGoogle Calendars available:")
    for i, cal in enumerate(calendars):
        print(f"  {i + 1}. {cal['summary']}")

    choice = input(f"Import into which calendar? (Default 1): ").strip() or "1"
    if not choice.isdigit() or not 1 <= int(choice) <= len(calendars):
        print("Invalid choice.", file=sys.stderr)
        sys.exit(1)

    target_calendar = calendars[int(choice) - 1]
    calendar_id = target_calendar["id"]
    print(f"Importing into: {target_calendar['summary']}")

    with open(ics_path, "rb") as f:
        cal = ICalendar.from_ical(f.read())

    imported = skipped = errors = 0

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        def to_rfc3339(dt_val):
            if isinstance(dt_val, datetime):
                if dt_val.tzinfo is None:
                    return dt_val.isoformat() + "Z"
                return dt_val.isoformat()
            if isinstance(dt_val, date):
                return dt_val.isoformat()
            return str(dt_val)

        def as_date_or_datetime(prop):
            val = prop.dt if hasattr(prop, "dt") else prop
            if isinstance(val, datetime):
                if val.tzinfo:
                    return {"dateTime": val.isoformat(), "timeZone": str(val.tzinfo)}
                return {"dateTime": val.isoformat() + "Z", "timeZone": "UTC"}
            return {"date": val.isoformat()}

        summary = str(component.get("SUMMARY", "")).strip() or "(no title)"
        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")

        if not dtstart:
            skipped += 1
            continue

        event_body = {"summary": summary, "start": as_date_or_datetime(dtstart)}

        if dtend:
            event_body["end"] = as_date_or_datetime(dtend)
        else:
            event_body["end"] = event_body["start"]

        if component.get("DESCRIPTION"):
            event_body["description"] = str(component["DESCRIPTION"])
        if component.get("LOCATION"):
            event_body["location"] = str(component["LOCATION"])
        if component.get("UID"):
            event_body["iCalUID"] = str(component["UID"])

        try:
            service.events().import_(calendarId=calendar_id, body=event_body).execute()
            imported += 1
        except HttpError as e:
            if e.resp.status == 409:
                # Duplicate event (already exists by iCalUID)
                skipped += 1
            else:
                print(f"  Error importing '{summary}': {e}", file=sys.stderr)
                errors += 1

    print(f"Done: {imported} imported, {skipped} skipped (duplicates), {errors} errors")


def main():
    parser = argparse.ArgumentParser(
        description="Export TimeTree to .ics and optionally sync to Google Calendar"
    )
    parser.add_argument(
        "-o", "--output",
        default=str(Path.cwd() / "timetree.ics"),
        help="Output .ics file path (default: timetree.ics)",
    )
    parser.add_argument(
        "--google-calendar",
        action="store_true",
        help="After exporting, import events into Google Calendar",
    )
    parser.add_argument(
        "--google-only",
        action="store_true",
        help="Skip TimeTree export and import an existing .ics file into Google Calendar",
    )
    args = parser.parse_args()

    if args.google_only:
        if not Path(args.output).exists():
            print(f"File not found: {args.output}", file=sys.stderr)
            sys.exit(1)
        sync_to_google_calendar(args.output)
    else:
        ics_path = export_ics(args.output)
        if args.google_calendar:
            sync_to_google_calendar(ics_path)


if __name__ == "__main__":
    main()
