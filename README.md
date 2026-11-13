# Microsoft login and email sender (Python Flask)

This example signs a user in with Microsoft and sends an email from that user's mailbox through Microsoft Graph. It uses delegated `Mail.Send` permission, so the user sees a consent prompt and the message appears in their Sent Items.

Multiple Microsoft accounts can be connected. Their passwords are never stored. The application encrypts each account's MSAL token cache in SQLite, displays saved accounts in a sender dropdown, and silently refreshes an expired access token when Microsoft still permits it.

## 1. Register the Microsoft application

1. Open the [Microsoft Entra admin center](https://entra.microsoft.com/) and go to **Identity → Applications → App registrations → New registration**.
2. Choose the supported account type you need. For both company and personal Microsoft accounts, choose **Accounts in any organizational directory and personal Microsoft accounts**.
3. Under **Authentication**, add a **Web** redirect URI exactly equal to:

   `http://localhost:8000/auth/callback`

4. Under **Certificates & secrets**, create a client secret. Copy the secret **Value** immediately.
5. Under **API permissions**, add **Microsoft Graph → Delegated permissions → Mail.Send**. Some organizations require an administrator to approve this permission.

## 2. Configure and run

In PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set the application/client ID, client secret value, tenant setting, and a random Flask secret. Then run:

```powershell
python app.py
```

Open <http://localhost:8000>, sign in, enter up to 50 recipient addresses (one per line or comma-separated), add the subject and message, then select **Send email**. The app sends a separate message to each recipient so recipients do not see the rest of the list.

To add multiple senders, select **Add Microsoft account**, complete Microsoft login, then select **Sign out Microsoft** and repeat with another account. Previously connected accounts remain available in the sender dropdown. Keep `TOKEN_ENCRYPTION_KEY` secret and unchanged; changing it makes saved tokens unreadable.

### Windows socket error or port already in use

If `python app.py` reports that socket access is forbidden or the port is already in use, choose an available port in `.env`, for example:

```dotenv
PORT=5000
MICROSOFT_REDIRECT_URI=http://localhost:5000/auth/callback
```

Add `http://localhost:5000/auth/callback` as a **Web** redirect URI under **Authentication** in your Microsoft app registration. Restart `python app.py` and open <http://localhost:5000>. Keep `PORT`, the callback URL, and the registered redirect URI in sync. Changing the port avoids stopping another application that needs port 8000.

## Notes

Optional: [open, click and reply tracking for Microsoft-sent emails](TRACKING_SETUP.md). The existing Microsoft sender and email form stay in use.

- `MICROSOFT_TENANT_ID=common` permits both organizational and personal Microsoft accounts when the app registration supports them. Use your Directory (tenant) ID to restrict sign-in to one organization.
- Never commit `.env` or the client secret.
- This is a small local example. For production, use HTTPS, server-side session storage, CSRF protection on the send form, a production WSGI server, and a certificate or managed secret store instead of a plain client secret.
