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

1. Open [Meta for Developers](https://developers.facebook.com/) → Create App → type **Business**.
2. Add **Facebook Login** and permissions used by MultiStream:
   `pages_show_list`, `pages_manage_posts`, `pages_read_engagement`, `publish_video`, `pages_manage_engagement`
3. Facebook Login → Settings → Valid OAuth Redirect URI:

   `https://multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com/oauth/facebook/callback`

4. In development mode, add yourself as app admin/developer/tester.
5. Paste App ID + App Secret into MultiStream → **Save app credentials**.
6. Click **Connect Facebook** — pick an account that manages a **Page** (required).

## 3) Every stream

1. PIN login → set **Title** + **Description**.
2. **Prepare live on platforms** (creates YT broadcast + FB live video, updates stream keys).
3. Start Zoom **Custom Live Streaming** with the Azure RTMP URL/key.
4. Use a watch URL from the UI for Zoom’s “Live streaming page URL”.

Anyone with the PIN can change title/description afterward; they do not need your laptop. They use the same stored OAuth tokens on the server.
