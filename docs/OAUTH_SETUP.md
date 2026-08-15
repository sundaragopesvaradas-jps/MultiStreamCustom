# OAuth setup (title / description on YouTube + Facebook)

RTMP cannot set title/description. MultiStream uses platform APIs after a one-time OAuth connect.

## 1) Google (YouTube)

1. Open [Google Cloud Console](https://console.cloud.google.com/) → create/select a project.
2. Enable **YouTube Data API v3**.
3. OAuth consent screen → External (or Internal for Workspace) → add your Google account as a test user.
4. Credentials → **Create OAuth client ID** → Web application.
5. Authorized redirect URI (exact):

   `https://multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com/oauth/youtube/callback`

6. Copy Client ID + Client Secret into the MultiStream UI → **One-time API app setup**.
7. Click **Connect YouTube** (must use an account that can go live on that channel).

## 2) Meta (Facebook Page)

Meta replaced the old "pick an app type" screen with **use cases**, so the dashboard looks
nothing like older tutorials. The steps below match the current dashboard.

### Before you start — eligibility

Since June 2024 Meta blocks going live unless **both** are true. Check these first, because
the app will build fine and then fail at broadcast time:

- The Facebook account is at least **60 days old**.
- The **Page has at least 100 followers**.

You must broadcast to a **Page** you administer. Personal-profile lives need the
`publish_video` permission and full App Review, which this setup deliberately avoids.

### Create the app

1. Go to <https://developers.facebook.com/apps/> and click **Create app**.
   (The site is live; if the root URL misbehaves, go straight to `/apps/`. It also requires
   a verified Facebook account, so complete any "verify your account" prompt first.)
2. **App details**: enter a name and contact email. If you have a Meta Business portfolio,
   connect it — this makes the Page permissions much easier to grant.
3. **Use cases**: select **Other** → **Business**, or pick **Manage everything on your Page**.
   Do not pick "Authenticate and request data from users with Facebook Login" — it is
   incompatible with the Page permissions we need and will be greyed out later.
4. Click through **Requirements** → **Go to dashboard**.

Meta may auto-add **Facebook Login for Business** to the app. Which product you get decides
the next step, so check the left sidebar before continuing.

### Configure the redirect URI

In the left sidebar open **Facebook Login for Business** (or plain **Facebook Login**) →
**Settings**, and set **Valid OAuth Redirect URIs** to exactly:

```
https://multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com/oauth/facebook/callback
```

Click **Check URI**, keep **Client OAuth login** and **Web OAuth login** on, then **Save changes**.

### Grant the permissions

MultiStream asks for exactly three permissions — nothing more:

`pages_show_list`, `pages_read_engagement`, `pages_manage_posts`

- **If the sidebar says "Facebook Login"** (no "for Business"): nothing else to do. The app
  requests these as scopes during connect.
- **If the sidebar says "Facebook Login for Business"**: go to **Configurations** →
  **Create configuration**, choose **User access token**, select your **Page** as the business
  asset, tick those three permissions, and **Create**. Copy the **Configuration ID** it gives you.

### Finish in MultiStream

1. App Dashboard → **App settings → Basic** → copy **App ID** and **App Secret**.
2. In the MultiStream UI → **One-time API app setup**, paste both. If you created a
   configuration above, also paste the **Configuration ID** into the new field; otherwise
   leave it blank.
3. Keep the app in **Development mode** and make sure you are listed under **App roles** as
   admin, developer or tester. In Development mode the Live Video API works for those roles
   without App Review — that is why you do not need to submit anything.
4. Click **Connect Facebook** and grant access to your Page when prompted.

### If connect fails

| What you see | Cause |
| --- | --- |
| "No Facebook Pages were returned" | The login did not grant `pages_show_list`, or the Login for Business configuration did not include your Page. |
| Error mentioning `1363120` | Account younger than 60 days. |
| Error mentioning `1363144` | Page has fewer than 100 followers. |
| `(#10) ... live-video-api` | You are acting as someone without an app role. Add them under App roles, or keep broadcasting as an admin. |
| "URL blocked" on the consent screen | The redirect URI above does not match character for character. |

MultiStream calls Graph API `v23.0`. If Meta retires that version, set a Key Vault secret
named `facebook-graph-version` (for example `v26.0`) to move without a code change.

## 3) Every stream

1. PIN login. Set **Default title (auto-prepare)** once (Key Vault secrets
   `default-stream-title` / `default-stream-description`). Fallback if unset:
   **ISKCON Deoghar Live**.
2. Prefer **Prepare live on platforms** before Zoom (creates YT + FB lives + fresh keys).
3. If you skip Prepare live, Zoom Custom Live Streaming still works: MultiStream auto-creates
   lives with that default title when Zoom connects. Use **Update title & description** mid-stream
   to rename without rotating keys.
4. Use a watch URL from the UI for Zoom’s “Live streaming page URL”.

Anyone with the **manager PIN** can start/stop streaming and update title/destinations
mid-stream. Only the **owner PIN** can open section 4 (OAuth apps, keys, prepare live).

Until an owner PIN is created, the existing team PIN still has full (owner) access so
you are not locked out. After you save an owner PIN in section 4, that same team PIN
becomes manager-only.

## 4) Login lockout email

After 5 wrong PIN attempts from one IP, login locks for 60 minutes and emails
`sundaragopesvaradas.jps@gmail.com`.

### Store SMTP secrets in Azure Key Vault

Vault name: **`mskvjixozpwkde4wu`** (resource group `rg-multistream`).

Secret names (exact):

| Secret name | Value |
| --- | --- |
| `smtp-user` | Gmail address that sends the alert (e.g. `sundaragopesvaradas.jps@gmail.com`) |
| `smtp-password` | 16-character [Gmail App Password](https://myaccount.google.com/apppasswords) — paste **without spaces** |
| `smtp-host` (optional) | default `smtp.gmail.com` |
| `smtp-port` (optional) | default `587` |

**Portal**

1. [Azure Portal](https://portal.azure.com) → Key vaults → `mskvjixozpwkde4wu` → **Secrets**
2. **Generate/Import** → name `smtp-user` → paste the Gmail address → Create
3. **Generate/Import** → name `smtp-password` → paste the App Password with spaces removed → Create

**Azure CLI** (from a machine that has Key Vault Secrets Officer on this vault, or from the MultiStream VM after `az login --identity`):

```bash
az keyvault secret set --vault-name mskvjixozpwkde4wu --name smtp-user \
  --value 'sundaragopesvaradas.jps@gmail.com'

az keyvault secret set --vault-name mskvjixozpwkde4wu --name smtp-password \
  --value 'YOUR_16_CHAR_APP_PASSWORD'
```

No UI redeploy is required — the next lockout reads these secrets live from Key Vault.

**Note:** Use a Gmail App Password, not the normal Gmail login password. If the App Password was ever shared in chat or screenshots, revoke it in Google Account → App passwords and create a new one.
