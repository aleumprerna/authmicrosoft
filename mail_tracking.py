"""Optional open/click tracking and reply matching for Microsoft-sent email."""

import html
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit
from uuid import uuid4

import requests


GRAPH = "https://graph.microsoft.com/v1.0"
TRACKING_HEADER = "x-app-email-id"
MAX_PAGES_PER_CHECK = 5
_HEALTH_CACHE = {}


class TrackingError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def public_base_url(value=None):
    value = (os.getenv("TRACKING_PUBLIC_BASE_URL", "") if value is None else value).strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate a configured port, if present.
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment):
            return value
    except ValueError:
        pass
    return ""


def endpoint_health(base_url=None, force=False):
    """Probe a non-counting public endpoint, never a real recipient's pixel."""
    base_url = public_base_url(base_url)
    result = {"url": base_url, "checked_at": now(), "state": "unconfigured",
              "temporary": bool(base_url and urlsplit(base_url).hostname.endswith(".trycloudflare.com"))}
    if not base_url:
        return {**result, "reason": "No valid public HTTPS tracking URL is configured."}
    cached = _HEALTH_CACHE.get(base_url)
    if not force and cached and time.monotonic() - cached[0] < 15:
        return dict(cached[1])
    result.update(state="offline", reason="The public tracking address cannot be reached.")
    try:
        response = requests.get(
            base_url + "/t/health", timeout=(2, 3), allow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 EmailTrackingHealthCheck", "Accept": "application/json"},
        )
        if response.status_code == 200 and response.json() == {"service": "microsoft-email-tracking", "status": "ok"}:
            result.update(state="online", reason="The public tracking endpoint is reachable.")
        else:
            result["reason"] = f"The tracking endpoint is unavailable or returned an unexpected page (HTTP {response.status_code})."
    except (requests.RequestException, ValueError):
        pass
    _HEALTH_CACHE[base_url] = (time.monotonic(), result)
    return dict(result)


def tracking_health(database):
    current = endpoint_health()
    with database() as connection:
        # Bound external probes; recent local test addresses are the most relevant.
        rows = connection.execute(
            """SELECT tracking_base_url, COUNT(*) AS emails, MAX(created_at) AS latest
               FROM microsoft_email_tracking WHERE open_click_enabled = 1 AND tracking_base_url IS NOT NULL
               GROUP BY tracking_base_url ORDER BY latest DESC LIMIT 4"""
        ).fetchall()
    endpoints = {current["url"]: current} if current["url"] else {}
    unavailable = 0
    for row in rows:
        health = endpoints.get(row["tracking_base_url"]) or endpoint_health(row["tracking_base_url"])
        endpoints[row["tracking_base_url"]] = health
        if health["state"] != "online":
            unavailable += row["emails"]
    return {"current": current, "endpoints": endpoints, "unavailable_emails": unavailable}


