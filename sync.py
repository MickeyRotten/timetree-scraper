#!/usr/bin/env python3
"""
TimeTree → ICS sync, with optional Google Calendar push.

First run:   python sync.py          (launches setup wizard)
Later runs:  python sync.py          (skips setup, syncs changes only)
Re-run setup: python sync.py --setup

Events are diffed against a local cache; the ICS and Google Calendar are only
updated when something actually changed.
"""

# stdlib only at the top — everything else is installed / imported lazily
import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / ".timetree_sync.json"
CACHE_FILE  = SCRIPT_DIR / "timetree_cache.json"
TOKEN_FILE  = SCRIPT_DIR / "token.json"
CREDS_FILE  = SCRIPT_DIR / "credentials.json"

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ---------------------------------------------------------------------------
# Dependency bootstrap
# ---------------------------------------------------------------------------

def _importable(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _pip(*packages: str):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", *packages],
    )


def ensure_core_deps():
    missing = []
    if not _importable("timetree_exporter"):
        missing.append("timetree-exporter")
    if not _importable("icalendar"):
        missing.append("icalendar")
    if missing:
        print(f"Installing: {', '.join(missing)} ...", flush=True)
        _pip(*missing)
        # Re-add site-packages in case this is the very first install
        import site
        for sp in site.getsitepackages():
            if sp not in sys.path:
                sys.path.insert(0, sp)


def ensure_google_deps():
    missing = []
    if not _importable("googleapiclient"):
        missing.append("google-api-python-client")
    if not _importable("google_auth_oauthlib"):
        missing.append("google-auth-oauthlib")
    if missing:
        print(f"Installing: {', '.join(missing)} ...", flush=True)
        _pip(*missing)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG_FILE.chmod(0o600)  # password lives here — keep it private
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def _hr(char="─", width=52):
    print(char * width)


def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = input(f"  {prompt}{hint}: ").strip()
    return val or default


