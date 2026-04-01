#!/usr/bin/env python3
"""
EventbriteResBaz CLI

A command-line tool for managing Eventbrite events for ResBaz based on a
shared Google Sheet.

Configuration is loaded from a .env file (see .env.example).
"""

import difflib
import os
from io import StringIO
from pathlib import Path

__version__ = "1.0.0"

import click
import pandas as pd
import requests
from dotenv import load_dotenv
from ruamel.yaml import YAML
from tqdm.contrib.concurrent import thread_map

load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────
EVENTBRITE_API_BASE = "https://www.eventbriteapi.com/v3"
EVENTS_URL = f"{EVENTBRITE_API_BASE}/events/"

# ── Helpers ────────────────────────────────────────────────────────────────────


def _require_env(key: str) -> str:
    """Return the value of *key* from the environment, or abort with a clear message."""
    value = os.getenv(key)
    if not value:
        raise click.ClickException(
            f"Required environment variable '{key}' is not set. "
            f"Add it to your .env file (see .env.example)."
        )
    return value


def _get_headers() -> dict:
    """Return Eventbrite API request headers."""
    api_key = _require_env("EVENTBRITE_API_KEY")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _get_worksheet():
    """Return an authenticated gspread Worksheet object."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise click.ClickException(
            "google-auth and gspread are required. Run: pip install -r requirements.txt"
        ) from exc

    sa_path = _require_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not os.path.isfile(sa_path):
        raise click.ClickException(
            f"Service account file not found: {sa_path}\n"
            "Set GOOGLE_SERVICE_ACCOUNT_JSON in your .env file."
        )

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(sa_path, scopes=scopes)
    gc = gspread.authorize(creds)

    sheet_key = _require_env("GOOGLE_SHEET_KEY")
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "sessions")
    spreadsheet = gc.open_by_key(sheet_key)
    return spreadsheet.worksheet(worksheet_name)


def _load_sheet_data() -> tuple:
    """Load the Google Sheet into a DataFrame.

    Returns:
        (df, worksheet) where df is a pandas DataFrame and worksheet is the
        gspread Worksheet object (needed for updates).
    """
    worksheet = _get_worksheet()
    rows = worksheet.get_all_values()
    df = pd.DataFrame(rows[1:], columns=rows[0])
    return df, worksheet


def _load_schedule_from_github() -> dict:
    """Download the ResBaz schedule YAML from GitHub and return a session lookup dict.

    Returns:
        A dict mapping integer session IDs to a list of
        ``{"startTime": Timestamp, "endTime": Timestamp}`` dicts.
    """
    schedule_url = _require_env("RESBAZ_SCHEDULE_URL")
    response = requests.get(schedule_url, timeout=30)
    response.raise_for_status()

    content = response.text.replace("\t", " ")
    yml = YAML()
    data = yml.load(StringIO(content))

    session_lookup: dict = {}
    for day in data:
        date = day["date"]
        for timeslot in day["timeslots"]:
            start_time = pd.Timestamp(
                date + "T" + timeslot["startTime"], tz="Pacific/Auckland"
            )
            end_time = pd.Timestamp(
                date + "T" + timeslot["endTime"], tz="Pacific/Auckland"
            )
            for session_id in timeslot["sessionIds"]:
                key = int(session_id)
                session_lookup.setdefault(key, []).append(
                    {"startTime": start_time, "endTime": end_time}
                )
    return session_lookup


def _enrich_df_with_schedule(df: pd.DataFrame, session_lookup: dict) -> pd.DataFrame:
    """Add start/end time columns and capacity normalisation to *df*.

    This replicates the logic in the original Jupyter notebook.
    """

    def _get_time(id_, key="startTime"):
        try:
            id_int = int(id_)
        except (ValueError, TypeError):
            return pd.NaT
        if id_int in session_lookup:
            times = [obj[key] for obj in session_lookup[id_int]]
            return min(times) if key == "startTime" else max(times)
        return pd.NaT

    df["start_time_Auckland"] = pd.to_datetime(
        df["id"].apply(_get_time, key="startTime")
    )
    df["end_time_Auckland"] = pd.to_datetime(
        df["id"].apply(_get_time, key="endTime")
    )
    df["duration"] = df["end_time_Auckland"] - df["start_time_Auckland"]
    df["duration_hours"] = (
        df["duration"].dt.round("h").dt.components["hours"].astype("Int64")
    )
    df["start_time_UTC"] = df["start_time_Auckland"].dt.tz_convert("UTC")
    df["end_time_UTC"] = df["end_time_Auckland"].dt.tz_convert("UTC")

    # Normalise capacity: "open" or empty → 1000; column may be absent on partial DFs
    if "capacity" in df.columns:
        df["capacity"] = (
            df["capacity"]
            .replace({"open": "1000", "": "1000"})
            .pipe(pd.to_numeric, errors="coerce")
            .fillna(1000)
            .astype(int)
        )
    return df


def _diff_strings(a: str, b: str) -> str:
    """Return a readable inline diff of *a* vs *b* using ANSI colour codes."""
    output = []
    matcher = difflib.SequenceMatcher(None, a, b)
    for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
        if opcode == "equal":
            output.append(a[a0:a1])
        elif opcode == "insert":
            output.append(click.style(b[b0:b1], fg="green"))
        elif opcode == "delete":
            output.append(click.style(a[a0:a1], fg="red", strikethrough=True))
        elif opcode == "replace":
            output.append(click.style(b[b0:b1], fg="green"))
            output.append(click.style(a[a0:a1], fg="red", strikethrough=True))
    return "".join(output)


# ── CLI group ──────────────────────────────────────────────────────────────────


@click.group()
@click.version_option(__version__)
def cli():
    """EventbriteResBaz CLI — manage Eventbrite events from a Google Sheet.

    \b
    Quick-start:
      1. Copy .env.example to .env and fill in your credentials.
      2. Run a command, e.g.:  python cli.py list-events

    All commands support --help for detailed documentation.
    """


# ── list-events ────────────────────────────────────────────────────────────────


@cli.command("list-events")
@click.option(
    "--status",
    default="all",
    show_default=True,
    type=click.Choice(
        ["all", "draft", "live", "cancelled", "scheduled", "ended", "completed"],
        case_sensitive=False,
    ),
    help="Filter events by status.",
)
@click.option(
    "--page-size",
    default=50,
    show_default=True,
    type=int,
    help="Maximum number of events to return.",
)
def list_events(status, page_size):
    """List Eventbrite events in your organisation.

    \b
    Examples:
      python cli.py list-events
      python cli.py list-events --status draft
      python cli.py list-events --status live --page-size 100
    """
    headers = _get_headers()
    org_id = _require_env("EVENTBRITE_ORG_ID")

    params = {"page_size": page_size}
    if status != "all":
        params["status"] = status

    response = requests.get(
        f"{EVENTBRITE_API_BASE}/organizations/{org_id}/events/",
        headers=headers,
        params=params,
        timeout=30,
    )
    if response.status_code != 200:
        raise click.ClickException(
            f"Eventbrite API error {response.status_code}: {response.text}"
        )

    events = response.json().get("events", [])
    if not events:
        click.echo("No events found.")
        return

    click.echo(f"\nFound {len(events)} event(s):\n")
    for event in events:
        name = event.get("name", {}).get("text", "Untitled")
        ev_id = event.get("id", "?")
        ev_status = event.get("status", "unknown")
        start = event.get("start", {}).get("local", "N/A")
        url = event.get("url", "")
        click.echo(f"  [{ev_status:<12s}] {ev_id}  {name}  ({start})")
        if url:
            click.echo(f"                    {url}")
    click.echo()


# ── create-events ──────────────────────────────────────────────────────────────


@cli.command("create-events")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview what would be created without making any API calls.",
)
@click.option(
    "--content-version",
    default=2,
    show_default=True,
    type=int,
    help=(
        "Structured-content version to use when setting descriptions. "
        "Increment this if descriptions don't appear on Eventbrite."
    ),
)
def create_events(dry_run, content_version):
    """Create Eventbrite events from Google Sheet rows that have no registration link.

    The tool copies a template event on Eventbrite (EVENTBRITE_TEMPLATE_ID),
    then updates the copy with the title, description, times, and capacity from
    the sheet.  Structured content (rich description) is also set.

    After creation, run 'update-sheet' to write the new Eventbrite URLs back
    to the Google Sheet.

    \b
    Examples:
      python cli.py create-events --dry-run
      python cli.py create-events
      python cli.py create-events --content-version 3
    """
    headers = _get_headers()
    template_id = _require_env("EVENTBRITE_TEMPLATE_ID")

    click.echo("Loading Google Sheet data…")
    df, _ = _load_sheet_data()

    click.echo("Loading ResBaz schedule from GitHub…")
    try:
        session_lookup = _load_schedule_from_github()
        df = _enrich_df_with_schedule(df, session_lookup)
    except Exception as exc:
        click.echo(
            click.style(
                f"Warning: could not load schedule ({exc}). "
                "Using existing time columns from the sheet if present.",
                fg="yellow",
            )
        )

    if "start_time_UTC" not in df.columns:
        raise click.ClickException(
            "Could not determine event start times. The schedule could not be "
            "loaded and the sheet does not contain a 'start_time_UTC' column. "
            "Please either fix the schedule source or add a 'start_time_UTC' "
            "column with UTC start times to the sheet."
        )

    df_with_time = df[~df["start_time_UTC"].isna()]
    df_to_create = df_with_time[df_with_time["registration_link"] == ""]

    if df_to_create.empty:
        click.echo(
            "Nothing to create — all scheduled sessions already have a registration link."
        )
        return

    click.echo(f"\nThe following {len(df_to_create)} event(s) will be created:\n")
    for _, row in df_to_create.iterrows():
        start = row.get("start_time_UTC", "no time")
        click.echo(f"  • {row['title']}  ({start})")

    if dry_run:
        click.echo(click.style("\n[dry-run] No events created.", fg="yellow"))
        return

    click.confirm(f"\nCreate {len(df_to_create)} event(s) on Eventbrite?", abort=True)

    def _make_event(row):
        # Step 1: copy the template
        r = requests.post(f"{EVENTS_URL}{template_id}/copy/", headers=headers, timeout=30)
        if r.status_code not in (200, 201):
            click.echo(
                click.style(
                    f"  ✗ Failed to copy template for '{row.title}': HTTP {r.status_code}",
                    fg="red",
                )
            )
            return None

        new_id = r.json()["id"]
        description = row.description if row.description else "Description to follow soon."

        # Step 2: update the copy with sheet data
        r2 = requests.post(
            f"{EVENTS_URL}{new_id}/",
            headers=headers,
            timeout=30,
            json={
                "event.name.html": row.title,
                "event.description.html": description,
                "event.start.utc": row.start_time_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event.start.timezone": "Pacific/Auckland",
                "event.end.utc": row.end_time_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event.end.timezone": "Pacific/Auckland",
                "event.capacity": int(row.capacity),
            },
        )
        return r2

    click.echo("\nCreating events…")
    responses = list(
        thread_map(_make_event, df_to_create.itertuples(), total=len(df_to_create))
    )

    successes = [
        (r, row)
        for r, row in zip(responses, df_to_create.itertuples())
        if r is not None and r.status_code in (200, 201)
    ]
    failures = [
        (r, row)
        for r, row in zip(responses, df_to_create.itertuples())
        if r is None or r.status_code not in (200, 201)
    ]

    click.echo(click.style(f"\n✓ Created {len(successes)} event(s).", fg="green"))
    if failures:
        click.echo(click.style(f"✗ Failed to create {len(failures)} event(s):", fg="red"))
        for r, row in failures:
            code = r.status_code if r else "no response"
            click.echo(f"  • {row.title}: HTTP {code}")

    # Step 3: set structured-content descriptions for successful events
    if successes:
        click.echo("\nSetting structured content (descriptions)…")

        def _set_desc(args):
            r, row = args
            desc = row.description if row.description else "Description to follow soon."
            ev_id = r.json()["id"]
            return requests.post(
                f"{EVENTS_URL}{ev_id}/structured_content/{content_version}/",
                headers=headers,
                timeout=30,
                json={
                    "modules": [
                        {
                            "data": {
                                "body": {"alignment": "left", "text": desc},
                                "type": "text",
                            },
                            "type": "text",
                        }
                    ],
                    "publish": True,
                    "purpose": "listing",
                },
            )

        desc_responses = list(thread_map(_set_desc, successes, total=len(successes)))
        desc_errors = [r for r in desc_responses if r.status_code != 200]
        if desc_errors:
            click.echo(
                click.style(
                    f"  Warning: {len(desc_errors)} description(s) failed. "
                    "Try --content-version with a higher number.",
                    fg="yellow",
                )
            )
        else:
            click.echo(click.style("  ✓ Descriptions set.", fg="green"))

    click.echo(
        "\nNext step: run 'python cli.py update-sheet' to write Eventbrite URLs "
        "back to the Google Sheet."
    )


# ── update-events ──────────────────────────────────────────────────────────────


@cli.command("update-events")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without making any API calls.",
)
@click.option(
    "--content-version",
    default=2,
    show_default=True,
    type=int,
    help="Structured-content version to use for descriptions.",
)
@click.option(
    "--skip-descriptions",
    is_flag=True,
    default=False,
    help="Skip updating structured content (descriptions).",
)
def update_events(dry_run, content_version, skip_descriptions):
    """Sync Google Sheet data to existing Eventbrite events.

    Updates the title, description, start/end times, and capacity of every
    event that already has a registration link in the Google Sheet.

    \b
    Examples:
      python cli.py update-events --dry-run
      python cli.py update-events
      python cli.py update-events --content-version 3
      python cli.py update-events --skip-descriptions
    """
    headers = _get_headers()

    click.echo("Loading Google Sheet data…")
    df, _ = _load_sheet_data()

    click.echo("Loading ResBaz schedule from GitHub…")
    try:
        session_lookup = _load_schedule_from_github()
        df = _enrich_df_with_schedule(df, session_lookup)
    except Exception as exc:
        click.echo(click.style(f"Warning: could not load schedule ({exc}).", fg="yellow"))

    df_happening = df[df["registration_link"] != ""].copy()

    if "start_time_UTC" not in df_happening.columns:
        raise click.ClickException(
            "Expected 'start_time_UTC' column in data but it was missing. "
            "Ensure the schedule download and enrichment completed successfully."
        )

    df_happening["eventbrite_id"] = (
        df_happening["registration_link"].str.split("-").str[-1]
    )
    df_happening = df_happening[
        df_happening["eventbrite_id"].notna()
        & (df_happening["eventbrite_id"] != "")
        & df_happening["start_time_UTC"].notna()
    ]

    if df_happening.empty:
        click.echo("No events with registration links and schedule data found.")
        return

    click.echo(f"\nWill update {len(df_happening)} event(s):")
    for _, row in df_happening.iterrows():
        click.echo(f"  • {row['title']}  (ID: {row['eventbrite_id']})")

    if dry_run:
        click.echo(click.style("\n[dry-run] No changes made.", fg="yellow"))
        return

    click.confirm(f"\nUpdate {len(df_happening)} event(s) on Eventbrite?", abort=True)

    def _update_event(row):
        return requests.post(
            f"{EVENTS_URL}{row.eventbrite_id}/",
            headers=headers,
            timeout=30,
            json={
                "event.name.html": row.title,
                "event.description.html": row.description,
                "event.start.utc": row.start_time_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event.start.timezone": "Pacific/Auckland",
                "event.end.utc": row.end_time_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "event.end.timezone": "Pacific/Auckland",
                "event.capacity": int(row.capacity),
            },
        )

    click.echo("Updating events…")
    responses = list(
        thread_map(_update_event, df_happening.itertuples(), total=len(df_happening))
    )
    errors = [
        (r, row)
        for r, row in zip(responses, df_happening.itertuples())
        if r.status_code not in (200, 201)
    ]
    click.echo(
        click.style(
            f"\n✓ Updated {len(responses) - len(errors)} event(s).", fg="green"
        )
    )
    if errors:
        click.echo(click.style(f"✗ {len(errors)} error(s):", fg="red"))
        for r, row in errors:
            click.echo(f"  • {row.title}: HTTP {r.status_code}")

    if skip_descriptions:
        return

    # Update structured content descriptions
    click.echo("\nUpdating structured content (descriptions)…")

    def _set_desc(row):
        desc = row.description if row.description else "Description to follow soon."
        return requests.post(
            f"{EVENTS_URL}{row.eventbrite_id}/structured_content/{content_version}/",
            headers=headers,
            timeout=30,
            json={
                "modules": [
                    {
                        "data": {
                            "body": {"alignment": "left", "text": desc},
                            "type": "text",
                        },
                        "type": "text",
                    }
                ],
                "publish": True,
                "purpose": "listing",
            },
        )

    desc_responses = list(
        thread_map(_set_desc, df_happening.itertuples(), total=len(df_happening))
    )
    desc_errors = [r for r in desc_responses if r.status_code != 200]
    click.echo(
        click.style(
            f"✓ Set descriptions for {len(desc_responses) - len(desc_errors)} event(s).",
            fg="green",
        )
    )
    if desc_errors:
        click.echo(
            click.style(
                f"  Warning: {len(desc_errors)} description(s) failed. "
                "Try --content-version with a higher number.",
                fg="yellow",
            )
        )


# ── set-zoom ───────────────────────────────────────────────────────────────────


@cli.command("set-zoom")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview without making changes.",
)
@click.option(
    "--content-version",
    default=1,
    show_default=True,
    type=int,
    help=(
        "Structured-content version for digital content. "
        "Increment if Zoom links don't appear on the event page."
    ),
)
def set_zoom(dry_run, content_version):
    """Add Zoom meeting links from Google Sheet to Eventbrite events.

    Reads the 'zoom_link' column from the Google Sheet and sets the online
    meeting URL for each event that already has a registration link.

    \b
    Examples:
      python cli.py set-zoom --dry-run
      python cli.py set-zoom
      python cli.py set-zoom --content-version 8
    """
    headers = _get_headers()

    click.echo("Loading Google Sheet data…")
    df, _ = _load_sheet_data()

    if "zoom_link" not in df.columns:
        raise click.ClickException(
            "Column 'zoom_link' not found in the Google Sheet."
        )

    df["eventbrite_id"] = df["registration_link"].str.split("-").str[-1]
    df_zoom = df[
        (df["zoom_link"] != "")
        & df["eventbrite_id"].notna()
        & (df["eventbrite_id"] != "")
        & (df["registration_link"] != "")
    ]

    if df_zoom.empty:
        click.echo("No events with Zoom links found.")
        return

    click.echo(f"\nFound {len(df_zoom)} event(s) with Zoom links:\n")
    for _, row in df_zoom.iterrows():
        click.echo(f"  • {row['title']}")
        click.echo(f"    Zoom URL: {row['zoom_link']}")

    if dry_run:
        click.echo(click.style("\n[dry-run] No changes made.", fg="yellow"))
        return

    click.confirm(f"\nSet Zoom links for {len(df_zoom)} event(s)?", abort=True)

    def _set_zoom_link(row):
        r = requests.post(
            f"{EVENTS_URL}{row.eventbrite_id}/structured_content/{content_version}/"
            "?purpose=digital_content",
            headers=headers,
            timeout=30,
            json={
                "modules": [
                    {
                        "data": {
                            "webinar_url": {
                                "text": "Zoom link",
                                "url": row.zoom_link,
                            }
                        },
                        "type": "webinar",
                    }
                ],
                "publish": True,
                "purpose": "digital_content",
            },
        )
        if r.status_code != 200:
            click.echo(
                click.style(
                    f"  ✗ Error for '{row.title}': HTTP {r.status_code}. "
                    "Try --content-version with a higher number.",
                    fg="red",
                )
            )
        return r

    click.echo("Setting Zoom links…")
    responses = list(
        thread_map(_set_zoom_link, df_zoom.itertuples(), total=len(df_zoom))
    )
    successes = sum(1 for r in responses if r.status_code == 200)
    click.echo(
        click.style(
            f"\n✓ Set {successes}/{len(responses)} Zoom link(s).", fg="green"
        )
    )


# ── publish ────────────────────────────────────────────────────────────────────


@cli.command("publish")
@click.option(
    "--event-id",
    multiple=True,
    help=(
        "Specific Eventbrite event ID(s) to publish. "
        "May be supplied multiple times. "
        "If omitted, all events in the Google Sheet are published."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview without making changes.",
)
def publish(event_id, dry_run):
    """Publish Eventbrite events (make them publicly visible).

    \b
    Examples:
      python cli.py publish --dry-run
      python cli.py publish
      python cli.py publish --event-id 634393647477
      python cli.py publish --event-id 634393647477 --event-id 634393376667
    """
    headers = _get_headers()

    if event_id:
        event_ids = list(event_id)
        click.echo(f"Will publish {len(event_ids)} specified event(s):")
        for eid in event_ids:
            click.echo(f"  • {eid}")
    else:
        click.echo("Loading Google Sheet data…")
        df, _ = _load_sheet_data()
        df["eventbrite_id"] = df["registration_link"].str.split("-").str[-1]
        df_happening = df[
            (df["registration_link"] != "")
            & df["eventbrite_id"].notna()
            & (df["eventbrite_id"] != "")
        ]
        event_ids = df_happening["eventbrite_id"].tolist()
        if not event_ids:
            click.echo("No events with registration links found in the sheet.")
            return
        click.echo(f"\nWill publish {len(event_ids)} event(s) from the Google Sheet:")
        for _, row in df_happening.iterrows():
            click.echo(f"  • {row['title']}  (ID: {row['eventbrite_id']})")

    if dry_run:
        click.echo(click.style("\n[dry-run] No changes made.", fg="yellow"))
        return

    click.confirm(f"\nPublish {len(event_ids)} event(s)?", abort=True)

    def _publish(eid):
        return requests.post(f"{EVENTS_URL}{eid}/publish/", headers=headers, timeout=30)

    click.echo("Publishing…")
    responses = list(thread_map(_publish, event_ids, total=len(event_ids)))
    successes = sum(1 for r in responses if r.status_code in (200, 201))
    errors = [
        (eid, r)
        for eid, r in zip(event_ids, responses)
        if r.status_code not in (200, 201)
    ]
    click.echo(click.style(f"\n✓ Published {successes} event(s).", fg="green"))
    if errors:
        click.echo(click.style(f"✗ {len(errors)} error(s):", fg="red"))
        for eid, r in errors:
            click.echo(f"  • Event {eid}: HTTP {r.status_code}")


# ── unpublish ──────────────────────────────────────────────────────────────────


@cli.command("unpublish")
@click.option(
    "--event-id",
    multiple=True,
    help=(
        "Specific Eventbrite event ID(s) to unpublish. "
        "May be supplied multiple times. "
        "If omitted, all events in the Google Sheet are unpublished."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview without making changes.",
)
def unpublish(event_id, dry_run):
    """Unpublish Eventbrite events (hide them from the public).

    \b
    Examples:
      python cli.py unpublish --dry-run
      python cli.py unpublish --event-id 634393647477
      python cli.py unpublish
    """
    headers = _get_headers()

    if event_id:
        event_ids = list(event_id)
    else:
        click.echo("Loading Google Sheet data…")
        df, _ = _load_sheet_data()
        df["eventbrite_id"] = df["registration_link"].str.split("-").str[-1]
        df_happening = df[
            (df["registration_link"] != "")
            & df["eventbrite_id"].notna()
            & (df["eventbrite_id"] != "")
        ]
        event_ids = df_happening["eventbrite_id"].tolist()
        if not event_ids:
            click.echo("No events found.")
            return

    click.echo(f"\nAbout to unpublish {len(event_ids)} event(s):")
    for eid in event_ids:
        click.echo(f"  • Event ID: {eid}")

    if dry_run:
        click.echo(click.style("\n[dry-run] No changes made.", fg="yellow"))
        return

    click.confirm(f"\nUnpublish {len(event_ids)} event(s)?", abort=True)

    def _unpublish(eid):
        return requests.post(f"{EVENTS_URL}{eid}/unpublish/", headers=headers, timeout=30)

    click.echo("Unpublishing…")
    responses = list(thread_map(_unpublish, event_ids, total=len(event_ids)))
    successes = sum(1 for r in responses if r.status_code in (200, 201))
    click.echo(click.style(f"\n✓ Unpublished {successes} event(s).", fg="green"))


# ── delete-drafts ──────────────────────────────────────────────────────────────


@cli.command("delete-drafts")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be deleted without actually deleting anything.",
)
@click.option(
    "--page-size",
    default=200,
    show_default=True,
    type=int,
    help="Maximum number of draft events to fetch.",
)
def delete_drafts(dry_run, page_size):
    """Delete all draft events in your Eventbrite organisation.

    This command ALWAYS shows you exactly which events will be deleted and
    requires an explicit confirmation before proceeding.  Use --dry-run to
    preview without any risk.

    \b
    Examples:
      python cli.py delete-drafts --dry-run
      python cli.py delete-drafts
    """
    headers = _get_headers()
    org_id = _require_env("EVENTBRITE_ORG_ID")

    click.echo(f"Fetching draft events for organisation {org_id}…")
    response = requests.get(
        f"{EVENTBRITE_API_BASE}/organizations/{org_id}/events/",
        headers=headers,
        params={"status": "draft", "page_size": page_size},
        timeout=30,
    )
    if response.status_code != 200:
        raise click.ClickException(
            f"Eventbrite API error {response.status_code}: {response.text}"
        )

    events = response.json().get("events", [])
    if not events:
        click.echo("No draft events found.")
        return

    click.echo(
        f"\nThe following {len(events)} draft event(s) will be "
        + click.style("PERMANENTLY DELETED", fg="red", bold=True)
        + ":\n"
    )
    for event in events:
        name = event.get("name", {}).get("text", "Untitled")
        ev_id = event.get("id", "?")
        start = event.get("start", {}).get("local", "N/A")
        url = event.get("url", "")
        click.echo(f"  [{ev_id}] {name}")
        click.echo(f"           Start : {start}")
        if url:
            click.echo(f"           URL   : {url}")

    if dry_run:
        click.echo(
            click.style(
                f"\n[dry-run] {len(events)} draft event(s) would be deleted. No changes made.",
                fg="yellow",
            )
        )
        return

    click.echo()
    click.confirm(
        click.style(
            f"⚠️  PERMANENTLY DELETE all {len(events)} draft event(s)? "
            "This cannot be undone!",
            fg="red",
            bold=True,
        ),
        abort=True,
    )

    def _delete(event):
        return requests.delete(
            f"{EVENTS_URL}{event['id']}/", headers=headers, timeout=30
        )

    click.echo("Deleting…")
    responses = list(thread_map(_delete, events, total=len(events)))
    successes = sum(1 for r in responses if r.status_code in (200, 204))
    errors = [
        (ev, r)
        for ev, r in zip(events, responses)
        if r.status_code not in (200, 204)
    ]
    click.echo(click.style(f"\n✓ Deleted {successes} draft event(s).", fg="green"))
    if errors:
        click.echo(click.style(f"✗ {len(errors)} error(s):", fg="red"))
        for ev, r in errors:
            name = ev.get("name", {}).get("text", "Untitled")
            click.echo(f"  • {name}: HTTP {r.status_code}")


# ── update-sheet ───────────────────────────────────────────────────────────────


@cli.command("update-sheet")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview changes without writing to the sheet.",
)
def update_sheet(dry_run):
    """Sync schedule times and durations from the ResBaz schedule back to the Google Sheet.

    This command updates the following columns when the schedule provides
    new or changed values:
      • Column J  — Duration in hours
      • Column R  — Start time (UTC)
      • Column S  — End time (UTC)
      • Column T  — Start time (Auckland)
      • Column U  — End time (Auckland)

    Note: registration URLs (Column N) are managed directly by Eventbrite
    and are not written by this command.

    \b
    Examples:
      python cli.py update-sheet --dry-run
      python cli.py update-sheet
    """
    click.echo("Loading Google Sheet data…")
    df, worksheet = _load_sheet_data()
    orig_df = df.copy()

    click.echo("Loading ResBaz schedule from GitHub…")
    try:
        session_lookup = _load_schedule_from_github()
        df = _enrich_df_with_schedule(df, session_lookup)
    except Exception as exc:
        click.echo(click.style(f"Warning: could not load schedule ({exc}).", fg="yellow"))

    required_time_col = "start_time_UTC"
    if required_time_col not in df.columns or required_time_col not in orig_df.columns:
        raise click.ClickException(
            f"Time column '{required_time_col}' is not available; "
            "cannot compute time updates. Ensure the sheet and schedule data "
            "provide this column."
        )

    # Rows where the schedule produced new/different times
    current_times = df[required_time_col]
    original_times = orig_df[required_time_col]
    needs_time_update = df[
        current_times.notna()
        & (current_times.astype(str) != original_times.astype(str))
    ]

    time_rows = list(needs_time_update.iterrows())
    click.echo(f"\nTime updates pending  : {len(time_rows)} row(s)")

    if not time_rows:
        click.echo("Nothing to update.")
        return

    click.echo("\nRows that will be updated (time data):\n")
    for i, row in time_rows:
        click.echo(
            f"  Row {i + 2:>4}: {row.get('title', '')}  "
            f"→  {row[required_time_col].strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )

    if dry_run:
        click.echo(click.style("\n[dry-run] No changes made.", fg="yellow"))
        return

    click.confirm(f"\nWrite {len(time_rows)} row(s) to the Google Sheet?", abort=True)

    updates = []
    for i, row in time_rows:
        row_idx = i + 2  # account for header row
        updates.append(
            {
                "range": f"R{row_idx}:U{row_idx}",
                "values": [
                    [
                        row["start_time_UTC"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                        row["end_time_UTC"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                        row["start_time_Auckland"].strftime("%Y-%m-%dT%H:%M:%S"),
                        row["end_time_Auckland"].strftime("%Y-%m-%dT%H:%M:%S"),
                    ]
                ],
            }
        )
        if pd.notna(row.get("duration_hours")):
            updates.append(
                {
                    "range": f"J{row_idx}",
                    "values": [[int(row["duration_hours"])]],
                }
            )

    if updates:
        worksheet.batch_update(updates)

    click.echo(click.style("\n✓ Google Sheet updated.", fg="green"))


# ── update-ticket-classes ──────────────────────────────────────────────────────


@cli.command("update-ticket-classes")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview without making changes.",
)
def update_ticket_classes(dry_run):
    """Update ticket class capacity and sales-end time for events in the Google Sheet.

    Sets the capacity and sales end time of the first ticket class of each
    event to match the values in the Google Sheet.

    \b
    Examples:
      python cli.py update-ticket-classes --dry-run
      python cli.py update-ticket-classes
    """
    headers = _get_headers()

    click.echo("Loading Google Sheet data…")
    df, _ = _load_sheet_data()

    click.echo("Loading ResBaz schedule from GitHub…")
    try:
        session_lookup = _load_schedule_from_github()
        df = _enrich_df_with_schedule(df, session_lookup)
    except Exception as exc:
        click.echo(click.style(f"Warning: could not load schedule ({exc}).", fg="yellow"))

    df_happening = df[df["registration_link"] != ""].copy()
    df_happening["eventbrite_id"] = (
        df_happening["registration_link"].str.split("-").str[-1]
    )

    if "start_time_UTC" not in df_happening.columns:
        raise click.ClickException(
            "Expected 'start_time_UTC' column in data but it was missing. "
            "Ensure the schedule download and enrichment completed successfully."
        )

    df_happening = df_happening[
        df_happening["eventbrite_id"].notna()
        & (df_happening["eventbrite_id"] != "")
        & df_happening["start_time_UTC"].notna()
    ]

    if df_happening.empty:
        click.echo("No events with registration links and schedule data found.")
        return

    click.echo(f"\nWill update ticket classes for {len(df_happening)} event(s):")
    for _, row in df_happening.iterrows():
        click.echo(
            f"  • {row['title']}  (ID: {row['eventbrite_id']}, capacity: {row['capacity']})"
        )

    if dry_run:
        click.echo(click.style("\n[dry-run] No changes made.", fg="yellow"))
        return

    click.confirm(
        f"\nUpdate ticket classes for {len(df_happening)} event(s)?", abort=True
    )

    def _update_ticket(row):
        r = requests.get(
            f"{EVENTS_URL}{row.eventbrite_id}/ticket_classes/",
            headers=headers,
            timeout=30,
        )
        if r.status_code != 200:
            return r
        ticket_classes = r.json().get("ticket_classes", [])
        if not ticket_classes:
            return r
        ticket_class_id = ticket_classes[0]["id"]
        end_time = row.end_time_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")
        return requests.post(
            f"{EVENTS_URL}{row.eventbrite_id}/ticket_classes/{ticket_class_id}/",
            headers=headers,
            timeout=30,
            json={
                "ticket_class.capacity": int(row.capacity),
                "ticket_class.sales_end": end_time,
            },
        )

    click.echo("Updating ticket classes…")
    responses = list(
        thread_map(_update_ticket, df_happening.itertuples(), total=len(df_happening))
    )
    successes = sum(1 for r in responses if r.status_code in (200, 201))
    click.echo(
        click.style(
            f"\n✓ Updated {successes}/{len(responses)} ticket class(es).", fg="green"
        )
    )


# ── get-attendees ──────────────────────────────────────────────────────────────


@cli.command("get-attendees")
@click.option(
    "--output",
    "-o",
    default="attendees.csv",
    show_default=True,
    help="Path of the output CSV file (deduplicated by email).",
)
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Also export a full (non-deduplicated) attendee CSV.",
)
def get_attendees(output, full):
    """Export all attendees across events listed in the Google Sheet to a CSV.

    The default output is deduplicated by email address.  Use --full to also
    save a complete (non-deduplicated) copy.

    \b
    Examples:
      python cli.py get-attendees
      python cli.py get-attendees --output my-attendees.csv
      python cli.py get-attendees --full
    """
    headers = _get_headers()

    click.echo("Loading Google Sheet data…")
    df, _ = _load_sheet_data()
    df["eventbrite_id"] = df["registration_link"].str.split("-").str[-1]
    df_happening = df[
        (df["registration_link"] != "")
        & df["eventbrite_id"].notna()
        & (df["eventbrite_id"] != "")
    ]

    if df_happening.empty:
        click.echo("No events with registration links found.")
        return

    click.echo(f"Fetching attendees for {len(df_happening)} event(s)…")

    def _get_attendees(event_id):
        continuation = ""
        attendees = []
        while continuation is not None:
            resp = requests.get(
                f"{EVENTS_URL}{event_id}/attendees/",
                params={"continuation": continuation},
                headers=headers,
                timeout=60,
            )
            if resp.status_code != 200:
                raise click.ClickException(
                    f"Failed to fetch attendees for event {event_id}: "
                    f"HTTP {resp.status_code} {resp.text}"
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise click.ClickException(
                    f"Invalid JSON response from Eventbrite for event {event_id}: {exc}"
                )
            continuation = data.get("pagination", {}).get("continuation")
            attendees.extend(data.get("attendees", []))
        if not attendees:
            return pd.DataFrame()
        attendee_df = pd.json_normalize(attendees)
        if "answers" in attendee_df.columns:
            attendee_df["primary_affiliation"] = attendee_df["answers"].apply(
                lambda a: a[0].get("answer") if a else None
            )
            attendee_df["other_affiliation"] = attendee_df["answers"].apply(
                lambda a: a[-1].get("answer") if len(a) > 1 else None
            )
        return attendee_df

    attendee_dfs = list(
        thread_map(_get_attendees, df_happening["eventbrite_id"], total=len(df_happening))
    )
    attendee_dfs = [adf for adf in attendee_dfs if not adf.empty]

    if not attendee_dfs:
        click.echo("No attendees found.")
        return

    all_attendees = pd.concat(attendee_dfs, ignore_index=True)

    if full:
        full_path = str(Path(output).with_stem(Path(output).stem + "-full"))
        all_attendees.to_csv(full_path, index=False)
        click.echo(click.style(f"✓ Full attendee data saved to {full_path}", fg="green"))

    email_col = "profile.email" if "profile.email" in all_attendees.columns else None
    summary = (
        all_attendees.drop_duplicates(subset=[email_col])
        if email_col
        else all_attendees
    )
    cols = [
        c
        for c in ["profile.name", "profile.email", "primary_affiliation"]
        if c in summary.columns
    ]
    summary[cols].to_csv(output, index=False)
    click.echo(
        click.style(
            f"✓ Saved {len(summary)} unique attendee(s) to {output}", fg="green"
        )
    )


# ── check ──────────────────────────────────────────────────────────────────────


@cli.command("check")
@click.option(
    "--show-diff",
    is_flag=True,
    default=False,
    help="Show inline character-level diffs for mismatched fields.",
)
def check(show_diff):
    """Check Google Sheet data against live Eventbrite events and report differences.

    Fetches each event from Eventbrite and compares its title against what is
    recorded in the Google Sheet.  The Eventbrite event status is displayed
    alongside any mismatches but is not itself compared.

    \b
    Examples:
      python cli.py check
      python cli.py check --show-diff
    """
    headers = _get_headers()

    click.echo("Loading Google Sheet data…")
    df, _ = _load_sheet_data()
    df["eventbrite_id"] = df["registration_link"].str.split("-").str[-1]
    df_happening = df[
        (df["registration_link"] != "")
        & df["eventbrite_id"].notna()
        & (df["eventbrite_id"] != "")
    ].copy()

    if df_happening.empty:
        click.echo("No events with registration links found.")
        return

    click.echo(f"Checking {len(df_happening)} event(s) against Eventbrite…\n")

    differences = []
    from tqdm.auto import tqdm as _tqdm

    for row in _tqdm(df_happening.itertuples(), total=len(df_happening)):
        r = requests.get(
            f"{EVENTS_URL}{row.eventbrite_id}/", headers=headers, timeout=30
        )
        if r.status_code != 200:
            click.echo(
                click.style(
                    f"  Warning: could not fetch event {row.eventbrite_id} — HTTP {r.status_code}",
                    fg="yellow",
                )
            )
            continue

        event = r.json()
        eb_name = event.get("name", {}).get("text", "")
        eb_status = event.get("status", "unknown")
        sheet_name = row.title

        diffs = []
        if eb_name.lower().strip() != sheet_name.lower().strip():
            diffs.append(("title", sheet_name, eb_name))

        if diffs:
            differences.append((row, eb_status, diffs))

    if not differences:
        click.echo(
            click.style(
                "✓ All events match between Google Sheet and Eventbrite!", fg="green"
            )
        )
        return

    click.echo(
        click.style(
            f"\n⚠️  Found {len(differences)} event(s) with differences:\n", fg="yellow"
        )
    )
    for row, eb_status, diffs in differences:
        click.echo(
            f"  [{eb_status:<12s}] {row.title}  (Eventbrite ID: {row.eventbrite_id})"
        )
        if show_diff:
            for field, sheet_val, eb_val in diffs:
                click.echo(f"    field      : {field}")
                click.echo(f"    sheet      : {sheet_val!r}")
                click.echo(f"    eventbrite : {eb_val!r}")
                click.echo(f"    diff       : {_diff_strings(sheet_val, eb_val)}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
