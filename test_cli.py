"""
Tests for the EventbriteResBaz CLI (cli.py).

Uses Click's CliRunner and unittest.mock to exercise every command without
making real network calls or Google Sheets API calls.
"""

import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from click.testing import CliRunner

from cli import (
    __version__,
    _diff_strings,
    _enrich_df_with_schedule,
    _require_env,
    cli,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────

SHEET_ROWS = [
    {
        "id": "1",
        "title": "Intro to Python",
        "description": "Learn Python basics.",
        "registration_link": "",
        "capacity": "30",
        "zoom_link": "",
    },
    {
        "id": "2",
        "title": "Data Science Workshop",
        "description": "Data science with pandas.",
        "registration_link": "https://www.eventbrite.com/e/data-science-workshop-tickets-99999",
        "capacity": "open",
        "zoom_link": "https://zoom.us/j/12345",
    },
]


def _make_df(rows=None):
    """Return a DataFrame with the same shape as the Google Sheet."""
    if rows is None:
        rows = SHEET_ROWS
    return pd.DataFrame(rows)


def _make_df_with_times(rows=None):
    """Return a DataFrame that already has start/end UTC time columns."""
    df = _make_df(rows)
    df["start_time_Auckland"] = pd.to_datetime(
        ["2025-06-10T09:00:00+12:00", "2025-06-11T10:00:00+12:00"]
    )
    df["end_time_Auckland"] = pd.to_datetime(
        ["2025-06-10T10:00:00+12:00", "2025-06-11T12:00:00+12:00"]
    )
    df["start_time_UTC"] = df["start_time_Auckland"].dt.tz_convert("UTC")
    df["end_time_UTC"] = df["end_time_Auckland"].dt.tz_convert("UTC")
    df["duration_hours"] = pd.array([1, 2], dtype="Int64")
    df["capacity"] = pd.to_numeric(
        df["capacity"].replace({"open": "1000", "": "1000"}), errors="coerce"
    ).fillna(1000).astype(int)
    return df


# ── Helper unit tests ──────────────────────────────────────────────────────────


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_require_env_raises_when_missing(monkeypatch):
    monkeypatch.delenv("EVENTBRITE_API_KEY", raising=False)
    import click

    with pytest.raises(click.ClickException, match="EVENTBRITE_API_KEY"):
        _require_env("EVENTBRITE_API_KEY")


def test_require_env_returns_value(monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "abc123")
    assert _require_env("MY_TEST_KEY") == "abc123"


def test_diff_strings_equal():
    result = _diff_strings("hello", "hello")
    assert "hello" in result


def test_diff_strings_insert():
    result = _diff_strings("hello", "hello world")
    assert " world" in result


def test_enrich_df_with_schedule():
    df = pd.DataFrame({"id": ["1", "2", "99"], "capacity": ["30", "open", ""]})
    session_lookup = {
        1: [
            {
                "startTime": pd.Timestamp("2025-06-10T09:00:00+12:00"),
                "endTime": pd.Timestamp("2025-06-10T10:00:00+12:00"),
            }
        ],
        2: [
            {
                "startTime": pd.Timestamp("2025-06-11T10:00:00+12:00"),
                "endTime": pd.Timestamp("2025-06-11T12:00:00+12:00"),
            }
        ],
    }
    result = _enrich_df_with_schedule(df, session_lookup)
    assert "start_time_UTC" in result.columns
    assert "end_time_UTC" in result.columns
    assert "duration_hours" in result.columns
    # session 99 has no entry → NaT
    assert pd.isna(result.loc[result["id"] == "99", "start_time_UTC"].iloc[0])
    # "open" and "" capacity values normalised to 1000
    assert result.loc[result["id"] == "2", "capacity"].iloc[0] == 1000
    assert result.loc[result["id"] == "99", "capacity"].iloc[0] == 1000
    # numeric capacity preserved
    assert result.loc[result["id"] == "1", "capacity"].iloc[0] == 30


# ── CLI command tests ──────────────────────────────────────────────────────────

# Shared environment variables used by most tests
ENV = {
    "EVENTBRITE_API_KEY": "test_key",
    "EVENTBRITE_ORG_ID": "12345",
    "EVENTBRITE_TEMPLATE_ID": "99999",
    "GOOGLE_SHEET_KEY": "sheet_key",
    "GOOGLE_WORKSHEET_NAME": "sessions",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "/fake/path.json",
    "RESBAZ_SCHEDULE_URL": "https://example.com/schedule.yml",
}


class TestListEvents:
    def test_missing_api_key(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["list-events"], env={})
        assert result.exit_code != 0
        assert "EVENTBRITE_API_KEY" in result.output

    def test_api_error(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        with patch("cli.requests.get", return_value=mock_resp):
            result = runner.invoke(cli, ["list-events"], env=ENV)
        assert result.exit_code != 0
        assert "401" in result.output

    def test_no_events(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"events": []}
        with patch("cli.requests.get", return_value=mock_resp):
            result = runner.invoke(cli, ["list-events"], env=ENV)
        assert result.exit_code == 0
        assert "No events found" in result.output

    def test_lists_events(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "events": [
                {
                    "id": "111",
                    "name": {"text": "Test Event"},
                    "status": "live",
                    "start": {"local": "2025-06-10T09:00:00"},
                    "url": "https://www.eventbrite.com/e/test-event-111",
                }
            ]
        }
        with patch("cli.requests.get", return_value=mock_resp):
            result = runner.invoke(cli, ["list-events"], env=ENV)
        assert result.exit_code == 0
        assert "Test Event" in result.output
        assert "live" in result.output

    def test_status_filter(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"events": []}
        with patch("cli.requests.get", return_value=mock_resp) as mock_get:
            runner.invoke(cli, ["list-events", "--status", "draft"], env=ENV)
        call_kwargs = mock_get.call_args
        assert call_kwargs.kwargs["params"]["status"] == "draft"


class TestCreateEvents:
    def _mock_sheet(self, df):
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            yield mock_ws

    def test_dry_run_no_api_calls(self):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("network")):
                with patch("cli.requests.post") as mock_post:
                    result = runner.invoke(
                        cli, ["create-events", "--dry-run"], env=ENV
                    )
        assert result.exit_code == 0
        mock_post.assert_not_called()
        assert "dry-run" in result.output

    def test_no_schedule_raises_when_column_missing(self):
        runner = CliRunner()
        # df without start_time_UTC column
        df = _make_df()
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("network")):
                result = runner.invoke(cli, ["create-events"], env=ENV)
        assert result.exit_code != 0
        assert "start_time_UTC" in result.output

    def test_all_have_links_returns_early(self):
        runner = CliRunner()
        df = _make_df_with_times()
        # Give every row a registration link so nothing needs creating
        df["registration_link"] = "https://www.eventbrite.com/e/example-tickets-00001"
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("net")):
                result = runner.invoke(
                    cli, ["create-events", "--dry-run"], env=ENV
                )
        assert result.exit_code == 0
        assert "Nothing to create" in result.output

    def test_resbaz_suffix_appended_to_title(self):
        """create-events must send '<title> [Resbaz]' to the Eventbrite API."""
        runner = CliRunner()
        df = _make_df_with_times()
        # Only the first row lacks a registration link → needs creating
        df.loc[1, "registration_link"] = "https://www.eventbrite.com/e/existing-99999"
        mock_ws = MagicMock()

        copy_resp = MagicMock()
        copy_resp.status_code = 200
        copy_resp.json.return_value = {"id": "11111"}

        update_resp = MagicMock()
        update_resp.status_code = 200
        update_resp.json.return_value = {"id": "11111", "url": "https://www.eventbrite.com/e/new-11111"}

        desc_resp = MagicMock()
        desc_resp.status_code = 200

        post_responses = [copy_resp, update_resp, desc_resp]

        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("network")):
                with patch("cli.requests.post", side_effect=post_responses) as mock_post:
                    result = runner.invoke(cli, ["create-events"], env=ENV, input="y\n")

        assert result.exit_code == 0, result.output
        # Second POST is the update call — verify [Resbaz] suffix
        update_call_kwargs = mock_post.call_args_list[1].kwargs
        assert update_call_kwargs["json"]["event.name.html"].endswith(" [Resbaz]")


