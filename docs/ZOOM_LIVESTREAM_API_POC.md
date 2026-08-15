# Zoom Custom RTMP via API

Zoom can push Custom Live Streaming to MultiStream when the **host is only on
mobile**, by calling Zoom’s livestream REST APIs instead of clicking Custom Live
Streaming on desktop. This was proven with the PoC script below and is now wired
into the UI as the **Start streaming** button.

## Day-to-day use (no scripts)

1. Someone starts the Zoom meeting — the host can be on a phone. The meeting must
   be **scheduled on the Zoom account that owns the API app**; whoever hosts or
   co-hosts it does not matter.
2. Open the MultiStream UI, enter the PIN.
3. In **Start streaming**: tick YouTube / Facebook, set the title and description,
   enter the meeting ID, press the button.

That one action writes the destination toggles, saves the title as the default,
creates the YouTube broadcast and Facebook live if they aren’t already usable,
refreshes the relay keys, then points the meeting at
`rtmp://<host>/live` + `ingest-stream-key` and flips the livestream to `start`.

**You do not need to press Stop.** Ending the Zoom meeting stops the RTMP
publisher; MediaMTX fires `on-stream-end.sh`, the FFmpeg pushes die, YouTube
auto-completes the broadcast and Facebook closes the live video. **Stop
streaming** exists only to end the broadcast early while keeping the meeting open.

The one-time credentials go in **One-time API app setup → Zoom API app** in the UI,
which writes `zoom-account-id`, `zoom-client-id` and `zoom-client-secret` to Key Vault.

## Result criteria (original PoC)

| Outcome | Meaning |
| --- | --- |
| `start` succeeds **and** MediaMTX shows a ready path within ~45s | **PASS** — API path works for mobile hosts; build thin Start/Stop UI |
| `start` fails, or succeeds but **no RTMP** arrives | **FAIL** — need co-host laptop |

Result: **PASS**.

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

## 2) Store credentials

Easiest: paste them into **One-time API app setup → Zoom API app** in the UI.

Or by hand — vault `mskvjixozpwkde4wu`:

```bash
# From the MultiStream VM (has managed identity):
az login --identity
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-account-id --value 'ACCOUNT_ID'
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-client-id --value 'CLIENT_ID'
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-client-secret --value 'CLIENT_SECRET'
```

Or paste the three values here and ask the agent to store them (preferred: you run the commands so secrets never land in chat).

## 3) Run the PoC script (only for debugging)

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
