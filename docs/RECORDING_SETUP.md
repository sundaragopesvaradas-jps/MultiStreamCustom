# Scheduled Zoom recording (Meeting SDK → Azure Blob)

Owner-only feature: a bot joins the configured Zoom meeting during IST schedule
windows, records with the **Meeting SDK raw data** path, uploads the file to
Azure Blob Storage, emails you when a recording is saved, and deletes blobs
older than **6 months**.

This is separate from livestreaming (YouTube / Facebook).

## Architecture (separation of concerns)

| Piece | Role |
| --- | --- |
| `recording/models.py` + `store.py` | Schedule data + JSON on the VM |
| `recording/clock.py` | IST window matching |
| `recording/meeting.py` | “Is the meeting running?” via Zoom REST |
| `recording/recorder/` | Meeting SDK process adapter |
| `recording/blobstore.py` | Upload + retention |
| `recording/pipeline.py` | One scheduler tick |
| Owner UI (section 4) | Edit schedule / bot name / enable |

## 1) Azure Blob Storage

Create a storage account in the same subscription (Central India is fine), e.g.
`msisckcondeoghar`, with a container named `recordings`.

Grant the **VM managed identity** the role **Storage Blob Data Contributor** on
that storage account.

Save in Key Vault (or paste in UI → One-time API app setup → Recording):

| Secret | Example |
| --- | --- |
| `recording-storage-account` | `msisckcondeoghar` |
| `recording-storage-container` | `recordings` (optional; default) |

## 2) Zoom Meeting SDK app (raw data)

1. [Zoom Marketplace](https://marketplace.zoom.com/) → Build App → **Meeting SDK**
2. Enable **raw data** (Zoom may require an approval / request form)
3. Copy **SDK Key** and **SDK Secret**
4. Paste into the UI (section 4 → Recording credentials) or:

```bash
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-sdk-key --value 'SDK_KEY'
az keyvault secret set --vault-name mskvjixozpwkde4wu --name zoom-sdk-secret --value 'SDK_SECRET'
```

### Recording rights for the bot

Joining is not enough: Zoom only releases raw audio/video to a participant that
has local-recording rights. The scheduler asks the Server-to-Server app for a
**local recording token** per meeting, which grants those rights automatically.

Add these scopes to the existing Server-to-Server OAuth app:

| Scope | Used for |
| --- | --- |
| `meeting:read:meeting:admin` | Is the meeting in progress + passcode |
| `meeting:read:local_recording_token:admin` | Recording rights for the bot |

The token needs a **paid (Pro or higher) Zoom plan** with *Local recording*
enabled in account settings. Without it the bot still joins, you get an email
saying so, and the host must click **Allow to record local files** for the bot
in every meeting.

## 3) Install the Linux Meeting SDK recorder binary

Zoom's Linux Meeting SDK is a proprietary download — a script cannot fetch it,
and Zoom's own sample is demo code rather than a finished recorder.

1. Download `zoom-meeting-sdk-linux_x86_64-<version>.tar.xz` from the
   Marketplace app (Download SDK → Linux) and copy it to the VM.
2. Clone [zoom/meetingsdk-linux-raw-recording-sample](https://github.com/zoom/meetingsdk-linux-raw-recording-sample)
   and drop the SDK headers/libs into `demo/` as its README describes.
3. Build dependencies on Ubuntu:

```bash
sudo apt-get install -y build-essential cmake pkg-config openssl ca-certificates \
  libcurl4-openssl-dev libx11-xcb1 libxcb-xfixes0 libxcb-shape0 libxcb-shm0 \
  libxcb-randr0 libxcb-image0 libxcb-keysyms1 libxcb-xtest0 libdbus-1-3 \
  libglib2.0-0 libgbm1 libxfixes3 libgl1 libdrm2 libgssapi-krb5-2 \
  pulseaudio pulseaudio-utils
```

4. The VM has no sound card, so raw audio needs a virtual PulseAudio sink plus
   `~/.config/zoomus.conf` containing `[General]` / `system.audio.type=default`
   (the sample's `setup-pulseaudio.sh` does both).
5. `cmake -B build && make` in `demo/`, then install a wrapper as
   `/opt/multistream/bin/zoom-sdk-recorder` that reads our job JSON, writes the
   sample's `config.txt`, runs the binary, and on SIGTERM muxes the raw YUV +
   PCM output into the requested MP4 with FFmpeg.

Contract (what our Python adapter launches):

```bash
/opt/multistream/bin/zoom-sdk-recorder --job /opt/multistream/run/recording-jobs/job-….json
```

The job JSON contains:

```json
{
  "meeting_number": "89742214086",
  "token": "<Meeting SDK JWT>",
  "meeting_password": "<passcode, empty when none>",
  "recording_token": "<local recording token, empty when unavailable>",
  "display_name": "ISKCON Deoghar Archive",
  "output_path": "/var/lib/multistream/recordings/zoom-….mp4",
  "sdk_key": "…"
}
```

Those keys map onto the sample's `config.txt`, plus the display name it
hard-codes in `withoutloginParam.userName`.

Until that binary exists, deploy installs a placeholder that exits with an error
and the scheduler emails “SDK recorder not installed”.

### Capacity warning

On the current `Standard_B2s` (2 vCPU / 4 GB) the bot decodes meeting video and
re-encodes it to MP4 while MediaMTX and the YouTube/Facebook pushes are already
running. Expect CPU contention, and do not combine recording with the **enhance
audio/video** toggle at this VM size — `B2ms`/`B4ms` is where both fit.

## 4) Owner UI schedule

1. Log in with the **owner** PIN
2. Section 4 → **Scheduled Zoom recording**
3. Enable, set meeting ID, bot display name
4. Per weekday, enter slots in IST as `HH:MM-HH:MM` (multiple: comma-separated)
5. Save

Example: Monday–Thursday `05:00-18:00`

## 5) Emails

Using the existing SMTP Key Vault secrets (`smtp-user` / `smtp-password`):

- Meeting not running during an active slot (once per window)
- Recording successfully uploaded to Blob (**every save**)
- Upload failure / SDK missing / retention purge summary

## 6) Services on the VM

```bash
systemctl status multistream-recording.timer
journalctl -u multistream-recording.service -n 50
/opt/multistream/ui/.venv/bin/python -m recording status
/opt/multistream/ui/.venv/bin/python -m recording tick
```

Weekly purge: `multistream-recording-purge.timer` (Sundays 03:30).

## Notes

- The bot **appears as a participant** with the configured display name.
- It does **not** use Zoom Cloud Recording (no Zoom “recording” banner from that path).
- Consent and temple policy are your responsibility — tell attendees if required.
