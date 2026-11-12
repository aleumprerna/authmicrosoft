import app as mail_app
import pytest


@pytest.fixture(autouse=True)
def temporary_database(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_app, "DATABASE_PATH", tmp_path / "test-accounts.db")
    mail_app.init_database()


def add_test_account():
    with mail_app.database() as connection:
        cursor = connection.execute(
            """
            INSERT INTO microsoft_accounts
                (home_account_id, email, display_name, token_cache, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("test-home-id", "sender@example.com", "Test Sender", b"encrypted", "now"),
        )
        return cursor.lastrowid


def test_home_page_signed_out():
    client = mail_app.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Add Microsoft account" in response.data


def test_send_requires_login():
    client = mail_app.app.test_client()
    response = client.post(
        "/send",
        data={"recipients": "person@example.com", "subject": "Hello", "body": "Hi"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_send_mail_success(monkeypatch):
    class Response:
        status_code = 202

    monkeypatch.setattr(mail_app.requests, "post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(
        mail_app, "get_access_token_for_account", lambda account_id: ("test-token", None)
    )
    account_id = add_test_account()
    client = mail_app.app.test_client()

    response = client.post(
        "/send",
        data={
            "sender_account": account_id,
            "recipients": "person@example.com",
            "subject": "Hello",
            "body": "Hi",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Sent 1 email(s) successfully" in response.data


def test_bulk_send_deduplicates_recipients(monkeypatch):
    sent_to = []

    class Response:
        status_code = 202

    def fake_post(*args, **kwargs):
        sent_to.append(kwargs["json"]["message"]["toRecipients"][0]["emailAddress"]["address"])
        return Response()

    monkeypatch.setattr(mail_app.requests, "post", fake_post)
    monkeypatch.setattr(
        mail_app, "get_access_token_for_account", lambda account_id: ("test-token", None)
    )
    account_id = add_test_account()
    client = mail_app.app.test_client()

    response = client.post(
        "/send",
        data={
            "sender_account": account_id,
            "recipients": "one@example.com, TWO@example.com\none@example.com",
            "subject": "Bulk hello",
            "body": "Hi everyone",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert sent_to == ["one@example.com", "TWO@example.com"]
    assert b"Sent 2 email(s) successfully" in response.data
