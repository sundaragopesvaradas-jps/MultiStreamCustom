# Zoom Custom RTMP via API — PoC

Goal: prove whether Zoom can push Custom Live Streaming to MultiStream when the
**host is only on mobile**, by calling Zoom’s livestream REST APIs instead of
clicking Custom Live Streaming on desktop.

## Result criteria

| Outcome | Meaning |
| --- | --- |
| `start` succeeds **and** MediaMTX shows a ready path within ~45s | **PASS** — API path works for mobile hosts; build thin Start/Stop UI |
| `start` fails, or succeeds but **no RTMP** arrives | **FAIL** — need Meeting SDK bot or co-host laptop |

## 1) Create a Zoom Server-to-Server OAuth app

1. Open [Zoom Marketplace](https://marketplace.zoom.com/) → **Develop** → **Build App**
2. Choose **Server-to-Server OAuth**
3. Add scopes (granular admin variants if offered):
   - `meeting:read:meeting:admin`
   - `meeting:write:meeting:admin`
   - `meeting:read:livestream:admin`
   - `meeting:update:livestream:admin`
   - `meeting:update:livestream_status:admin`
4. Activate the app
5. Copy **Account ID**, **Client ID**, **Client Secret**

Also confirm in Zoom web settings → **In Meeting (Advanced)**:
- Allow live streaming meetings
- **Custom Live Streaming Service** checked

## 2) Store credentials in Key Vault

Vault: `mskvjixozpwkde4wu`

```bash
# From the MultiStream VM (has managed identity):
az login --identity
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-account-id --value 'ACCOUNT_ID'
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-client-id --value 'CLIENT_ID'
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-client-secret --value 'CLIENT_SECRET'
```

Or paste the three values here and ask the agent to store them (preferred: you run the commands so secrets never land in chat).

## 3) Run the PoC

On the Azure VM (or any machine with the secrets + network access to Zoom + the VM’s MediaMTX check only works on the VM):

```bash
# Install script once
sudo install -m 755 /path/to/repo/tools/zoom-livestream-poc.py \
  /opt/multistream/bin/zoom-livestream-poc.py

# 1. Start a Zoom meeting from your PHONE (host). Note the Meeting ID.
# 2. Point that meeting at MultiStream ingest:
sudo /opt/multistream/ui/.venv/bin/python /opt/multistream/bin/zoom-livestream-poc.py \
  configure --meeting-id YOUR_MEETING_ID

# 3. Try to start Custom RTMP with phone-only host (no desktop Zoom):
sudo /opt/multistream/ui/.venv/bin/python /opt/multistream/bin/zoom-livestream-poc.py \
  configure --meeting-id YOUR_MEETING_ID   # safe to re-run
sudo /opt/multistream/ui/.venv/bin/python /opt/multistream/bin/zoom-livestream-poc.py \
  start --meeting-id YOUR_MEETING_ID

# 4. Inspect
sudo /opt/multistream/ui/.venv/bin/python /opt/multistream/bin/zoom-livestream-poc.py \
  status --meeting-id YOUR_MEETING_ID

# 5. Stop when done
sudo /opt/multistream/ui/.venv/bin/python /opt/multistream/bin/zoom-livestream-poc.py \
  stop --meeting-id YOUR_MEETING_ID
```

`start` prints **PASS** or **FAIL** based on whether MediaMTX receives the publisher.

## Notes

- Meeting must already be **in progress** before `start` (Zoom returns an error otherwise).
- MultiStream ingest used: `rtmp://multistream-jixozpwkde4wu.centralindia.cloudapp.azure.com/live` + `ingest-stream-key`.
- Keep destination toggles / auto-prepare as usual if you want YT/FB fan-out after RTMP arrives.
