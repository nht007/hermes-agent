"""Tests for Google Workspace gws bridge and CLI wrapper."""

import base64
import importlib.util
import json
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


BRIDGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/gws_bridge.py"
)
API_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/google_api.py"
)


def _gmail_text_part(mime_type, text, *, padded=True):
    data = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return {
        "mimeType": mime_type,
        "body": {"data": data if padded else data.rstrip("=")},
    }


@pytest.fixture(params=["gws", "python-client"])
def read_gmail_message(request, api_module, monkeypatch, capsys):
    def read(payload):
        message = {
            "id": "message-1", "threadId": "thread-1",
            "payload": {**payload, "headers": [
                {"name": "Subject", "value": "Confirmation"},
                *payload.get("headers", []),
            ]},
        }
        if request.param == "gws":
            fetch = MagicMock(return_value=message)
            monkeypatch.setattr(api_module, "_run_gws", fetch)
        else:
            monkeypatch.setattr(api_module, "_gws_binary", lambda: None)
            service = MagicMock()
            fetch = service.users.return_value.messages.return_value.get
            fetch.return_value.execute.return_value = message
            monkeypatch.setattr(api_module, "build_service", lambda *args: service)

        api_module.gmail_get(types.SimpleNamespace(message_id=message["id"]))
        result = json.loads(capsys.readouterr().out)
        if request.param == "gws":
            fetch.assert_called_once_with(
                ["gmail", "users", "messages", "get"],
                params={"userId": "me", "id": message["id"], "format": "full"},
            )
        else:
            fetch.assert_called_once_with(userId="me", id=message["id"], format="full")
        assert result["id"] == message["id"]
        assert result["threadId"] == message["threadId"]
        assert result["subject"] == "Confirmation"
        return result["body"]

    return read


@pytest.mark.parametrize(
    "payload, expected",
    [
        (_gmail_text_part("text/plain", "Confirmed ✓"), "Confirmed ✓"),
        (_gmail_text_part("text/plain", "Confirmed ✓", padded=False), "Confirmed ✓"),
        (_gmail_text_part("text/html", "<p>Confirmed</p>"), "<p>Confirmed</p>"),
        ({"mimeType": "multipart/alternative", "parts": [
            _gmail_text_part("text/html", "<p>HTML alternative</p>"),
            _gmail_text_part("text/plain", "Plain alternative"),
        ]}, "Plain alternative"),
        ({"mimeType": "multipart/mixed", "parts": [
            _gmail_text_part("text/html", "<p>Earlier HTML</p>"),
            {"mimeType": "multipart/related", "parts": [
                {"mimeType": "multipart/alternative", "parts": [
                    _gmail_text_part("text/html", "<p>Confirmation</p>"),
                    _gmail_text_part("text/plain", "Nested confirmation"),
                ]},
                {"mimeType": "image/png", "body": {"attachmentId": "image-1"}},
            ]},
        ]}, "Nested confirmation"),
        ({"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/related", "parts": [
                _gmail_text_part("text/html", "<p>Nested HTML only</p>"),
            ]},
        ]}, "<p>Nested HTML only</p>"),
        ({"mimeType": "multipart/mixed", "parts": []}, ""),
    ],
    ids=["root-plain", "unpadded", "root-html", "plain-preference", "nested-plain", "nested-html", "empty"],
)
def test_gmail_get_reads_visible_text_at_any_mime_depth(read_gmail_message, payload, expected):
    assert read_gmail_message(payload) == expected


def test_gmail_get_excludes_attached_text_and_messages(read_gmail_message):
    payload = {"mimeType": "multipart/mixed", "parts": [
        {**_gmail_text_part("text/plain", "Text attachment"), "filename": "notes.txt"},
        {"mimeType": "message/rfc822", "filename": "forwarded.eml", "parts": [
            _gmail_text_part("text/plain", "Attached message body"),
        ]},
        {"mimeType": "message/rfc822", "parts": [
            _gmail_text_part("text/plain", "Unnamed attached message body"),
        ]},
        {**_gmail_text_part("text/plain", "Disposition attachment"), "headers": [
            {"name": "Content-Disposition", "value": "ATTACHMENT; filename=notes.txt"},
        ]},
        {"mimeType": "multipart/related", "parts": [
            _gmail_text_part("text/html", "<p>Visible body</p>"),
        ]},
    ]}
    assert read_gmail_message(payload) == "<p>Visible body</p>"


