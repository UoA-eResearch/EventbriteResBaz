# EventbriteResBaz

Tooling for programmatically creating and managing Eventbrite events for
[ResBaz Auckland](https://resbaz.auckland.ac.nz/) from a shared Google Sheet.

Two interfaces are provided:

| Interface | File | Best for |
|-----------|------|----------|
| Jupyter notebook | `EventbriteResBaz.ipynb` | Exploratory / one-off runs in Google Colab |
| **Command-line tool** | `cli.py` | Repeatable, scriptable workflows for staff |

---

## CLI Quick-start

### 1. Prerequisites

- Python 3.9+
- A Google Cloud **service account** with access to the Google Sheet
  (see [Creating a service account](#creating-a-google-service-account) below)
- An [Eventbrite API key](https://www.eventbrite.com/platform/api-keys)

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure secrets

```bash
cp .env.example .env
# Open .env in your editor and fill in every value
```

Key variables in `.env`:

| Variable | Description |
|----------|-------------|
| `EVENTBRITE_API_KEY` | Your Eventbrite private API token |
| `EVENTBRITE_ORG_ID` | Eventbrite organisation ID (visible in the org dashboard URL) |
| `EVENTBRITE_TEMPLATE_ID` | ID of the Eventbrite "template" event that new events are copied from |
| `GOOGLE_SHEET_KEY` | Spreadsheet ID from the Google Sheets URL |
| `GOOGLE_WORKSHEET_NAME` | Worksheet (tab) name — defaults to `sessions` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Absolute path to your service account JSON key file |
| `RESBAZ_SCHEDULE_URL` | URL of the ResBaz schedule YAML on GitHub |

> **Never commit `.env` to version control.**  It is already listed in `.gitignore`.

---

## CLI Commands

Run `python cli.py --help` to see all commands, or `python cli.py COMMAND --help`
for detailed help on any command.

### `list-events`

List Eventbrite events in your organisation.

```bash
python cli.py list-events
python cli.py list-events --status draft
python cli.py list-events --status live --page-size 100
```

### `create-events`

Create new Eventbrite events from Google Sheet rows that do not yet have a
registration link.  Each event is copied from the template event and then
updated with the sheet data.  Rich descriptions (structured content) are also
set.

```bash
python cli.py create-events --dry-run   # preview only
python cli.py create-events
python cli.py create-events --content-version 3
```

After creating events, run `update-sheet` to sync schedule times and durations
back to the Google Sheet (registration URLs are already in the sheet after
Eventbrite creates the events).

### `update-events`

Sync changes from the Google Sheet to **existing** Eventbrite events (title,
description, times, capacity, and structured content).

```bash
python cli.py update-events --dry-run
python cli.py update-events
python cli.py update-events --skip-descriptions
```

### `set-zoom`

Set the Zoom meeting link for online events.  Reads the `zoom_link` column
from the Google Sheet.

```bash
python cli.py set-zoom --dry-run
python cli.py set-zoom
python cli.py set-zoom --content-version 8   # if links don't appear, increment this
```

### `publish`

Publish events (make them publicly visible on Eventbrite).

```bash
python cli.py publish --dry-run
python cli.py publish                                     # all events in sheet
python cli.py publish --event-id 634393647477             # specific event
python cli.py publish --event-id 634393647477 --event-id 634393376667
```

### `unpublish`

Unpublish events (hide them from the public).

```bash
python cli.py unpublish --dry-run
python cli.py unpublish --event-id 634393647477
python cli.py unpublish   # all events in sheet
```

### `delete-drafts`

**Destructive.**  Delete all draft events in the organisation.  The command
always lists every event that will be deleted and requires an explicit `y`
confirmation before proceeding.

```bash
python cli.py delete-drafts --dry-run   # safe preview — no changes made
python cli.py delete-drafts
```

### `update-sheet`

Sync schedule times and durations from the ResBaz schedule back to the Google
Sheet.  This updates columns J (duration), R–U (start/end times in UTC and
Auckland timezone).  Registration URLs (column N) are not modified by this
command; they are populated by Eventbrite when events are created.

```bash
python cli.py update-sheet --dry-run
python cli.py update-sheet
```

### `update-ticket-classes`

Update the capacity and sales-end time of the first ticket class for each
event in the Google Sheet.

```bash
python cli.py update-ticket-classes --dry-run
python cli.py update-ticket-classes
```

### `get-attendees`

Export all attendees (across all events in the sheet) to a CSV file,
deduplicated by email address.

```bash
python cli.py get-attendees
python cli.py get-attendees --output my-attendees.csv
python cli.py get-attendees --full   # also export a full (non-deduplicated) copy
```

### `check`

Compare the Google Sheet against live Eventbrite events and report any
differences in title.

```bash
python cli.py check
python cli.py check --show-diff   # show character-level inline diffs
```

---

## Typical workflow

```
# 1. (One-time) Configure .env
cp .env.example .env && nano .env

# 2. Preview what will be created
python cli.py create-events --dry-run

# 3. Create events
python cli.py create-events

# 4. Sync schedule times back to the Google Sheet
python cli.py update-sheet

# 5. Set Zoom links
python cli.py set-zoom

# 6. Publish all events
python cli.py publish

# Later — sync sheet edits back to Eventbrite
python cli.py update-events

# Check everything matches
python cli.py check --show-diff
```

---

## Creating a Google Service Account

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create
   (or select) a project.
2. Enable the **Google Sheets API** and **Google Drive API**.
3. Go to **IAM & Admin → Service Accounts** and create a new service account.
4. Create a JSON key for that service account and download it.
5. Share the Google Sheet with the service account email address
   (e.g. `my-service-account@my-project.iam.gserviceaccount.com`) as an
   **Editor**.
6. Set `GOOGLE_SERVICE_ACCOUNT_JSON` in `.env` to the path of the downloaded
   JSON file.

---

## Notebook

The original Google Colab notebook (`EventbriteResBaz.ipynb`) is still
available for exploratory use.  It uses interactive Google Colab authentication
(`auth.authenticate_user()`) rather than a service account.
