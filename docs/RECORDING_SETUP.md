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

The existing Server-to-Server OAuth app is still used only to check whether the
meeting is in progress (`meeting:read:meeting:admin`).

## 3) Install the Linux Meeting SDK recorder binary

Zoom ships a Linux Meeting SDK sample that can subscribe to raw audio/video and
write a file. Build that sample on the VM (or cross-build), then install it as:

```text
/opt/multistream/bin/zoom-sdk-recorder
```

Contract (what our Python adapter launches):

```bash
/opt/multistream/bin/zoom-sdk-recorder --job /opt/multistream/run/recording-jobs/job-….json
```

The job JSON contains:

```json
{
  "meeting_number": "89742214086",
  "token": "<Meeting SDK JWT>",
  "display_name": "ISKCON Deoghar Archive",
  "output_path": "/var/lib/multistream/recordings/zoom-….mp4",
  "sdk_key": "…"
}
```

Until that binary exists, deploy installs a placeholder that exits with an error
and the scheduler emails “SDK recorder not installed”.

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
