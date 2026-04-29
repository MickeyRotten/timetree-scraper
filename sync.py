#!/usr/bin/env python3
"""
Export TimeTree calendar events to an .ics file and optionally sync to Google Calendar.

On each run, events are compared against a local cache (timetree_cache.json).
The ICS is only rewritten and Google Calendar is only updated when something changed.

Usage:
    python sync.py                          # interactive prompts
    python sync.py --output calendar.ics
    python sync.py --google-calendar        # also push diff to Google Calendar

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
import json
import os
import sys
from datetime import datetime, date
from pathlib import Path

from icalendar import Calendar as ICalendar

from timetree_exporter.api.auth import AuthenticationError
from timetree_exporter.utils import safe_getpass
from timetree_exporter.__main__ import (
    build_single_calendar,
    fetch_labels,
    write_calendar,
    select_calendar,
)

CACHE_FILE = Path("timetree_cache.json")


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def resolve_credentials():
    email = os.environ.get("TIMETREE_EMAIL") or input("TimeTree email: ")
    password = os.environ.get("TIMETREE_PASSWORD") or safe_getpass("TimeTree password: ", echo_char="*")
    calendar_code = os.environ.get("TIMETREE_CALENDAR_CODE")
    return email, password, calendar_code


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _serialize(val):
    """Convert an icalendar property value to a JSON-safe string."""
    if val is None:
        return ""
    if hasattr(val, "dt"):
        val = val.dt
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return str(val).strip()


def fingerprint(component) -> dict:
    """Extract the fields we care about from a VEVENT component."""
    return {
        "summary": _serialize(component.get("SUMMARY")),
        "dtstart": _serialize(component.get("DTSTART")),
        "dtend": _serialize(component.get("DTEND")),
        "description": _serialize(component.get("DESCRIPTION")),
        "location": _serialize(component.get("LOCATION")),
    }


def ics_to_cache(ics_path: str) -> dict:
    """Build a {uid: fingerprint} dict from an ICS file."""
    with open(ics_path, "rb") as f:
        cal = ICalendar.from_ical(f.read())
    cache = {}
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        uid = _serialize(component.get("UID"))
        if uid:
            cache[uid] = fingerprint(component)
    return cache


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def compute_diff(old: dict, new: dict) -> tuple[set, set, set]:
    """Return (added_uids, updated_uids, deleted_uids)."""
    old_keys = set(old)
    new_keys = set(new)
    added = new_keys - old_keys
    deleted = old_keys - new_keys
    updated = {uid for uid in old_keys & new_keys if old[uid] != new[uid]}
    return added, updated, deleted


# ---------------------------------------------------------------------------
# TimeTree fetch + ICS export
# ---------------------------------------------------------------------------

def fetch_and_export(output: str) -> tuple[str, ICalendar]:
    """Authenticate, fetch events, build the ICalendar object. Does NOT write yet."""
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
    return calendar_name, cal


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------

def _google_service():
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "Google Calendar dependencies missing. Install them with:\n"
            "  pip install google-api-python-client google-auth-oauthlib",
            file=sys.stderr,
        )
        sys.exit(1)

    SCOPES = ["https://www.googleapis.com/auth/calendar"]
    token_file = Path("token.json")
    creds_file = Path("credentials.json")

    if not creds_file.exists():
        print(
            "credentials.json not found.\n"
            "See the docstring at the top of this script for setup instructions.",
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

    return build("calendar", "v3", credentials=creds)


def _pick_google_calendar(service) -> str:
    """Prompt user to pick a writable Google Calendar. Returns calendar ID."""
    result = service.calendarList().list().execute()
    calendars = [c for c in result.get("items", []) if c.get("accessRole") in ("owner", "writer")]

    print("\nGoogle Calendars available:")
    for i, cal in enumerate(calendars):
        print(f"  {i + 1}. {cal['summary']}")

    choice = input("Import into which calendar? (Default 1): ").strip() or "1"
    if not choice.isdigit() or not 1 <= int(choice) <= len(calendars):
        print("Invalid choice.", file=sys.stderr)
        sys.exit(1)

    target = calendars[int(choice) - 1]
    print(f"Syncing to: {target['summary']}")
    return target["id"]


def _as_google_time(prop):
    """Convert an icalendar date/datetime property to a Google Calendar time dict."""
    val = prop.dt if hasattr(prop, "dt") else prop
    if isinstance(val, datetime):
        if val.tzinfo:
            return {"dateTime": val.isoformat(), "timeZone": str(val.tzinfo)}
        return {"dateTime": val.isoformat() + "Z", "timeZone": "UTC"}
    return {"date": val.isoformat()}


def _build_event_body(component) -> dict:
    dtstart = component.get("DTSTART")
    dtend = component.get("DTEND")
    body = {
        "summary": _serialize(component.get("SUMMARY")) or "(no title)",
        "start": _as_google_time(dtstart),
        "end": _as_google_time(dtend) if dtend else _as_google_time(dtstart),
        "iCalUID": _serialize(component.get("UID")),
    }
    if component.get("DESCRIPTION"):
        body["description"] = _serialize(component.get("DESCRIPTION"))
    if component.get("LOCATION"):
        body["location"] = _serialize(component.get("LOCATION"))
    return body


def _find_google_event_id(service, calendar_id: str, ical_uid: str) -> str | None:
    result = service.events().list(calendarId=calendar_id, iCalUID=ical_uid).execute()
    items = result.get("items", [])
    return items[0]["id"] if items else None


def apply_diff_to_google(service, calendar_id: str, ics_path: str,
                         added: set, updated: set, deleted: set):
    from googleapiclient.errors import HttpError

    with open(ics_path, "rb") as f:
        cal = ICalendar.from_ical(f.read())

    components_by_uid = {
        _serialize(c.get("UID")): c
        for c in cal.walk()
        if c.name == "VEVENT" and c.get("UID")
    }

    inserted = patched = removed = errors = 0

    for uid in added:
        component = components_by_uid.get(uid)
        if not component:
            continue
        try:
            service.events().insert(calendarId=calendar_id, body=_build_event_body(component)).execute()
            inserted += 1
        except HttpError as e:
            print(f"  Error inserting '{uid}': {e}", file=sys.stderr)
            errors += 1

    for uid in updated:
        component = components_by_uid.get(uid)
        if not component:
            continue
        google_id = _find_google_event_id(service, calendar_id, uid)
        if not google_id:
            # Not found remotely — insert instead
            try:
                service.events().insert(calendarId=calendar_id, body=_build_event_body(component)).execute()
                inserted += 1
            except HttpError as e:
                print(f"  Error inserting (fallback) '{uid}': {e}", file=sys.stderr)
                errors += 1
            continue
        try:
            service.events().patch(calendarId=calendar_id, eventId=google_id,
                                   body=_build_event_body(component)).execute()
            patched += 1
        except HttpError as e:
            print(f"  Error updating '{uid}': {e}", file=sys.stderr)
            errors += 1

    for uid in deleted:
        google_id = _find_google_event_id(service, calendar_id, uid)
        if not google_id:
            continue
        try:
            service.events().delete(calendarId=calendar_id, eventId=google_id).execute()
            removed += 1
        except HttpError as e:
            print(f"  Error deleting '{uid}': {e}", file=sys.stderr)
            errors += 1

    print(f"Google Calendar: +{inserted} added, ~{patched} updated, -{removed} deleted, {errors} errors")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export TimeTree to .ics, diff against last run, sync changes to Google Calendar"
    )
    parser.add_argument(
        "-o", "--output",
        default=str(Path.cwd() / "timetree.ics"),
        help="Output .ics file path (default: timetree.ics)",
    )
    parser.add_argument(
        "--google-calendar",
        action="store_true",
        help="Sync the diff to Google Calendar",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite the ICS and push all events even if nothing changed",
    )
    args = parser.parse_args()

    calendar_name, cal = fetch_and_export(args.output)

    # Write ICS to a temp location so we can parse it for diffing
    tmp_path = args.output + ".tmp"
    write_calendar(cal, tmp_path)

    old_cache = load_cache()
    new_cache = ics_to_cache(tmp_path)
    added, updated, deleted = compute_diff(old_cache, new_cache)

    has_changes = bool(added or updated or deleted)

    if not has_changes and not args.force:
        print("No changes detected — ICS and Google Calendar are already up to date.")
        Path(tmp_path).unlink(missing_ok=True)
        return

    if has_changes:
        print(f"Changes: +{len(added)} added, ~{len(updated)} updated, -{len(deleted)} deleted")
    else:
        print("No changes, but --force set — rewriting anyway.")

    # Promote tmp → real ICS
    Path(tmp_path).replace(args.output)
    print(f"Saved: {Path(args.output).resolve()}")

    save_cache(new_cache)

    if args.google_calendar:
        service = _google_service()
        calendar_id = _pick_google_calendar(service)
        if args.force:
            # Treat everything as added on --force
            apply_diff_to_google(service, calendar_id, args.output, new_cache.keys(), set(), set())
        else:
            apply_diff_to_google(service, calendar_id, args.output, added, updated, deleted)


if __name__ == "__main__":
    main()