class TestDeleteDrafts:
    def test_dry_run_no_delete(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "events": [
                {
                    "id": "555",
                    "name": {"text": "Draft Event"},
                    "status": "draft",
                    "start": {"local": "2025-06-10T09:00:00"},
                    "url": "https://www.eventbrite.com/e/draft-event-555",
                }
            ]
        }
        with patch("cli.requests.get", return_value=mock_resp):
            with patch("cli.requests.delete") as mock_del:
                result = runner.invoke(
                    cli, ["delete-drafts", "--dry-run"], env=ENV
                )
        assert result.exit_code == 0
        mock_del.assert_not_called()
        assert "dry-run" in result.output
        assert "Draft Event" in result.output

    def test_no_draft_events(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"events": []}
        with patch("cli.requests.get", return_value=mock_resp):
            result = runner.invoke(cli, ["delete-drafts"], env=ENV)
        assert result.exit_code == 0
        assert "No draft events" in result.output

    def test_aborted_when_user_declines(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "events": [{"id": "555", "name": {"text": "Draft"}, "status": "draft", "start": {"local": "2025-06-10T09:00:00"}, "url": ""}]
        }
        with patch("cli.requests.get", return_value=mock_resp):
            with patch("cli.requests.delete") as mock_del:
                # Answer 'n' to the confirmation prompt
                result = runner.invoke(cli, ["delete-drafts"], env=ENV, input="n\n")
        mock_del.assert_not_called()
        assert result.exit_code != 0