def _ask_yn(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    val = input(f"  {prompt} {hint}: ").strip().lower()
    return (val.startswith("y") if val else default)


def _google_auth_and_pick_calendar(cfg: dict):
    """Run OAuth flow and let user pick a target Google Calendar."""
    ensure_google_deps()

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("\n  A browser window will open for Google authorization...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    service = build("calendar", "v3", credentials=creds)
    items = service.calendarList().list().execute().get("items", [])
    calendars = [c for c in items if c.get("accessRole") in ("owner", "writer")]

    print("\n  Your Google Calendars:")
    for i, cal in enumerate(calendars):
        print(f"    {i + 1}. {cal['summary']}")

    choice = _ask("Sync into which calendar? (Default 1)", "1")
    if not choice.isdigit() or not 1 <= int(choice) <= len(calendars):
        print("  Invalid choice — disabling Google Calendar sync.")
        cfg["google_calendar_enabled"] = False
        return

    target = calendars[int(choice) - 1]
    cfg["google_calendar_id"] = target["id"]
    print(f"  Will sync to: {target['summary']}")


def run_setup(cfg: dict) -> dict:
    """Interactive first-time (or re-run) setup wizard. Returns updated config."""
    print()
    _hr("═")
    print("  TimeTree → ICS Sync  |  Setup")
    _hr("═")

    # ── Step 1: core dependencies ──────────────────────────────────────────
    print("\n[1/4]  Installing core dependencies")
    _hr()
    ensure_core_deps()
    print("  OK")

    # ── Step 2: TimeTree credentials + calendar selection ──────────────────
    print("\n[2/4]  TimeTree credentials")
    _hr()
    print("  Stored in .timetree_sync.json (mode 600) next to this script.\n")

    from timetree_exporter.api.auth import (
        AuthenticationError,
        InvalidCredentialsError,
        login,
    )
    from timetree_exporter.api.calendar import TimeTreeCalendar
    from timetree_exporter.utils import safe_getpass

    for attempt in range(3):
        email    = _ask("TimeTree email", cfg.get("timetree_email", ""))
        password = safe_getpass("  TimeTree password: ", echo_char="*") or cfg.get("timetree_password", "")
        print("  Logging in...", end="", flush=True)
        try:
            session_id = login(email, password)
            print(" OK")
            break
        except InvalidCredentialsError:
            print(" wrong email or password")
            if attempt == 2:
                print("  Too many failed attempts. Run --setup to try again.")
                sys.exit(1)
        except AuthenticationError as exc:
            print(f" {exc}")
            sys.exit(1)

    cfg["timetree_email"]    = email
    cfg["timetree_password"] = password

    api       = TimeTreeCalendar(session_id)
    calendars = [m for m in api.get_metadata() if m["deactivated_at"] is None]

    print("\n  Your TimeTree calendars:")
    for i, m in enumerate(calendars):
        print(f"    {i + 1}. {m['name'] or 'Unnamed'}  (code: {m['alias_code']})")

    current_code = cfg.get("timetree_calendar_code", "")
    current_idx  = next(
        (str(i + 1) for i, m in enumerate(calendars) if m["alias_code"] == current_code),
        "1",
    )
    choice = _ask("Which calendar to export? (Default 1)", current_idx)
    if not choice.isdigit() or not 1 <= int(choice) <= len(calendars):
        print("  Invalid choice.")
        sys.exit(1)
    chosen = calendars[int(choice) - 1]
    cfg["timetree_calendar_code"] = chosen["alias_code"]
    print(f"  Selected: {chosen['name'] or 'Unnamed'}")

    # ── Step 3: output path ────────────────────────────────────────────────
    print("\n[3/4]  Output file")
    _hr()
    default_out = cfg.get("output_path", str(SCRIPT_DIR / "timetree.ics"))
    out = _ask("Path for the .ics file", default_out)
    cfg["output_path"] = str(Path(out).expanduser().resolve())
    print(f"  Will save to: {cfg['output_path']}")

    # ── Step 4: Google Calendar (optional) ────────────────────────────────
    print("\n[4/4]  Google Calendar sync  (optional)")
    _hr()
    want_google = _ask_yn("Sync changes to Google Calendar?", cfg.get("google_calendar_enabled", False))
    cfg["google_calendar_enabled"] = want_google

    if want_google:
        if not CREDS_FILE.exists():
            print(f"""
  You need an OAuth credentials file from Google Cloud Console.

    1. Go to https://console.cloud.google.com
    2. Create/select a project
    3. APIs & Services → Enable APIs → search "Google Calendar API" → Enable
    4. APIs & Services → Credentials → Create Credentials → OAuth client ID
       Application type: Desktop app
    5. Download JSON → save it as:

         {CREDS_FILE}
""")
            input("  Press Enter once credentials.json is in place...")

        if not CREDS_FILE.exists():
            print("  credentials.json not found — disabling Google Calendar sync.")
            cfg["google_calendar_enabled"] = False
        else:
            _google_auth_and_pick_calendar(cfg)

    # ── Done ───────────────────────────────────────────────────────────────
    print()
    _hr("═")
    save_config(cfg)
    print(f"  Setup complete.  Config: {CONFIG_FILE}")
    print("  Run this script again (no flags) to sync your calendar.")
    _hr("═")
    print()
    return cfg


# ---------------------------------------------------------------------------
# Cache / diff
# ---------------------------------------------------------------------------

def _serialize(val) -> str:
    if val is None:
        return ""
    if hasattr(val, "dt"):
        val = val.dt
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return str(val).strip()


def _fingerprint(component) -> dict:
    return {
        "summary":     _serialize(component.get("SUMMARY")),
        "dtstart":     _serialize(component.get("DTSTART")),
        "dtend":       _serialize(component.get("DTEND")),
        "description": _serialize(component.get("DESCRIPTION")),
        "location":    _serialize(component.get("LOCATION")),
    }


def _ics_to_cache(ics_path: str) -> dict:
    from icalendar import Calendar as ICal
    with open(ics_path, "rb") as f:
        cal = ICal.from_ical(f.read())
    return {
        _serialize(c.get("UID")): _fingerprint(c)
        for c in cal.walk()
        if c.name == "VEVENT" and c.get("UID")
    }


def load_cache() -> dict:
    return json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def compute_diff(old: dict, new: dict) -> tuple[set, set, set]:
    old_k, new_k = set(old), set(new)
    added   = new_k - old_k
    deleted = old_k - new_k
    updated = {u for u in old_k & new_k if old[u] != new[u]}
    return added, updated, deleted


# ---------------------------------------------------------------------------
# TimeTree fetch
# ---------------------------------------------------------------------------

def fetch_ical(cfg: dict):
    """Login, fetch events, return an icalendar Calendar object."""
    from timetree_exporter.api.auth import AuthenticationError, login
    from timetree_exporter.api.calendar import TimeTreeCalendar
    from timetree_exporter.__main__ import build_single_calendar, fetch_labels, select_calendar

    try:
        api, cal_id, cal_name = select_calendar(
            cfg["timetree_email"],
            cfg["timetree_password"],
            cfg.get("timetree_calendar_code"),
        )
    except AuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching events from '{cal_name}'...", flush=True)
    events = api.get_events(cal_id, cal_name)
    print(f"  {len(events)} events found")

    labels = fetch_labels(api, cal_id)
    return build_single_calendar(events, labels)


# ---------------------------------------------------------------------------
# Google Calendar sync (diff-aware)
# ---------------------------------------------------------------------------

def _google_service():
    ensure_google_deps()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def _as_google_time(prop) -> dict:
    val = prop.dt if hasattr(prop, "dt") else prop
    if isinstance(val, datetime):
        if val.tzinfo:
            return {"dateTime": val.isoformat(), "timeZone": str(val.tzinfo)}
        return {"dateTime": val.isoformat() + "Z", "timeZone": "UTC"}
    return {"date": val.isoformat()}


def _event_body(component) -> dict:
    dtstart = component.get("DTSTART")
    dtend   = component.get("DTEND")
    body = {
        "summary":  _serialize(component.get("SUMMARY")) or "(no title)",
        "start":    _as_google_time(dtstart),
        "end":      _as_google_time(dtend) if dtend else _as_google_time(dtstart),
        "iCalUID":  _serialize(component.get("UID")),
    }
    if component.get("DESCRIPTION"):
        body["description"] = _serialize(component.get("DESCRIPTION"))
    if component.get("LOCATION"):
        body["location"] = _serialize(component.get("LOCATION"))
    return body


def _find_google_id(service, calendar_id: str, ical_uid: str) -> str | None:
    items = service.events().list(calendarId=calendar_id, iCalUID=ical_uid).execute().get("items", [])
    return items[0]["id"] if items else None


def apply_diff_to_google(service, calendar_id: str, ics_path: str,
                         added: set, updated: set, deleted: set):
    from googleapiclient.errors import HttpError
    from icalendar import Calendar as ICal

    with open(ics_path, "rb") as f:
        cal = ICal.from_ical(f.read())

    by_uid = {
        _serialize(c.get("UID")): c
        for c in cal.walk()
        if c.name == "VEVENT" and c.get("UID")
    }

    inserted = patched = removed = errors = 0

    for uid in added:
        if not (c := by_uid.get(uid)):
            continue
        try:
            service.events().insert(calendarId=calendar_id, body=_event_body(c)).execute()
            inserted += 1
        except HttpError as exc:
            print(f"  insert error {uid}: {exc}", file=sys.stderr)
            errors += 1

    for uid in updated:
        if not (c := by_uid.get(uid)):
            continue
        gid = _find_google_id(service, calendar_id, uid)
        try:
            if gid:
                service.events().patch(calendarId=calendar_id, eventId=gid, body=_event_body(c)).execute()
                patched += 1
            else:
                service.events().insert(calendarId=calendar_id, body=_event_body(c)).execute()
                inserted += 1
        except HttpError as exc:
            print(f"  update error {uid}: {exc}", file=sys.stderr)
            errors += 1

    for uid in deleted:
        gid = _find_google_id(service, calendar_id, uid)
        if not gid:
            continue
        try:
            service.events().delete(calendarId=calendar_id, eventId=gid).execute()
            removed += 1
        except HttpError as exc:
            print(f"  delete error {uid}: {exc}", file=sys.stderr)
            errors += 1

    print(f"Google Calendar: +{inserted} added  ~{patched} updated  -{removed} deleted  {errors} errors")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true", help="Re-run the setup wizard")
    parser.add_argument("--force", action="store_true", help="Rewrite ICS and push all events even if unchanged")
    args = parser.parse_args()

    cfg = load_config()

    # Run setup when no config exists yet, or when explicitly requested
    if args.setup or not cfg:
        cfg = run_setup(cfg)
        if args.setup:
            return  # don't also sync on a manual --setup run

    ensure_core_deps()

    output = cfg["output_path"]
    tmp    = output + ".tmp"

    ical = fetch_ical(cfg)

    from timetree_exporter.__main__ import write_calendar
    write_calendar(ical, tmp)

    old_cache = load_cache()
    new_cache = _ics_to_cache(tmp)
    added, updated, deleted = compute_diff(old_cache, new_cache)
    has_changes = bool(added or updated or deleted)

    if not has_changes and not args.force:
        print("No changes — already up to date.")
        Path(tmp).unlink(missing_ok=True)
        return

    if has_changes:
        print(f"Changes: +{len(added)} added  ~{len(updated)} updated  -{len(deleted)} deleted")
    else:
        print("No changes, but --force set — rewriting anyway.")

    Path(tmp).replace(output)
    print(f"Saved: {Path(output).resolve()}")
    save_cache(new_cache)

    if cfg.get("google_calendar_enabled") and cfg.get("google_calendar_id"):
        service = _google_service()
        cal_id  = cfg["google_calendar_id"]
        if args.force:
            apply_diff_to_google(service, cal_id, output, set(new_cache), set(), set())
        else:
            apply_diff_to_google(service, cal_id, output, added, updated, deleted)


if __name__ == "__main__":
    main()
