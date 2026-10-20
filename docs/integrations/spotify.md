# Spotify

Jarvis plays songs, artists, albums and playlists in the Spotify app on your Mac, and pauses, skips or tells
you what is playing. It finds music with Spotify's Web API and then tells the Spotify app what to play, so
playback happens in the normal app with your own account.

## What you need

- The Spotify desktop app in `/Applications`, signed in.
- A free Spotify developer app (a Client ID and a Client Secret). Since February 2026 Spotify requires the
  **owner of a developer app to have Spotify Premium**, and allows one such app per developer account; check
  <https://developer.spotify.com/documentation/web-api> if this has changed.
- Play, pause, next, previous and "what's playing" work without the keys; searching ("play some jazz") needs
  them.

## Set it up

1. Open <https://developer.spotify.com/dashboard> and log in with your Spotify account. Accept the developer
   terms if asked.
2. Click **Create app**.
   - App name: `Jarvis`; App description: `Personal assistant on my Mac`.
   - Redirect URI: the form asks for one, but Jarvis never uses it. Enter `http://127.0.0.1:8888/callback`
     and click **Add**.
   - Under the APIs to use, tick **Web API**.
   - Accept the terms and click **Save**.
3. Open the new app's **Settings**. Copy the **Client ID**; click **View client secret** and copy the
   **Client secret**.
4. Store both in the Keychain (`jarvis setup` offers the same):

   ```
   jarvis key set spotify_client_id
   jarvis key set spotify_client_secret
   ```

Jarvis uses the "client credentials" flow: it only searches the public catalog and never logs in as you or
reads your library.

## macOS permissions

The first playback command makes macOS ask "*Terminal* wants access to control *Spotify*". Click **OK**.
If you refused it: System Settings > Privacy & Security > **Automation**, find your terminal app and turn on
**Spotify** under it. For the voice service and Telegram the same switch appears under the tool environment's
Python (see [troubleshooting](../troubleshooting.md#which-app-gets-the-permission)).

## Check that it works

`jarvis doctor` shows `Spotify credentials  found in the Keychain` and `Spotify.app  installed at
/Applications/Spotify.app`. In the chat:

- "Play some lo-fi."
- "Play the album Kind of Blue."
- "What's playing?" then "Next song."

By voice, when a name was heard poorly, Jarvis asks "Did you mean ...?" before playing the closest match.

## Common problems

| Problem | Fix |
|---|---|
| `Spotify credentials are missing` | Run both `jarvis key set` commands above. |
| `Spotify rejected the client id/secret` | Copy them again from the dashboard (no spaces). If the app owner's Premium lapsed, Spotify switches the app off until it is renewed. |
| Nothing plays, no error | Open the Spotify app once and sign in; it must be running and logged in. |
| "Not authorized to send Apple events to Spotify" | The Automation permission was refused. Turn it on as described above, then restart the terminal. |
| `Spotify.app not found` | Install the desktop app into `/Applications` (not the web player). |