@pytest.fixture
def bridge_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    spec = importlib.util.spec_from_file_location("gws_bridge_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    spec = importlib.util.spec_from_file_location("gws_api_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    # Ensure the gws CLI code path is taken even when the binary isn't
    # installed (CI).  Without this, calendar_list() falls through to the
    # Python SDK path which imports ``googleapiclient`` — not in deps.
    module._gws_binary = lambda: "/usr/bin/gws"
    # Bypass authentication check — no real token file in CI.
    module._ensure_authenticated = lambda: None
    return module


def _write_token(path: Path, *, token="ya29.test", expiry=None, **extra):
    data = {
        "token": token,
        "refresh_token": "1//refresh",
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        **extra,
    }
    if expiry is not None:
        data["expiry"] = expiry
    path.write_text(json.dumps(data), encoding="utf-8")


def test_bridge_returns_valid_token(bridge_module, tmp_path):
    """Non-expired token is returned without refresh."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    token_path = bridge_module.get_token_path()
    _write_token(token_path, token="ya29.valid", expiry=future)

    result = bridge_module.get_valid_token()
    assert result == "ya29.valid"










def test_bridge_main_injects_token_env(bridge_module, tmp_path):
    """main() sets GOOGLE_WORKSPACE_CLI_TOKEN in subprocess env."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    token_path = bridge_module.get_token_path()
    _write_token(token_path, token="ya29.injected", expiry=future)

    captured = {}

    def capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return MagicMock(returncode=0)

    with patch.object(sys, "argv", ["gws_bridge.py", "gmail", "+triage"]):
        with patch.object(subprocess, "run", side_effect=capture_run):
            with pytest.raises(SystemExit):
                bridge_module.main()

    assert captured["env"]["GOOGLE_WORKSPACE_CLI_TOKEN"] == "ya29.injected"
    assert captured["cmd"] == ["gws", "gmail", "+triage"]


def test_api_calendar_list_uses_events_list(api_module):
    """calendar_list calls _run_gws with events list + params."""
    captured = {}

    def capture_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="{}", stderr="")

    args = api_module.argparse.Namespace(
        start="", end="", max=25, calendar="primary", func=api_module.calendar_list,
    )

    with patch.object(api_module.subprocess, "run", side_effect=capture_run):
        api_module.calendar_list(args)

    cmd = captured["cmd"]
    # _gws_binary() returns "/usr/bin/gws", so cmd[0] is that binary
    assert cmd[0] == "/usr/bin/gws"
    assert "calendar" in cmd
    assert "events" in cmd
    assert "list" in cmd
    assert "--params" in cmd
    params = json.loads(cmd[cmd.index("--params") + 1])
    assert "timeMin" in params
    assert "timeMax" in params
    assert params["calendarId"] == "primary"












def test_api_get_credentials_refresh_persists_authorized_user_type(api_module, monkeypatch):
    token_path = api_module.TOKEN_PATH
    _write_token(token_path, token="ya29.old")

    class FakeCredentials:
        def __init__(self):
            self.expired = True
            self.refresh_token = "1//refresh"
            self.valid = True

        def refresh(self, request):
            self.expired = False

        def to_json(self):
            return json.dumps({
                "token": "ya29.refreshed",
                "refresh_token": "1//refresh",
                "client_id": "123.apps.googleusercontent.com",
                "client_secret": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
            })

    class FakeCredentialsModule:
        @staticmethod
        def from_authorized_user_file(filename, scopes):
            assert filename == str(token_path)
            assert scopes == api_module.SCOPES
            return FakeCredentials()

    google_module = types.ModuleType("google")
    oauth2_module = types.ModuleType("google.oauth2")
    credentials_module = types.ModuleType("google.oauth2.credentials")
    credentials_module.Credentials = FakeCredentialsModule
    transport_module = types.ModuleType("google.auth.transport")
    requests_module = types.ModuleType("google.auth.transport.requests")
    requests_module.Request = lambda: object()

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2_module)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", credentials_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests_module)

    creds = api_module.get_credentials()

    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert isinstance(creds, FakeCredentials)
    assert saved["token"] == "ya29.refreshed"
    assert saved["type"] == "authorized_user"


def _tabbed_doc():
    """A Doc with two tabs (one nested), as the Docs API returns with includeTabsContent."""
    def body(text):
        return {"content": [
            {"endIndex": len(text) + 2,
             "paragraph": {"elements": [{"textRun": {"content": text + "\n"}}]}},
        ]}
    return {
        "title": "Tabbed",
        "documentId": "doc1",
        "tabs": [
            {
                "tabProperties": {"tabId": "t.0", "title": "First"},
                "documentTab": {"body": body("alpha")},
                "childTabs": [
                    {
                        "tabProperties": {"tabId": "t.0.a", "title": "Nested"},
                        "documentTab": {"body": body("beta")},
                    }
                ],
            },
            {
                "tabProperties": {"tabId": "t.1", "title": "Second"},
                "documentTab": {"body": body("gamma")},
            },
        ],
    }


def test_docs_get_returns_every_tab_of_a_tabbed_doc(api_module, monkeypatch, capsys):
    """A multi-tab Doc must not lose tab content: reads traverse the tabs tree
    (preorder, nested tabs included) instead of only the legacy top-level body."""
    monkeypatch.setattr(
        api_module, "_run_gws",
        lambda parts, params=None, body=None: _tabbed_doc(),
    )
    args = types.SimpleNamespace(doc_id="doc1", tab=None)
    api_module.docs_get(args)
    result = json.loads(capsys.readouterr().out)
    tabs = {t["tabId"]: t for t in result["tabs"]}
    assert set(tabs) == {"t.0", "t.0.a", "t.1"}
    assert tabs["t.0.a"]["body"] == "beta\n"
    assert tabs["t.0.a"]["level"] == 1
    # Multi-tab docs have no single merged "body" — index spaces are independent.
    assert "body" not in result


def test_docs_append_carries_tab_id_and_refuses_ambiguous_writes(api_module, monkeypatch, capsys):
    """Each tab has its own index space, so a write must target exactly one tab:
    the insert location carries the tabId, and an un-targeted write against a
    multi-tab doc errors instead of silently landing in the first tab."""
    monkeypatch.setattr(
        api_module, "_run_gws",
        lambda parts, params=None, body=None: _tabbed_doc(),
    )
    sent = {}
    monkeypatch.setattr(
        api_module, "_docs_insert_text",
        lambda doc_id, text, index, tab_id=None: sent.update(
            {"doc_id": doc_id, "index": index, "tab_id": tab_id}
        ),
    )

    api_module.docs_append(types.SimpleNamespace(doc_id="doc1", text="more", tab="t.1"))
    assert sent["tab_id"] == "t.1"
    assert sent["index"] == len("gamma") + 1  # endIndex - 1 within THAT tab's space
    capsys.readouterr()

    with pytest.raises(SystemExit):
        api_module.docs_append(types.SimpleNamespace(doc_id="doc1", text="more", tab=None))
    err = json.loads(capsys.readouterr().err)
    assert "tabs" in err and len(err["tabs"]) == 3
