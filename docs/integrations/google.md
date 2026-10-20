# Google: Calendar, Gmail and Sheets

With Google connected, Jarvis reads and writes events in your Google Calendar, searches and reads your
Gmail and writes drafts, and reads and updates your Google Sheets. It talks to Google with your own OAuth
client, so no third party sits between Jarvis and your account.

Jarvis asks Google for exactly these permissions: Calendar events, Gmail read, Gmail compose (drafts only),
Sheets, and Drive metadata (file names, to find a spreadsheet by name; never file contents). There is no
send permission: Jarvis can never send an email, it only creates drafts that you send yourself.

Google Maps is a separate key, see [maps](maps.md).

## What you need

- A Google account (a personal Gmail account works).
- A Google Cloud project with your own OAuth client. It is free; no billing account is needed for
  Calendar, Gmail and Sheets. Every user creates their own client: Jarvis ships none.
- About 15 minutes, once.

## Set it up

### 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com> and sign in with the Google account Jarvis should use.
2. In the project picker at the top, click **New project**, name it `jarvis`, click **Create**, and make sure
   the new project is selected.

### 2. Turn on the four APIs

Menu > **APIs & Services** > **Library**. Search for each of these, open it and click **Enable**:

- **Google Calendar API**
- **Gmail API**
- **Google Sheets API**
- **Google Drive API** (only used to find spreadsheets by name)

### 3. Configure the consent screen (Google Auth platform)

1. Menu > **Google Auth platform** > **Branding**. If it says "Google Auth platform not configured yet", click
   **Get started**.
2. **App information**: app name `Jarvis`, user support email = your address. **Next**.
3. **Audience**: choose **External**. **Next**.
4. **Contact information**: your address. **Next**.
5. Accept the Google API Services User Data Policy, **Continue**, **Create**.
6. Open **Audience**. Leave the publishing status at **Testing**. Under **Test users** click **Add users**, add
   your own Google address, **Save**. Only test users can log in while the app is in Testing.
7. Adding scopes under **Data Access** is not required: Jarvis asks for its scopes during the login.

Do not publish the app. A published app with these Gmail scopes would need Google's security review.

### 4. Create a Desktop OAuth client

1. **Google Auth platform** > **Clients** > **Create client**.
2. Application type: **Desktop app**. Name: `Jarvis desktop`. **Create**.
3. In the dialog that follows, click **Download JSON**. The file is called something like
   `client_secret_1234-abcd.apps.googleusercontent.com.json` and lands in `~/Downloads`. Newer consoles show the
   client secret only at this moment; if you missed it, create a new client.

### 5. Log in from Jarvis

```
jarvis google login --client-secret ~/Downloads/client_secret_XXXX.json
```

(`jarvis setup` offers the same step and asks for the file path.)

1. A browser window opens. Pick the account you added as a test user.
2. Google warns "Google hasn't verified this app". That is expected for your own Testing app: click
   **Continue** (or **Advanced** > **Go to Jarvis (unsafe)**).
3. Tick every box (calendar events, read Gmail, manage drafts, Sheets, Drive file information) and click
   **Continue**.
4. The terminal prints `Google login complete. Token stored in the macOS Keychain.`

Both the client JSON and the token are now in the macOS Keychain (`google_client_secret`, `google_token`).
You can delete the downloaded JSON file.

### 6. Optional settings

```toml
[calendar]
default_write_calendar = "google"   # where new events go: "google" or "apple"

[sheets]
aliases = { "training" = "<spreadsheet id from the sheet's URL>" }   # "add this to my training sheet"
```

Jarvis uses your **primary** Google calendar. If the same Google calendar is also added to the macOS Calendar
app, list its name under `[calendar] apple_calendar_exclude` so events do not show up twice.

## The 7-day login limit

While the consent screen is in **Testing**, Google lets the login expire after **7 days**. Jarvis then reports
that Google refused to refresh the token. Log in again (the client is still in the Keychain, so no file is
needed):

```
jarvis google login
```

This is a Google rule for apps in Testing; it is the price of not needing Google's review. `jarvis google
logout` removes the token; `jarvis key delete google_client_secret` removes the client too.

## macOS permissions

None. The login uses your default browser and a short-lived local port.

## Check that it works

`jarvis doctor` shows:

```
[ok  ] Google OAuth client   found in Keychain
[ok  ] Google token          valid for 52 more minute(s)
[ok  ] Google scopes         calendar.events, gmail.readonly, gmail.compose, spreadsheets, drive.metadata.readonly
[ok  ] Google Sheets         token covers spreadsheets, drive.metadata.readonly; 0 alias(es) configured in [sheets]
```

"expired, refresh token present (refreshes on first use)" is fine too. Then in the chat:

- "What's on my Google calendar today?"
- "Search my mail for invoices from last week."
- "Draft a reply to the last email from Alex saying I'll be late." (asks for confirmation; check Gmail's Drafts
  folder: it is a draft, not sent)
- "Find my spreadsheet called budget."

## Common problems

| Problem | Fix |
|---|---|
| `Google refused to refresh the token (invalid_grant ...)` | The 7-day Testing limit, or you removed Jarvis's access in your Google account. Run `jarvis google login`. |
| "Access blocked: Jarvis has not completed the Google verification process" | Your address is not a test user. Add it under Google Auth platform > Audience > Test users. |
| `forbidden (403) ... has not been used ... or it is disabled` | The API is not enabled in this project. Enable it under APIs & Services > Library and wait a minute. |
| `the Google token lacks the scope(s) ...` or `Google Sheets scopes  missing` | A box was unticked during login. Run `jarvis google login` again and tick everything; earlier permissions are kept. |
| `the Google OAuth client JSON must be a Desktop-app client file` | You downloaded a "Web application" client. Create a **Desktop app** client instead. |
| Events appear twice | Add the synced Google calendar's name to `[calendar] apple_calendar_exclude`. |
| A sheet is not found by name | Give it an alias under `[sheets] aliases`, or paste its URL in the chat. |
