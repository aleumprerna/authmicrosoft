# Tracking for the existing Microsoft email sender

Emails continue to send through **Microsoft Graph**, using the same connected Microsoft accounts, form and `POST /send` route. There is no provider selector and no SendGrid dependency. The tracking table appears below the original form.

## What you can see

- **Send status:** whether Microsoft accepted the request, rejected it or returned an uncertain connection result. Microsoft's `202 Accepted` response is not proof of delivery. See [Microsoft sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0).
- **Opens:** requests for a unique tracking image in that recipient's email.
- **Clicks:** requests for that email's tracked links, which redirect to the original destination.
- **Reply:** a received email from that recipient whose reply headers reference the exact Microsoft-sent message. Replies are checked when you select **Check replies**; the dashboard refreshes stored results every five seconds.

Each email row shows the recipient's name when Microsoft has supplied one (otherwise the email address), subject, sent time, send status, open activity, click activity and reply status. Select **View reply** on a replied email to read its actual replies in a dialog. Desktop columns become labelled cards on smaller screens. After a successful send, the app scrolls to the tracking section.

Open/click tracking applies to **new emails sent with a public tracking URL configured**. It cannot be added retroactively to email already delivered. Reply matching applies to messages recorded by this version of the app.

## 1. Enable opens and clicks

Start the small tracking-only gateway in a second terminal. It shares the existing database and does not expose the sending form or mailbox APIs:

```powershell
python tracking_gateway.py
```

Then start a Cloudflare Quick Tunnel to the gateway in a third terminal:

```powershell
.venv\Scripts\cloudflared.exe tunnel --url http://127.0.0.1:5001
```

