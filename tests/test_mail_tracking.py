import re
import sqlite3

import pytest
import requests

import app as mail_app
import mail_tracking as tracking


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_app, "DATABASE_PATH", tmp_path / "accounts.db")
    monkeypatch.setitem(mail_app.app.config, "TESTING", True)
    monkeypatch.delenv("TRACKING_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(mail_app, "CLIENT_ID", "test-client")
    monkeypatch.setattr(mail_app, "CLIENT_SECRET", "test-secret")
    mail_app.init_database()


@pytest.fixture
def account():
    with mail_app.database() as connection:
        return connection.execute(
            """INSERT INTO microsoft_accounts
               (home_account_id, email, display_name, token_cache, updated_at)
               VALUES ('test', 'sender@example.com', 'Sender', X'01', 'now')"""
        ).lastrowid


@pytest.fixture
def client():
    return mail_app.app.test_client()


@pytest.fixture
def microsoft_send(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return type("Response", (), {"status_code": 202})()

    monkeypatch.setattr(mail_app.requests, "post", post)
    monkeypatch.setattr(mail_app, "get_access_token_for_account", lambda account_id: ("test-token", None))
    monkeypatch.setattr(tracking, "endpoint_health", lambda *args, **kwargs: {
        "state": "online" if tracking.public_base_url() else "unconfigured", "url": tracking.public_base_url(),
    })
    return calls


def send(client, account, recipients="one@example.com", body="Hello\nhttps://example.com/?a=1&b=2"):
    return client.post("/send", data={"sender_account": account, "recipients": recipients,
                                      "subject": "Hello", "body": body}, follow_redirects=True)


def rows(client):
    return client.get("/api/tracking").json["messages"]


def make_record(account):
    email_id, message = tracking.prepare_message(mail_app.database, account, "one@example.com", "Hello", "Hi")
    tracking.finish_message(mail_app.database, email_id, "accepted")
    return email_id


def sent_copy(email_id, internet_id="<sent@example.com>"):
    return {"id": "graph-sent", "internetMessageId": internet_id,
            "internetMessageHeaders": [{"name": "X-App-Email-Id", "value": email_id}],
            "toRecipients": [{"emailAddress": {"address": "one@example.com"}}]}


def reply(internet_id="<sent@example.com>", **extra):
    return {"id": "graph-reply", "internetMessageId": "<reply@example.com>",
            "receivedDateTime": tracking.now(), "from": {"emailAddress": {"address": "one@example.com"}},
            "internetMessageHeaders": [{"name": "In-Reply-To", "value": internet_id}], **extra}


def mock_pages(monkeypatch, pages):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        page = pages.pop(0)
        return type("Response", (), {"status_code": 200, "json": lambda self: page})()

    monkeypatch.setattr(tracking.requests, "get", get)
    return calls


def test_same_microsoft_endpoint_sender_and_form_are_preserved(client, account, microsoft_send):
    response = send(client, account, recipients="one@example.com,TWO@example.com;one@example.com")
    assert b"Sent 2 email(s) successfully" in response.data
    assert b"SendGrid" not in response.data
    assert b'id="provider"' not in response.data
    assert b'id="sender_account"' in response.data
    assert len(microsoft_send) == 2
    for url, kwargs in microsoft_send:
        assert url == mail_app.GRAPH_SEND_MAIL
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        payload = kwargs["json"]
        assert payload["saveToSentItems"] is True
        assert payload["message"]["body"]["contentType"] == "Text"
        assert payload["message"]["subject"] == "Hello"
        assert "from" not in payload["message"]
        assert payload["message"]["internetMessageHeaders"][0]["name"] == tracking.TRACKING_HEADER
    records = rows(client)
    assert len({record["email_id"] for record in records}) == 2
    assert all(record["status"] == "accepted" for record in records)
    assert all(record["sender"] == "sender@example.com" for record in records)
    assert all(record["open_click_enabled"] == 0 for record in records)
    assert all("open_token" not in record for record in records)


def test_public_url_adds_tracking_without_changing_microsoft_delivery(client, account, microsoft_send, monkeypatch):
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://tracking.example.com/")
    send(client, account, body='<script>alert("x")</script>\nhttps://example.com/?a=1&b=2')
    url, kwargs = microsoft_send[0]
    assert url == mail_app.GRAPH_SEND_MAIL
    content = kwargs["json"]["message"]["body"]
    assert content["contentType"] == "HTML"
    assert "<script>" not in content["content"]
    assert "&lt;script&gt;" in content["content"]
    pixel_url = re.search(r'src="([^"]+)"', content["content"]).group(1)
    click_url = re.search(r'href="([^"]+)"', content["content"]).group(1)
    assert pixel_url.startswith("https://tracking.example.com/t/o/")
    assert click_url.startswith("https://tracking.example.com/t/c/")
    pixel_path = pixel_url.replace("https://tracking.example.com", "")
    click_path = click_url.replace("https://tracking.example.com", "")
    assert client.head(pixel_path).status_code == 200
    assert client.head(click_path).status_code == 302
    assert rows(client)[0]["open_count"] == rows(client)[0]["click_count"] == 0
    response = client.get(pixel_path)
    assert response.mimetype == "image/gif"
    assert response.data.startswith(b"GIF89a")
    assert "no-store" in response.headers["Cache-Control"]
    client.get(pixel_path)
    response = client.get(click_path)
    assert response.status_code == 302
    assert response.headers["Location"] == "https://example.com/?a=1&b=2"
    record = rows(client)[0]
    assert record["open_count"] == 2
    assert record["click_count"] == 1
    assert record["replied"] is False
    assert client.get("/t/c/missing?url=https://attacker.example").status_code == 404
    assert client.get("/t/o/missing.gif").status_code == 404


def test_open_click_signals_are_separate_for_each_recipient(client, account, microsoft_send, monkeypatch):
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://tracking.example.com")
    send(client, account, recipients="one@example.com,two@example.com")
    content = microsoft_send[0][1]["json"]["message"]["body"]["content"]
    path = re.search(r'src="https://tracking.example.com([^"]+)"', content).group(1)
    client.get(path)
    records = {record["recipient"]: record for record in rows(client)}
    assert records["one@example.com"]["opened"] is True
    assert records["two@example.com"]["opened"] is False


@pytest.mark.parametrize("url", ["http://localhost:5000", "https://bad.example/?x=1", "https://user:pass@bad.example", "invalid"])
def test_invalid_tracking_url_keeps_original_plain_text_sending(client, account, microsoft_send, monkeypatch, url):
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", url)
    send(client, account, body="Original text")
    assert microsoft_send[0][1]["json"]["message"]["body"] == {"contentType": "Text", "content": "Original text"}


def test_tracking_storage_failure_does_not_stop_microsoft_sending(client, account, microsoft_send, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError("Tracking database unavailable")

    monkeypatch.setattr(tracking, "prepare_message", fail)
    assert b"Sent 1 email(s) successfully" in send(client, account).data
    assert len(microsoft_send) == 1


@pytest.mark.parametrize("code", [401, 429, 500])
def test_failed_microsoft_requests_are_not_labelled_sent(client, account, microsoft_send, monkeypatch, code):
    monkeypatch.setattr(mail_app.requests, "post", lambda *args, **kwargs: type("Response", (), {"status_code": code})())
    send(client, account)
    assert rows(client)[0]["status"] == "failed"


def test_connection_failure_is_unknown(client, account, microsoft_send, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(mail_app.requests, "post", fail)
    send(client, account)
    assert rows(client)[0]["status"] == "unknown"


def test_normal_login_keeps_send_only_scope_and_reply_login_is_opt_in(client, monkeypatch):
    scopes = []

    class Microsoft:
        def initiate_auth_code_flow(self, requested_scopes, **kwargs):
            scopes.append(requested_scopes)
            return {"auth_uri": "https://login.microsoftonline.com/test"}

    monkeypatch.setattr(mail_app, "build_msal_app", lambda *args: Microsoft())
    assert client.get("/login").status_code == 302
    assert client.get("/login?tracking=1").status_code == 302
    assert scopes == [["Mail.Send"], ["Mail.Send", "Mail.Read"]]


def test_reply_matching_uses_real_message_headers_and_is_idempotent(client, account, monkeypatch):
    email_id = make_record(account)
    pages = [{"value": [sent_copy(email_id)]}, {"value": [reply()]}]
    calls = mock_pages(monkeypatch, pages)
    result = tracking.sync_replies(mail_app.database, account, "read-token")
    assert result["replies_added"] == 1
    assert result["complete"] is True
    assert rows(client)[0]["replied"] is True
    assert rows(client)[0]["opened"] is False
    assert all(kwargs["headers"]["Authorization"] == "Bearer read-token" for _, kwargs in calls)
    assert "body" not in calls[1][1]["params"]["$select"].split(",")
    pages.append({"value": [reply()]})
    assert tracking.sync_replies(mail_app.database, account, "read-token")["replies_added"] == 0
    assert rows(client)[0]["reply_count"] == 1


def test_same_subject_wrong_sender_autoreply_and_receipt_are_not_human_replies(client, account, monkeypatch):
    email_id = make_record(account)
    automated = reply(internetMessageHeaders=[{"name": "In-Reply-To", "value": "<sent@example.com>"},
                                             {"name": "Auto-Submitted", "value": "auto-replied"}])
    receipt = reply(internetMessageHeaders=[{"name": "In-Reply-To", "value": "<sent@example.com>"},
                                           {"name": "Content-Type", "value": "multipart/report"}])
    pages = [{"value": [sent_copy(email_id)]}, {"value": [reply("<unrelated@example.com>", subject="Re: Hello"),
              reply(**{"from": {"emailAddress": {"address": "stranger@example.com"}}}), automated, receipt]}]
    mock_pages(monkeypatch, pages)
    assert tracking.sync_replies(mail_app.database, account, "read-token")["replies_added"] == 0
    assert rows(client)[0]["replied"] is False


def test_references_header_fallback_detects_reply(client, account, monkeypatch):
    email_id = make_record(account)
    pages = [{"value": [sent_copy(email_id)]}, {"value": [reply(internetMessageHeaders=[
        {"name": "References", "value": "<older@example.com> <sent@example.com>"}])]}]
    mock_pages(monkeypatch, pages)
    assert tracking.sync_replies(mail_app.database, account, "read-token")["replies_added"] == 1


def test_mailbox_pagination_resumes_and_deduplicates(client, account, monkeypatch):
    email_id = make_record(account)
    monkeypatch.setattr(tracking, "MAX_PAGES_PER_CHECK", 1)
    pages = [{"value": [sent_copy(email_id)]}, {"value": [reply()],
              "@odata.nextLink": f"{tracking.GRAPH}/me/messages?$skip=100"}]
    calls = mock_pages(monkeypatch, pages)
    first = tracking.sync_replies(mail_app.database, account, "read-token")
    assert first["complete"] is False
    pages.append({"value": [reply()]})
    second = tracking.sync_replies(mail_app.database, account, "read-token")
    assert second["complete"] is True
    assert calls[-1][0].endswith("?$skip=100")
    assert calls[-1][1]["params"] is None
    assert rows(client)[0]["reply_count"] == 1


def test_delayed_sent_copy_rewinds_reply_scan(client, account, monkeypatch):
    first = make_record(account)
    second = make_record(account)
    old_reply = reply()
    pages = [{"value": [sent_copy(first, "<first@example.com>")]}, {"value": [old_reply]},
             {"value": [sent_copy(second)]}, {"value": [old_reply]}]
    calls = mock_pages(monkeypatch, pages)
    tracking.sync_replies(mail_app.database, account, "read-token")
    result = tracking.sync_replies(mail_app.database, account, "read-token")
    assert result["replies_added"] == 1
    assert next(record for record in rows(client) if record["email_id"] == second)["replied"] is True
    assert calls[3][1]["params"]["$filter"].startswith("receivedDateTime ge ")


def test_foreign_pagination_url_never_receives_access_token(monkeypatch):
    calls = mock_pages(monkeypatch, [])
    with pytest.raises(tracking.TrackingError):
        tracking._get_page("https://attacker.example/steal-token", "read-token")
    assert calls == []


@pytest.mark.parametrize("code", [403, 429, 500])
def test_graph_mailbox_errors_are_actionable(account, monkeypatch, code):
    make_record(account)
    monkeypatch.setattr(tracking.requests, "get", lambda *args, **kwargs: type("Response", (), {"status_code": code})())
    with pytest.raises(tracking.TrackingError):
        tracking.sync_replies(mail_app.database, account, "read-token")


def test_reply_route_requests_read_scope_only_after_manual_action(client, account, monkeypatch):
    calls = []

    def token(account_id, scopes=None):
        calls.append((account_id, scopes))
        return None, "Consent missing"

    monkeypatch.setattr(mail_app, "get_access_token_for_account", token)
    response = client.post(f"/tracking/replies/{account}")
    assert response.status_code == 403
    assert "Enable reply tracking" in response.json["error"]
    assert calls == [(account, ["Mail.Read"])]


def test_tracking_ui_and_assets_do_not_expose_tokens(client, account):
    make_record(account)
    assert b'id="tracking-table"' in client.get("/").data
    assert client.get("/static/tracking.js").status_code == 200
    response = client.get("/api/tracking")
    assert "open_token" not in response.get_data(as_text=True)
    assert "token_cache" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_public_gateway_exposes_only_tracking_routes(client, account, microsoft_send, monkeypatch):
    from werkzeug.test import Client
    from werkzeug.wrappers import Response
    from tracking_gateway import tracking_gateway

    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://tracking.example.com")
    send(client, account)
    content = microsoft_send[0][1]["json"]["message"]["body"]["content"]
    path = re.search(r'src="https://tracking.example.com([^"]+)"', content).group(1)
    gateway = Client(tracking_gateway, Response)
    assert gateway.get(path).mimetype == "image/gif"
    for blocked in ("/", "/api/tracking", "/send", "/login", "/tracking/replies/1", "/t/o/../../api/tracking"):
        assert gateway.get(blocked).status_code == 404
    assert gateway.post(path).status_code == 404


def matched_reply(account, graph_id=None):
    email_id = make_record(account)
    with mail_app.database() as connection:
        connection.execute(
            """INSERT INTO microsoft_tracking_events
               (email_id, kind, occurred_at, external_id, graph_message_id)
               VALUES (?, 'reply', ?, '<reply@example.com>', ?)""",
            (email_id, tracking.now(), graph_id),
        )
    return email_id


def reply_message(body="Thanks, please send the details."):
    return {"id": "graph-reply", "internetMessageId": "<reply@example.com>",
            "subject": "Re: Hello", "from": {"emailAddress": {"address": "one@example.com", "name": "Alex Example"}},
            "receivedDateTime": tracking.now(), "body": {"contentType": "text", "content": body}}


def test_view_reply_reads_matching_message_on_demand_without_storing_body(client, account, monkeypatch):
    email_id = matched_reply(account, "graph-reply")
    calls = mock_pages(monkeypatch, [reply_message()])
    monkeypatch.setattr(mail_app, "get_access_token_for_account", lambda account_id, scopes=None: ("read-token", None))
    response = client.get(f"/api/tracking/{email_id}/replies")
    assert response.status_code == 200
    assert response.json["replies"][0]["body"] == "Thanks, please send the details."
    assert response.json["replies"][0]["sender_name"] == "Alex Example"
    assert response.headers["Cache-Control"] == "no-store"
    assert calls[0][0].endswith("/me/messages/graph-reply")
    assert calls[0][1]["headers"]["Prefer"] == 'outlook.body-content-type="text"'
    assert rows(client)[0]["recipient_name"] == "Alex Example"
    with mail_app.database() as connection:
        assert "Thanks, please send the details." not in "\n".join(connection.iterdump())


def test_legacy_reply_can_be_opened_using_its_internet_message_id(account, monkeypatch):
    email_id = matched_reply(account)
    calls = mock_pages(monkeypatch, [{"value": [reply_message()]}])
    result = tracking.load_replies(mail_app.database, email_id, "read-token")
    assert result["replies"][0]["unavailable"] is False
    assert calls[0][1]["params"]["$filter"] == "internetMessageId eq '<reply@example.com>'"
    with mail_app.database() as connection:
        assert connection.execute("SELECT graph_message_id FROM microsoft_tracking_events WHERE email_id = ?", (email_id,)).fetchone()[0] == "graph-reply"


def test_moved_reply_falls_back_to_stable_message_id(account, monkeypatch):
    email_id = matched_reply(account, "old-folder-id")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return type("Response", (), {"status_code": 404})()
        return type("Response", (), {"status_code": 200, "json": lambda self: {"value": [reply_message()]}})()

    monkeypatch.setattr(tracking.requests, "get", get)
    assert tracking.load_replies(mail_app.database, email_id, "read-token")["replies"][0]["body"]
    assert len(calls) == 2


def test_deleted_reply_keeps_received_status_and_shows_unavailable(client, account, monkeypatch):
    email_id = matched_reply(account)
    mock_pages(monkeypatch, [{"value": []}])
    result = tracking.load_replies(mail_app.database, email_id, "read-token")
    assert result["replies"][0]["unavailable"] is True
    assert rows(client)[0]["replied"] is True


def test_reply_html_is_converted_to_text_and_does_not_load_remote_content(account, monkeypatch):
    email_id = matched_reply(account, "graph-reply")
    message = reply_message()
    message["body"] = {"contentType": "html", "content": '<style>secret-style</style><script>alert("bad")</script><p>Hello &amp; welcome</p><img src="https://tracking.example/pixel">'}
    mock_pages(monkeypatch, [message])
    body = tracking.load_replies(mail_app.database, email_id, "read-token")["replies"][0]["body"]
    assert body == "Hello & welcome"


@pytest.mark.parametrize("mismatch", ["sender", "message_id"])
def test_reply_view_rejects_an_unrelated_mailbox_message(account, monkeypatch, mismatch):
    email_id = matched_reply(account, "graph-reply")
    message = reply_message()
    if mismatch == "sender":
        message["from"]["emailAddress"]["address"] = "other@example.com"
    else:
        message["internetMessageId"] = "<different@example.com>"
    mock_pages(monkeypatch, [message])
    with pytest.raises(tracking.TrackingError, match="does not match"):
        tracking.load_replies(mail_app.database, email_id, "read-token")


def test_reply_view_handles_missing_consent_and_unknown_email(client, account, monkeypatch):
    email_id = matched_reply(account)
    monkeypatch.setattr(mail_app, "get_access_token_for_account", lambda account_id, scopes=None: (None, "Consent missing"))
    assert client.get(f"/api/tracking/{email_id}/replies").status_code == 403
    response = client.get("/api/tracking/unknown/replies")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"


def test_repeated_reply_checks_show_existing_total_instead_of_implying_none(client, account, monkeypatch):
    email_id = make_record(account)
    message = reply(**{"from": {"emailAddress": {"address": "one@example.com", "name": "Alex Example"}}})
    pages = [{"value": [sent_copy(email_id)]}, {"value": [message]}, {"value": [message]}]
    mock_pages(monkeypatch, pages)
    tracking.sync_replies(mail_app.database, account, "read-token")
    result = tracking.sync_replies(mail_app.database, account, "read-token")
    assert result["replies_added"] == 0
    assert result["total_replies"] == result["replied_emails"] == 1
    assert "1 reply already found" in result["message"]
    assert rows(client)[0]["recipient_name"] == "Alex Example"


def test_known_recipient_name_applies_to_older_emails_to_the_same_address(client, account):
    first = make_record(account)
    make_record(account)
    with mail_app.database() as connection:
        connection.execute("UPDATE microsoft_email_tracking SET recipient_name = 'Alex Example' WHERE email_id = ?", (first,))
    assert all(record["recipient_name"] == "Alex Example" for record in rows(client))


def test_upgrade_preserves_existing_tracking_records_and_reply_events(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE microsoft_email_tracking (
                email_id TEXT PRIMARY KEY, account_id INTEGER, sender TEXT, recipient TEXT,
                subject TEXT, status TEXT, created_at TEXT, sent_at TEXT, open_token TEXT,
                open_click_enabled INTEGER, internet_message_id TEXT, error TEXT
            );
            CREATE TABLE microsoft_tracking_events (
                id INTEGER PRIMARY KEY, email_id TEXT, kind TEXT, occurred_at TEXT, url TEXT,
                external_id TEXT, UNIQUE(email_id, kind, external_id)
            );
            INSERT INTO microsoft_email_tracking (email_id, account_id, recipient, subject)
            VALUES ('existing', 1, 'one@example.com', 'Existing subject');
            INSERT INTO microsoft_tracking_events (email_id, kind, external_id)
            VALUES ('existing', 'reply', '<reply@example.com>');
        """)
        tracking.init_tables(connection)
        assert connection.execute("SELECT recipient, recipient_name FROM microsoft_email_tracking").fetchone() == ("one@example.com", None)
        assert connection.execute("SELECT external_id, graph_message_id FROM microsoft_tracking_events").fetchone() == ("<reply@example.com>", None)


def test_successful_send_returns_to_tracking_section(client, account, microsoft_send):
    response = client.post("/send", data={"sender_account": account, "recipients": "one@example.com", "subject": "Hello", "body": "Hi"})
    assert response.headers["Location"].endswith("#email-tracking")


def test_offline_tracking_preserves_original_message_and_still_sends(client, account, microsoft_send, monkeypatch):
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://expired.trycloudflare.com")
    monkeypatch.setattr(tracking, "endpoint_health", lambda *args, **kwargs: {"state": "offline"})
    original = "Read more: https://example.com/?a=1&b=2"
    response = send(client, account, body=original)
    assert b"Sent 1 email(s) successfully" in response.data
    message = microsoft_send[0][1]["json"]["message"]
    assert message["body"] == {"contentType": "Text", "content": original}
    assert len(microsoft_send) == 1
    record = rows(client)[0]
    assert record["status"] == "accepted"
    assert record["open_click_enabled"] == 0
    assert record["tracking_base_url"] == "https://expired.trycloudflare.com"
    assert "unavailable" in record["tracking_note"]
    with mail_app.database() as connection:
        assert connection.execute("SELECT COUNT(*) FROM microsoft_tracking_links").fetchone()[0] == 0


def test_connection_is_checked_once_for_a_bulk_send(client, account, microsoft_send, monkeypatch):
    checks = []
    monkeypatch.setattr(tracking, "endpoint_health", lambda *args, **kwargs: checks.append(kwargs) or {"state": "offline"})
    send(client, account, recipients="one@example.com,two@example.com")
    assert checks == [{"force": True}]
    assert len(microsoft_send) == 2


def test_health_probe_uses_dedicated_endpoint_and_does_not_record_opens(client, account, monkeypatch):
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://tracking.example.com")
    make_record(account)
    calls = mock_pages(monkeypatch, [{"service": "microsoft-email-tracking", "status": "ok"}])
    health = tracking.endpoint_health()
    assert health["state"] == "online"
    assert calls[0][0] == "https://tracking.example.com/t/health"
    assert calls[0][1]["allow_redirects"] is False
    assert tracking.endpoint_health()["state"] == "online"
    assert len(calls) == 1
    assert rows(client)[0]["open_count"] == 0


@pytest.mark.parametrize("failure", ["html", "unavailable", "network"])
def test_unexpected_pages_and_connection_errors_are_not_reported_healthy(monkeypatch, failure):
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://tracking.example.com")

    def get(*args, **kwargs):
        if failure == "network":
            raise requests.ConnectionError()
        class Response:
            status_code = 530 if failure == "unavailable" else 200

            def json(self):
                raise ValueError("HTML browser warning page")
        return Response()

    monkeypatch.setattr(tracking.requests, "get", get)
    assert tracking.endpoint_health()["state"] == "offline"


def test_health_endpoint_is_public_but_its_probes_do_not_change_recipient_counts(client, account):
    from tracking_gateway import tracking_gateway
    from werkzeug.test import Client
    from werkzeug.wrappers import Response

    make_record(account)
    gateway = Client(tracking_gateway, Response)
    assert gateway.get("/t/health").json == {"service": "microsoft-email-tracking", "status": "ok"}
    assert gateway.head("/t/health").status_code == 200
    assert gateway.get("/api/tracking/health").status_code == 404
    assert rows(client)[0]["open_count"] == 0


def test_health_api_distinguishes_an_old_dead_url_from_the_new_working_url(client, account, monkeypatch):
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://old.trycloudflare.com")
    email_id = make_record(account)
    monkeypatch.setenv("TRACKING_PUBLIC_BASE_URL", "https://new.trycloudflare.com")
    mock_pages(monkeypatch, [{"service": "microsoft-email-tracking", "status": "ok"}])
    # The old endpoint's HTTP error is separate from the new endpoint's response.
    original_get = tracking.requests.get

    def get(url, **kwargs):
        if url.startswith("https://old."):
            return type("Response", (), {"status_code": 530})()
        return original_get(url, **kwargs)

    monkeypatch.setattr(tracking.requests, "get", get)
    result = client.get("/api/tracking/health")
    assert result.status_code == 200
    assert result.json["current"]["state"] == "online"
    assert result.json["endpoints"]["https://old.trycloudflare.com"]["state"] == "offline"
    assert result.json["unavailable_emails"] == 1
    assert rows(client)[0]["open_count"] == 0