class TestPublish:
    def test_dry_run_specific_id(self):
        runner = CliRunner()
        with patch("cli.requests.post") as mock_post:
            result = runner.invoke(
                cli,
                ["publish", "--event-id", "12345", "--dry-run"],
                env=ENV,
            )
        assert result.exit_code == 0
        mock_post.assert_not_called()
        assert "dry-run" in result.output

    def test_publishes_specific_event(self):
        runner = CliRunner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("cli.requests.post", return_value=mock_resp):
            result = runner.invoke(
                cli,
                ["publish", "--event-id", "12345"],
                env=ENV,
                input="y\n",
            )
        assert result.exit_code == 0
        assert "Published" in result.output


class TestUnpublish:
    def test_dry_run_specific_id(self):
        runner = CliRunner()
        with patch("cli.requests.post") as mock_post:
            result = runner.invoke(
                cli,
                ["unpublish", "--event-id", "12345", "--dry-run"],
                env=ENV,
            )
        assert result.exit_code == 0
        mock_post.assert_not_called()
        assert "dry-run" in result.output


class TestUpdateEvents:
    def test_missing_start_time_utc_column_raises(self):
        runner = CliRunner()
        df = _make_df()  # no start_time_UTC column
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("net")):
                result = runner.invoke(cli, ["update-events", "--dry-run"], env=ENV)
        assert result.exit_code != 0
        assert "start_time_UTC" in result.output

    def test_dry_run_no_api_calls(self):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("net")):
                with patch("cli.requests.post") as mock_post:
                    result = runner.invoke(
                        cli, ["update-events", "--dry-run"], env=ENV
                    )
        assert result.exit_code == 0
        mock_post.assert_not_called()
        assert "dry-run" in result.output


class TestUpdateSheet:
    def test_missing_start_time_utc_column_raises(self):
        runner = CliRunner()
        df = _make_df()  # no time columns
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("net")):
                result = runner.invoke(cli, ["update-sheet"], env=ENV)
        assert result.exit_code != 0
        assert "start_time_UTC" in result.output

    def test_dry_run_no_sheet_writes(self):
        runner = CliRunner()
        df_orig = _make_df_with_times()
        df_new = df_orig.copy()
        # Shift one row's time so an update is needed
        df_new.loc[0, "start_time_UTC"] = pd.Timestamp(
            "2025-06-10T11:00:00", tz="UTC"
        )
        mock_ws = MagicMock()

        call_count = [0]

        def _fake_load():
            if call_count[0] == 0:
                call_count[0] += 1
                return df_orig, mock_ws
            return df_new, mock_ws

        with patch("cli._load_sheet_data", side_effect=_fake_load):
            with patch("cli._load_schedule_from_github", return_value={}):
                with patch("cli._enrich_df_with_schedule", return_value=df_new):
                    result = runner.invoke(
                        cli, ["update-sheet", "--dry-run"], env=ENV
                    )
        assert result.exit_code == 0
        mock_ws.batch_update.assert_not_called()