This needs the official [cloudflared executable](https://github.com/cloudflare/cloudflared/releases/latest) installed at that path (or use an installed `cloudflared` command). Quick Tunnels do not need an account or domain. Only the opaque image/link endpoints are exposed by the gateway; continue using <http://localhost:5000> for the app. See [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

Set the generated `trycloudflare.com` HTTPS root URL in your existing `.env`:

```dotenv
TRACKING_PUBLIC_BASE_URL=https://your-random-host.trycloudflare.com
```

Restart the app:

```powershell
python app.py
```

Send an email as usual from your Microsoft account. The app adds a unique tracking image and turns full HTTP(S) URLs into tracking links. The message text is safely escaped into HTML; the visible text, subject, recipients and Microsoft sender stay the same. Put a test URL on its own line, for example `https://example.com/?test=1`.

Keep the gateway, app and tunnel running while recipients open or click. Quick Tunnels have no uptime guarantee and their hostname changes on restart. If the tunnel URL changes, links and image URLs in earlier emails continue to depend on the old hostname; updating `.env` only affects new emails. A stable HTTPS domain is needed for long-term tracking.

Free ngrok was tested and returned its browser warning page instead of the image. Do not use that endpoint for real open/click tracking. Browser links and email images cannot attach the client header ngrok requires to skip it; use a public host without that interstitial. See [ngrok free-plan limits](https://ngrok.com/docs/pricing-limits/free-plan-limits).

If `TRACKING_PUBLIC_BASE_URL` is empty or invalid, the app keeps sending the original plain-text message. Sent records and reply matching still work; the open/click columns say **Not enabled**.

The dashboard now checks the public `/t/health` endpoint every 15 seconds and shows whether the connection is reachable. Health probes do not count as opens. A URL merely being configured does not mean its tunnel is online.

Before each send batch, the app checks tracking connectivity once. If it is offline, Microsoft still sends the original text and destination URLs; the row records that open/click tracking was unavailable at send time. It does not insert links pointing to a known-broken tunnel. Each new record saves the tracking address it actually depends on, so a healthy replacement tunnel does not hide an earlier email's offline address.

If the Cloudflare log says `Unauthorized: Tunnel not found`, stop that expired tunnel and create a new one. On Windows you can use IPv4 for the tunnel connection:

```powershell
.venv\Scripts\cloudflared.exe tunnel --url http://127.0.0.1:5001 --no-autoupdate --protocol http2 --edge-ip-version 4
```

Copy the new URL into `TRACKING_PUBLIC_BASE_URL`, restart the app and wait for the dashboard to report that tracking is connected. Then send a **new test email**. Images and links inside already delivered messages still contain the old address and cannot be repaired by changing the configuration. Their existing historical events are preserved, but new activity cannot arrive through a dead address. Temporary tunnels remain for localhost testing; a stable hosted address is required for durable tracking.

## 2. Enable reply checking once for each Microsoft sender

1. In your existing Microsoft app registration, under **API permissions**, add **Microsoft Graph > Delegated permissions > Mail.Read** alongside the existing **Mail.Send**. Your organization may require administrator consent.
2. In the app's tracking section, select **Enable reply tracking**.
3. Sign in to the same Microsoft sender account and grant the additional permission. The existing account is updated; its password is not stored.
4. Select that sender under **Check replies for**, then select **Check replies**.

The ordinary **Add Microsoft account** login continues to request only `Mail.Send`. Sending does not depend on granting `Mail.Read`. The extra permission is requested only through **Enable reply tracking**. Mailbox listing is documented in [Microsoft List messages](https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0).

Reply checks read message IDs, recipients, dates and headers. Clicking **View reply** retrieves only replies already matched to that email and displays their sender, subject, time and body as safe text. Bodies are fetched on demand and are not stored in SQLite. The app does not change inbox messages or send automated replies/follow-ups. Existing reply records can still be opened using their Internet message IDs, including after a message moves folders. Microsoft documents [reading messages as text](https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0).

The app's custom `x-app-email-id` header links the Sent Items copy to its local record. Received `In-Reply-To` or `References` headers then establish the reply relationship. Microsoft documents [custom headers and message IDs](https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0). Reply-check notices separate newly found replies from replies already found, so a check with zero new replies does not hide existing responses.

Checks ignore messages from other senders, same-subject messages without matching reply headers, drafts, automated responses marked `Auto-Submitted`, and report messages such as read receipts. A recipient replying from an alias or an email client that strips reply headers may not be matched. **No reply found** means no matching reply at the last completed check; it is not proof that none exists. Missing sent copies remain pending rather than being guessed from their subjects.

Large mailboxes are scanned in bounded pages. If the app says there are more messages to scan, select **Check replies** again to resume. Repeat checks do not duplicate reply counts. A two-minute overlap and a rewind when a late Sent Items copy appears reduce missed events.

## 3. Verify with inboxes you control

1. Configure the public URL and restart Flask. Keep the app and tunnel running.
2. Send to two test addresses using the existing Microsoft form, with an HTTPS link in the body.
3. Confirm the recipients have different tracking rows and that Microsoft still sends the email.
4. Open one email with remote images enabled. Its **Opens** count should update if the client loads the tracking image.
5. Click its link. Confirm the original destination opens and that only that recipient's **Clicks** count changes.
6. Reply normally from that inbox using its **Reply** action. Grant read consent for the sender account if needed, then select **Check replies**. That row should show **Replied** with a **View reply** button. Open it to see the actual message.
7. Check replies again. The same received reply must not increase the count twice, and the notice should still report the existing total.

Image blocking and caching can hide opens. Privacy services and scanners can load images or links without a human reading the email. Open and click counts are **approximate signals**. For example, [Apple Mail Privacy Protection](https://www.apple.com/legal/privacy/data/en/mail-privacy-protection/) can fetch remote content in the background regardless of engagement. The sender mailbox's `isRead` flag is not used to infer recipient opens.

## Tests and files

Run `python -m pytest -q`. Tests mock Microsoft API calls and do not send real mail. They cover the original Microsoft flow, unique recipient records, pixel and link endpoints, HEAD probes, escaping, unavailable tracking storage, Microsoft errors, optional read consent, exact reply matching, duplicates, pagination and delayed sent copies. Live mailbox verification requires your Microsoft account, consent and running public URL.

Changes are limited to `app.py` (tracking hooks/routes and optional reply consent), `templates/index.html` and `templates/base.html` (layout and reply reader), `.env`/`.env.example` (one optional URL), `mail_tracking.py`, `static/tracking.js`, `static/app.css`, `tests/test_mail_tracking.py`, this guide and `tracking_gateway.py`. Existing accounts and encryption settings are preserved in the same SQLite database. No new Python package is required.