def init_tables(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS microsoft_email_tracking (
            email_id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            recipient_name TEXT,
            tracking_base_url TEXT,
            tracking_note TEXT,
            subject TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            open_token TEXT NOT NULL UNIQUE,
            open_click_enabled INTEGER NOT NULL,
            internet_message_id TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS microsoft_tracking_account
            ON microsoft_email_tracking(account_id, created_at);
        CREATE TABLE IF NOT EXISTS microsoft_tracking_links (
            token TEXT PRIMARY KEY,
            email_id TEXT NOT NULL,
            destination TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS microsoft_tracking_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            url TEXT,
            external_id TEXT,
            graph_message_id TEXT,
            UNIQUE(email_id, kind, external_id)
        );
        CREATE INDEX IF NOT EXISTS microsoft_tracking_event_email
            ON microsoft_tracking_events(email_id, occurred_at);
        CREATE TABLE IF NOT EXISTS microsoft_tracking_sync (
            account_id INTEGER PRIMARY KEY,
            sent_next_link TEXT,
            replies_next_link TEXT,
            replies_since TEXT,
            replies_until TEXT,
            last_checked_at TEXT
        );
        """
    )
    # Existing tracking records and reply matches remain intact on upgrade.
    connection.execute("BEGIN IMMEDIATE")
    for table, column in (("microsoft_email_tracking", "recipient_name"),
                          ("microsoft_email_tracking", "tracking_base_url"),
                          ("microsoft_email_tracking", "tracking_note"),
                          ("microsoft_tracking_events", "graph_message_id")):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def prepare_message(database, account_id, recipient, subject, body, health=None):
    """Add tracking around the same Graph message; never send through another provider."""
    email_id, open_token = str(uuid4()), secrets.token_urlsafe(32)
    configured_url = public_base_url()
    # The sending route provides one fresh preflight result for the whole batch.
    # Keep original links and text when tracking is offline; Microsoft still sends.
    base_url = configured_url if health is None or health.get("state") == "online" else ""
    tracking_note = "Open/click tracking was unavailable when this email was sent. Original links were preserved." if configured_url and not base_url else None
    message = {"subject": subject, "body": {"contentType": "Text", "content": body},
               "toRecipients": [{"emailAddress": {"address": recipient}}],
               "internetMessageHeaders": [{"name": TRACKING_HEADER, "value": email_id}]}
    with database() as connection:
        account = connection.execute(
            "SELECT email FROM microsoft_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not account:
            raise TrackingError("Sender account not found.")
        connection.execute(
            """INSERT INTO microsoft_email_tracking
               (email_id, account_id, sender, recipient, subject, created_at,
                open_token, open_click_enabled, tracking_base_url, tracking_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (email_id, account_id, account["email"], recipient, subject, now(), open_token,
             bool(base_url), configured_url or None, tracking_note),
        )
        if base_url:
            # Keep the plain-text form. Escape its content, then link HTTP(S) URLs.
            parts, start = [], 0
            for match in re.finditer(r'https?://[^\s<>"\']+', body):
                destination = match.group()
                try:
                    if not urlsplit(destination).hostname:
                        continue
                except ValueError:
                    continue
                token = secrets.token_urlsafe(32)
                connection.execute(
                    "INSERT INTO microsoft_tracking_links VALUES (?, ?, ?)",
                    (token, email_id, destination),
                )
                parts.append(html.escape(body[start:match.start()]))
                parts.append(f'<a href="{html.escape(base_url, quote=True)}/t/c/{token}">'
                             f'{html.escape(destination)}</a>')
                start = match.end()
            parts.append(html.escape(body[start:]))
            content = "".join(parts).replace("\n", "<br>\n")
            content += (f'<img src="{html.escape(base_url, quote=True)}/t/o/{open_token}.gif" '
                        'width="1" height="1" alt="" style="border:0">')
            message["body"] = {"contentType": "HTML", "content": f"<html><body>{content}</body></html>"}
    return email_id, message


def finish_message(database, email_id, status, error=None):
    with database() as connection:
        connection.execute(
            "UPDATE microsoft_email_tracking SET status = ?, sent_at = ?, error = ? WHERE email_id = ?",
            (status, now() if status == "accepted" else None, error, email_id),
        )


def record_open(database, token, count=True):
    with database() as connection:
        record = connection.execute(
            "SELECT email_id FROM microsoft_email_tracking WHERE open_token = ? AND open_click_enabled = 1",
            (token,),
        ).fetchone()
        if record and count:
            connection.execute(
                "INSERT INTO microsoft_tracking_events (email_id, kind, occurred_at) VALUES (?, 'open', ?)",
                (record["email_id"], now()),
            )
    return bool(record)


def record_click(database, token, count=True):
    with database() as connection:
        link = connection.execute(
            "SELECT email_id, destination FROM microsoft_tracking_links WHERE token = ?", (token,)
        ).fetchone()
        if link and count:
            connection.execute(
                "INSERT INTO microsoft_tracking_events (email_id, kind, occurred_at, url) VALUES (?, 'click', ?, ?)",
                (link["email_id"], now(), link["destination"]),
            )
    return link["destination"] if link else None


def tracking_data(database):
    with database() as connection:
        rows = connection.execute(
            """SELECT m.email_id, m.account_id, m.sender, m.recipient, m.recipient_name, m.subject, m.status,
                      m.sent_at, m.created_at, m.open_click_enabled, m.error, m.tracking_base_url, m.tracking_note,
                      (m.internet_message_id IS NOT NULL) AS reply_match_ready,
                      SUM(CASE WHEN e.kind = 'open' THEN 1 ELSE 0 END) AS open_count,
                      SUM(CASE WHEN e.kind = 'click' THEN 1 ELSE 0 END) AS click_count,
                      SUM(CASE WHEN e.kind = 'reply' THEN 1 ELSE 0 END) AS reply_count,
                      MIN(CASE WHEN e.kind = 'open' THEN e.occurred_at END) AS first_opened_at,
                      MAX(CASE WHEN e.kind = 'reply' THEN e.occurred_at END) AS last_reply_at,
                      MAX(e.occurred_at) AS last_event_at
               FROM microsoft_email_tracking m
               LEFT JOIN microsoft_tracking_events e ON e.email_id = m.email_id
               GROUP BY m.email_id ORDER BY m.created_at DESC"""
        ).fetchall()
        messages = [dict(row) for row in rows]
        known_names = {(item["account_id"], item["recipient"].lower()): item["recipient_name"]
                       for item in reversed(messages) if item["recipient_name"]}
        for message in messages:
            message["recipient_name"] = message["recipient_name"] or known_names.get(
                (message["account_id"], message["recipient"].lower()))
            message.update(provider="microsoft", opened=bool(message["open_count"]),
                           clicked=bool(message["click_count"]), replied=bool(message["reply_count"]))
        checks = [dict(row) for row in connection.execute(
            "SELECT account_id, last_checked_at FROM microsoft_tracking_sync WHERE last_checked_at IS NOT NULL"
        )]
    return {"messages": messages, "reply_checks": checks,
            "summary": {"total": len(messages), "accepted": sum(m["status"] == "accepted" for m in messages),
                        "opened": sum(m["opened"] for m in messages), "clicked": sum(m["clicked"] for m in messages),
                        "replied": sum(m["replied"] for m in messages)}}


def _get_page(url, token, params=None):
    # Do not forward the Microsoft access token to a foreign pagination URL.
    try:
        parsed = urlsplit(url)
        valid_url = (parsed.scheme == "https" and parsed.netloc == "graph.microsoft.com"
                     and parsed.path.startswith("/v1.0/"))
    except (ValueError, TypeError):
        valid_url = False
    if not valid_url:
        raise TrackingError("Microsoft returned an invalid pagination URL.")
    try:
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
        if response.status_code in {401, 403}:
            raise TrackingError("Enable reply tracking for this Microsoft account and grant Mail.Read permission.")
        if response.status_code == 429:
            raise TrackingError("Microsoft is rate limiting mailbox checks. Wait and check again.")
        if response.status_code != 200:
            raise TrackingError(f"Microsoft mailbox check failed (HTTP {response.status_code}).")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("value"), list):
            raise TrackingError("Microsoft returned an unexpected mailbox response.")
        return data
    except (requests.RequestException, ValueError):
        raise TrackingError("Could not read the Microsoft mailbox. Try again later.") from None


def _headers(message):
    return {header["name"].lower(): header.get("value", "")
            for header in (message.get("internetMessageHeaders") or []) if isinstance(header, dict) and "name" in header}


def _overlap(value):
    return (datetime.fromisoformat(value.replace("Z", "+00:00")) - timedelta(minutes=2)).isoformat()


def sync_replies(database, account_id, access_token):
    """Match real reply headers, not subjects or Sent Items' isRead flag.

    Pagination checkpoints bound each check and allow the next check to resume.
    No mailbox messages are changed and no reply bodies are read or stored.
    """
    with database() as connection:
        records = [dict(row) for row in connection.execute(
            "SELECT * FROM microsoft_email_tracking WHERE account_id = ? AND status IN ('accepted', 'unknown')",
            (account_id,),
        )]
        connection.execute("INSERT OR IGNORE INTO microsoft_tracking_sync (account_id) VALUES (?)", (account_id,))
        state = dict(connection.execute("SELECT * FROM microsoft_tracking_sync WHERE account_id = ?", (account_id,)).fetchone())
    if not records:
        return {"replies_added": 0, "complete": True, "message": "No tracked Microsoft emails for this account yet."}

    unlinked = {record["email_id"]: record for record in records if not record["internet_message_id"]}
    if unlinked:
        since = _overlap(min(record["created_at"] for record in unlinked.values()))
        url = state["sent_next_link"] or f"{GRAPH}/me/mailFolders/sentitems/messages"
        params = None if state["sent_next_link"] else {
            "$filter": f"sentDateTime ge {since}", "$orderby": "sentDateTime asc", "$top": 100,
            "$select": "id,internetMessageId,internetMessageHeaders,toRecipients,sentDateTime",
        }
        for _ in range(MAX_PAGES_PER_CHECK):
            data = _get_page(url, access_token, params)
            linked_dates = []
            with database() as connection:
                for message in data["value"]:
                    email_id = _headers(message).get(TRACKING_HEADER)
                    local = unlinked.get(email_id)
                    addresses = {item.get("emailAddress", {}).get("address", "").lower()
                                 for item in message.get("toRecipients", [])}
                    if local and local["recipient"].lower() in addresses and message.get("internetMessageId"):
                        name = next((item.get("emailAddress", {}).get("name")
                                     for item in message.get("toRecipients", [])
                                     if item.get("emailAddress", {}).get("address", "").lower() == local["recipient"].lower()), None)
                        connection.execute(
                            """UPDATE microsoft_email_tracking SET internet_message_id = ?,
                               recipient_name = COALESCE(NULLIF(?, ''), recipient_name)
                               WHERE email_id = ? AND account_id = ?""",
                            (message["internetMessageId"], name, email_id, account_id),
                        )
                        local["internet_message_id"] = message["internetMessageId"]
                        linked_dates.append(_overlap(local["created_at"]))
                if linked_dates:
                    # A late Sent Items copy can become available after an earlier check.
                    # Rewind replies so those messages are not silently missed.
                    state["replies_since"] = min(linked_dates + [state["replies_since"] or since])
                    state["replies_next_link"], state["replies_until"] = None, None
                    connection.execute(
                        "UPDATE microsoft_tracking_sync SET replies_since = ?, replies_next_link = NULL, replies_until = NULL WHERE account_id = ?",
                        (state["replies_since"], account_id),
                    )
                next_link = data.get("@odata.nextLink")
                connection.execute("UPDATE microsoft_tracking_sync SET sent_next_link = ? WHERE account_id = ?", (next_link, account_id))
            if not next_link:
                break
            url, params = next_link, None
        else:
            return {"replies_added": 0, "complete": False,
                    "message": "More Sent Items to scan. Check replies again to continue."}

    matched = {record["internet_message_id"]: record for record in records if record["internet_message_id"]}
    if not matched:
        return {"replies_added": 0, "complete": False,
                "message": "Waiting to match tracked emails in Sent Items. Check again after Microsoft saves them."}
    since = state["replies_since"] or _overlap(min(record["created_at"] for record in records))
    until = state["replies_until"] or now()
    url = state["replies_next_link"] or f"{GRAPH}/me/messages"
    params = None if state["replies_next_link"] else {
        "$filter": f"receivedDateTime ge {since} and receivedDateTime le {until}",
        "$orderby": "receivedDateTime asc", "$top": 100,
        "$select": "id,internetMessageId,internetMessageHeaders,from,receivedDateTime,isDraft",
    }
    added = 0
    for _ in range(MAX_PAGES_PER_CHECK):
        data = _get_page(url, access_token, params)
        with database() as connection:
            for message in data["value"]:
                headers = _headers(message)
                if message.get("isDraft") or headers.get("auto-submitted", "no").lower() != "no":
                    continue
                if "report" in headers.get("content-type", "").lower():
                    continue
                references = re.findall(r"<[^<>\s]+>", headers.get("in-reply-to", ""))
                if not any(ref in matched for ref in references):
                    references = list(reversed(re.findall(r"<[^<>\s]+>", headers.get("references", ""))))
                sender = (message.get("from") or {}).get("emailAddress", {}).get("address", "").lower()
                local = next((matched[ref] for ref in references if ref in matched
                              and matched[ref]["recipient"].lower() == sender), None)
                external_id = message.get("internetMessageId") or message.get("id")
                received = message.get("receivedDateTime")
                if not local or not external_id or not received:
                    continue
                try:
                    occurred = datetime.fromisoformat(received.replace("Z", "+00:00"))
                    if occurred.tzinfo is None or occurred < datetime.fromisoformat(local["created_at"]):
                        continue
                except (ValueError, TypeError):
                    continue
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO microsoft_tracking_events
                       (email_id, kind, occurred_at, external_id, graph_message_id)
                       VALUES (?, 'reply', ?, ?, ?)""",
                    (local["email_id"], occurred.astimezone(timezone.utc).isoformat(), external_id, message.get("id")),
                )
                added += cursor.rowcount
                connection.execute(
                    """UPDATE microsoft_tracking_events SET graph_message_id = COALESCE(?, graph_message_id)
                       WHERE email_id = ? AND kind = 'reply' AND external_id = ?""",
                    (message.get("id"), local["email_id"], external_id),
                )
                sender_name = (message.get("from") or {}).get("emailAddress", {}).get("name")
                if sender_name:
                    connection.execute("UPDATE microsoft_email_tracking SET recipient_name = ? WHERE email_id = ?",
                                       (sender_name, local["email_id"]))
            next_link = data.get("@odata.nextLink")
            connection.execute(
                """UPDATE microsoft_tracking_sync
                   SET replies_next_link = ?, replies_since = ?, replies_until = ?, last_checked_at = ?
                   WHERE account_id = ?""",
                (next_link, since if next_link else _overlap(until), until if next_link else None,
                 None if next_link else now(), account_id),
            )
        if not next_link:
            with database() as connection:
                totals = connection.execute(
                    """SELECT COUNT(*) AS replies, COUNT(DISTINCT e.email_id) AS emails
                       FROM microsoft_tracking_events e JOIN microsoft_email_tracking m ON m.email_id = e.email_id
                       WHERE m.account_id = ? AND e.kind = 'reply'""", (account_id,),
                ).fetchone()
            total_label = "reply" if totals["replies"] == 1 else "replies"
            new_label = "reply" if added == 1 else "replies"
            return {"replies_added": added, "complete": True,
                    "total_replies": totals["replies"], "replied_emails": totals["emails"],
                    "unmatched_sent": sum(not record["internet_message_id"] for record in records),
                    "message": (f"{added} new {new_label}. {totals['replies']} {total_label} found in total."
                                if added else f"No new replies. {totals['replies']} {total_label} already found.")}
        url, params = next_link, None
    return {"replies_added": added, "complete": False,
            "message": "More mailbox messages to scan. Check replies again to continue."}


class _ReplyText(HTMLParser):
    """Convert fallback HTML to display text without executing or loading content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden_depth = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        if not self.hidden_depth and tag in {"br", "p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif not self.hidden_depth and tag in {"p", "div", "li", "tr", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden_depth:
            self.parts.append(data)


def _reply_request(url, access_token, params):
    try:
        response = requests.get(
            url, headers={"Authorization": f"Bearer {access_token}",
                          "Prefer": 'outlook.body-content-type="text"'},
            params=params, timeout=30,
        )
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise TrackingError("Enable reply tracking for this sender to view its replies.")
        if response.status_code == 429:
            raise TrackingError("Microsoft is rate limiting requests. Please try again shortly.")
        if response.status_code != 200:
            raise TrackingError(f"Could not load the reply (Microsoft HTTP {response.status_code}).")
        data = response.json()
        if not isinstance(data, dict):
            raise TrackingError("Microsoft returned an unexpected reply response.")
        return data
    except (requests.RequestException, ValueError):
        raise TrackingError("Could not load replies from Microsoft. Please try again.") from None


def load_replies(database, email_id, access_token):
    """Read only previously matched replies on demand; do not persist their bodies."""
    with database() as connection:
        record = connection.execute("SELECT * FROM microsoft_email_tracking WHERE email_id = ?", (email_id,)).fetchone()
        if not record:
            raise TrackingError("Tracked email not found.")
        events = connection.execute(
            """SELECT * FROM microsoft_tracking_events WHERE email_id = ? AND kind = 'reply'
               ORDER BY occurred_at DESC, id DESC""", (email_id,),
        ).fetchall()
    replies = []
    selection = "id,subject,from,receivedDateTime,body,internetMessageId"
    for event in events:
        message = None
        if event["graph_message_id"]:
            message = _reply_request(f"{GRAPH}/me/messages/{quote(event['graph_message_id'], safe='')}",
                                     access_token, {"$select": selection})
        if message is None and event["external_id"]:
            if event["external_id"].startswith("<"):
                data = _reply_request(f"{GRAPH}/me/messages", access_token,
                                      {"$filter": "internetMessageId eq '" + event["external_id"].replace("'", "''") + "'",
                                       "$select": selection, "$top": 1})
                values = (data or {}).get("value", [])
                message = values[0] if values else None
            elif not event["graph_message_id"]:
                message = _reply_request(f"{GRAPH}/me/messages/{quote(event['external_id'], safe='')}",
                                         access_token, {"$select": selection})
        if not message:
            replies.append({"received_at": event["occurred_at"], "unavailable": True,
                            "error": "This reply is no longer available in the sender mailbox."})
            continue
        sender = (message.get("from") or {}).get("emailAddress", {})
        if (sender.get("address", "").lower() != record["recipient"].lower()
                or (event["external_id"].startswith("<") and message.get("internetMessageId") != event["external_id"])):
            raise TrackingError("The mailbox reply does not match this tracked email.")
        body = message.get("body") or {}
        content = body.get("content", "")
        if body.get("contentType", "").lower() == "html":
            parser = _ReplyText()
            parser.feed(content)
            content = "".join(parser.parts).strip()
        replies.append({"subject": message.get("subject") or "(No subject)",
                        "sender": sender.get("address"), "sender_name": sender.get("name"),
                        "received_at": message.get("receivedDateTime") or event["occurred_at"],
                        "body": content, "unavailable": False})
        with database() as connection:
            connection.execute("UPDATE microsoft_tracking_events SET graph_message_id = ? WHERE id = ?",
                               (message.get("id"), event["id"]))
            if sender.get("name"):
                connection.execute("UPDATE microsoft_email_tracking SET recipient_name = ? WHERE email_id = ?",
                                   (sender["name"], email_id))
    return {"email_id": email_id, "recipient": record["recipient"], "subject": record["subject"], "replies": replies}