class TestUpdateTicketClasses:
    def test_missing_start_time_utc_raises(self):
        runner = CliRunner()
        df = _make_df()
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("net")):
                result = runner.invoke(
                    cli, ["update-ticket-classes", "--dry-run"], env=ENV
                )
        assert result.exit_code != 0
        assert "start_time_UTC" in result.output

    def test_dry_run_no_api_calls(self):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli._load_schedule_from_github", side_effect=Exception("net")):
                with patch("cli.requests.post") as mock_post:
                    result = runner.invoke(
                        cli, ["update-ticket-classes", "--dry-run"], env=ENV
                    )
        assert result.exit_code == 0
        mock_post.assert_not_called()
        assert "dry-run" in result.output


class TestSetZoom:
    def test_no_zoom_links(self):
        runner = CliRunner()
        df = _make_df()
        df["zoom_link"] = ""
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            result = runner.invoke(cli, ["set-zoom"], env=ENV)
        assert result.exit_code == 0
        assert "No events with Zoom links" in result.output

    def test_missing_zoom_link_column(self):
        runner = CliRunner()
        df = _make_df()
        df = df.drop(columns=["zoom_link"], errors="ignore")
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            result = runner.invoke(cli, ["set-zoom"], env=ENV)
        assert result.exit_code != 0
        assert "zoom_link" in result.output

    def test_dry_run_shows_links(self):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli.requests.post") as mock_post:
                result = runner.invoke(cli, ["set-zoom", "--dry-run"], env=ENV)
        assert result.exit_code == 0
        mock_post.assert_not_called()
        assert "zoom.us" in result.output
        assert "dry-run" in result.output


class TestGetAttendees:
    def test_http_error_raises(self):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli.requests.get", return_value=mock_resp):
                result = runner.invoke(cli, ["get-attendees"], env=ENV)
        assert result.exit_code != 0
        assert "403" in result.output

    def test_successful_export(self, tmp_path):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()
        output_file = str(tmp_path / "attendees.csv")

        attendee_data = {
            "pagination": {"continuation": None},
            "attendees": [
                {
                    "profile": {"name": "Alice", "email": "alice@example.com"},
                    "answers": [{"answer": "University"}, {"answer": "Other"}],
                }
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = attendee_data

        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli.requests.get", return_value=mock_resp):
                result = runner.invoke(
                    cli, ["get-attendees", "--output", output_file], env=ENV
                )
        assert result.exit_code == 0
        assert "attendee" in result.output.lower()


class TestCheck:
    def test_no_events_in_sheet(self):
        runner = CliRunner()
        # All rows have empty registration_link
        df = _make_df([SHEET_ROWS[0]])
        mock_ws = MagicMock()
        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            result = runner.invoke(cli, ["check"], env=ENV)
        assert result.exit_code == 0
        assert "No events" in result.output

    def test_all_match(self):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()

        eb_events = {
            "99999": {"name": {"text": "Data Science Workshop [Resbaz]"}, "status": "live"}
        }

        def _fake_get(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            event_id = url.rstrip("/").split("/")[-1]
            m.json.return_value = eb_events.get(event_id, {"name": {"text": ""}, "status": "unknown"})
            return m

        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli.requests.get", side_effect=_fake_get):
                result = runner.invoke(cli, ["check"], env=ENV)
        assert result.exit_code == 0

    def test_title_mismatch_reported(self):
        runner = CliRunner()
        df = _make_df_with_times()
        mock_ws = MagicMock()

        def _fake_get(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "name": {"text": "TOTALLY DIFFERENT TITLE"},
                "status": "live",
            }
            return m

        with patch("cli._load_sheet_data", return_value=(df, mock_ws)):
            with patch("cli.requests.get", side_effect=_fake_get):
                result = runner.invoke(cli, ["check"], env=ENV)
        assert result.exit_code == 0
        assert "difference" in result.output.lower() or "⚠" in result.output
