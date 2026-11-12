import os
import re
import base64
import hashlib
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import msal
import requests
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

load_dotenv()

PORT = int(os.getenv("PORT", "8000"))

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")
REDIRECT_URI = os.getenv(
    "MICROSOFT_REDIRECT_URI", f"http://localhost:{PORT}/auth/callback"
)
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["Mail.Send"]
GRAPH_SEND_MAIL = "https://graph.microsoft.com/v1.0/me/sendMail"
MAX_RECIPIENTS = 50
EMAIL_PATTERN = re.compile(r"^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", Path(__file__).with_name("accounts.db")))


def token_cipher():
    encryption_secret = os.getenv("TOKEN_ENCRYPTION_KEY", app.secret_key)
    key = base64.urlsafe_b64encode(hashlib.sha256(encryption_secret.encode()).digest())
    return Fernet(key)


def database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    with database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS microsoft_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                home_account_id TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                display_name TEXT NOT NULL,
                token_cache BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def list_saved_accounts():
    with database() as connection:
        return connection.execute(
            "SELECT id, email, display_name FROM microsoft_accounts ORDER BY email"
        ).fetchall()


def save_account(account, claims, cache):
    email = claims.get("preferred_username") or claims.get("email") or account.get("username")
    display_name = claims.get("name") or email
    encrypted_cache = token_cipher().encrypt(cache.serialize().encode())
    with database() as connection:
        connection.execute(
            """
            INSERT INTO microsoft_accounts
                (home_account_id, email, display_name, token_cache, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(home_account_id) DO UPDATE SET
                email = excluded.email,
                display_name = excluded.display_name,
                token_cache = excluded.token_cache,
                updated_at = excluded.updated_at
            """,
            (
                account["home_account_id"],
                email,
                display_name,
                encrypted_cache,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

def get_access_token_for_account(account_id):
    with database() as connection:
        saved = connection.execute(
            "SELECT * FROM microsoft_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    if not saved:
        return None, "The selected Microsoft account was not found."

    try:
        serialized_cache = token_cipher().decrypt(saved["token_cache"]).decode()
    except InvalidToken:
        return None, "The saved account token cannot be decrypted. Add the account again."

    cache = msal.SerializableTokenCache()
    cache.deserialize(serialized_cache)
    msal_app = build_msal_app(cache)
    matching_accounts = [
        item
        for item in msal_app.get_accounts()
        if item.get("home_account_id") == saved["home_account_id"]
    ]
    if not matching_accounts:
        return None, "This account needs to be added again."

    result = msal_app.acquire_token_silent(SCOPES, account=matching_accounts[0])
    if cache.has_state_changed:
        encrypted_cache = token_cipher().encrypt(cache.serialize().encode())
        with database() as connection:
            connection.execute(
                "UPDATE microsoft_accounts SET token_cache = ?, updated_at = ? WHERE id = ?",
                (encrypted_cache, datetime.now(timezone.utc).isoformat(), account_id),
            )
    if result and "access_token" in result:
        return result["access_token"], None
    return None, "Microsoft access expired or was revoked. Add this account again."


def build_msal_app(cache=None):
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
        token_cache=cache,
    )


def parse_recipients(raw_recipients):
    """Parse newline, comma, or semicolon-separated addresses and deduplicate them."""
    recipients = []
    seen = set()
    for address in re.split(r"[,;\r\n]+", raw_recipients):
        address = address.strip()
        normalized = address.lower()
        if address and normalized not in seen:
            recipients.append(address)
            seen.add(normalized)
    return recipients


def configured(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not CLIENT_ID or not CLIENT_SECRET:
            return render_template("configuration_error.html"), 500
        return view(*args, **kwargs)

    return wrapped


@app.get("/")
def index():
    return render_template("index.html", accounts=list_saved_accounts())


@app.get("/login")
@configured
def login():
    flow = build_msal_app().initiate_auth_code_flow(
        SCOPES,
        redirect_uri=REDIRECT_URI,
        prompt="select_account",
    )
    session["auth_flow"] = flow
    return redirect(flow["auth_uri"])


@app.get("/auth/callback")
@configured
def authorized():
    flow = session.pop("auth_flow", None)
    if not flow:
        flash("Your sign-in session expired. Please try again.", "error")
        return redirect(url_for("index"))

    cache = msal.SerializableTokenCache()
    msal_app = build_msal_app(cache)
    try:
        result = msal_app.acquire_token_by_auth_code_flow(
            flow, request.args.to_dict()
        )
    except ValueError:
        flash("Microsoft sign-in validation failed. Please try again.", "error")
        return redirect(url_for("index"))

    if "access_token" not in result:
        message = result.get("error_description", result.get("error", "Sign-in failed"))
        flash(message, "error")
        return redirect(url_for("index"))

    claims = result.get("id_token_claims", {})
    accounts = msal_app.get_accounts(username=claims.get("preferred_username"))
    if not accounts:
        accounts = msal_app.get_accounts()
    if not accounts:
        flash("Microsoft account information could not be saved.", "error")
        return redirect(url_for("index"))
    save_account(accounts[0], claims, cache)
    flash(f"Microsoft account {claims.get('preferred_username', '')} saved.", "success")
    return redirect(url_for("index"))


@app.post("/send")
def send_mail():
    account_id = request.form.get("sender_account", type=int)
    if not account_id:
        flash("Choose a sender Microsoft account.", "error")
        return redirect(url_for("index"))
    access_token, token_error = get_access_token_for_account(account_id)
    if not access_token:
        flash(token_error, "error")
        return redirect(url_for("index"))

    recipients = parse_recipients(request.form.get("recipients", ""))
    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    if not recipients or not subject or not body:
        flash("At least one recipient, a subject, and a message are required.", "error")
        return redirect(url_for("index"))
    if len(recipients) > MAX_RECIPIENTS:
        flash(f"You can send to at most {MAX_RECIPIENTS} recipients at one time.", "error")
        return redirect(url_for("index"))
    invalid = [address for address in recipients if not EMAIL_PATTERN.match(address)]
    if invalid:
        flash(f"Invalid email address: {invalid[0]}", "error")
        return redirect(url_for("index"))

    sent = 0
    failed = []
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    for recipient in recipients:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            },
            "saveToSentItems": True,
        }
        try:
            response = requests.post(
                GRAPH_SEND_MAIL, headers=headers, json=payload, timeout=30
            )
        except requests.RequestException:
            failed.append(recipient)
            continue

        if response.status_code == 202:
            sent += 1
        elif response.status_code == 401:
            flash(
                f"Your Microsoft session expired after sending {sent} email(s). "
                "Add this Microsoft account again.",
                "error",
            )
            return redirect(url_for("index"))
        else:
            failed.append(recipient)

    if sent:
        flash(f"Sent {sent} email(s) successfully.", "success")
    if failed:
        preview = ", ".join(failed[:5])
        suffix = "…" if len(failed) > 5 else ""
        flash(f"Failed to send to {len(failed)} recipient(s): {preview}{suffix}", "error")

    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    session.clear()
    post_logout = url_for("index", _external=True)
    return redirect(
        f"{AUTHORITY}/oauth2/v2.0/logout?post_logout_redirect_uri={post_logout}"
    )


init_database()


if __name__ == "__main__":
    app.run(host="localhost", port=PORT, debug=True)
