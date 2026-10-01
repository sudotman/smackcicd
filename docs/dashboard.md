# The dashboard

`http://<build-machine>:9099/` (see `[server]`). It shows:

- **State**: Idle, Building, or Paused, plus the queue, the engine, free disk
  space and when tags were last checked.
- **Building now**: the running tag and platform, elapsed time, and the live
  log, with Cancel.
- **Builds**, one card per tag with a row per platform: status, duration, and
  **Download** buttons for the zip and APK.
- **Details**, per build: commit, author, engine, signing, error and warning
  counts, every deliverable with its size and SHA-256, the staged files grouped
  by folder, and the end of the log, with a link to the full log.

## Admin actions

Rebuild, Cancel, Pause and Delete need the admin token, `SMACKCICD_ADMIN_TOKEN`
in `secrets.env`. Paste it into the box at the top right once; the browser
remembers it. Without a token configured on the runner the dashboard is
read-only.

**Delete** shows exactly what it will remove before asking you to confirm:

- Rebuilds of a tag share one drop folder, so deleting an old attempt removes
  only its history entry. The files go when no remaining build uses them.
- Deleting a tag's last build removes its folder, logs and index entry.
- A running build cannot be deleted. Nothing outside the artifact and log
  folders is ever touched.
- A deleted tag is **not** rebuilt by itself. Releases on the forge are left
  alone.

## API

Reads need no authentication:

| Route | Returns |
| --- | --- |
| `GET /healthz` | liveness, paused flag, uptime |
| `GET /api/overview` | health, queue, recent builds with their downloads |
| `GET /api/build?id=N` | one build: facts, deliverables, staged files |
| `GET /api/log?build=N&tail=1` | the end of a build's log (`build=live` for the running one; `offset=` to follow) |
| `GET /api/daemon-log` | the tail of `smackcicd.log` |
| `GET /logs/N` | download a build's full log |
| `GET /artifacts/<path>` | download a file from the artifacts folder |

Actions are `POST /api/actions/{build,retry,cancel,pause,delete}` with the
admin token in an `X-Smackcicd-Token` or `Authorization: Bearer` header:

```bash
curl -X POST -H "Authorization: Bearer $SMACKCICD_ADMIN_TOKEN" \
  -d '{"tag": "v1.4.2-beta.3", "platforms": ["Android"]}' \
  http://buildpc:9099/api/actions/build
```

`delete` takes `{"buildIds": [..], "dryRun": true}` to preview what would be
removed.

## Exposure

The dashboard is meant for a trusted network. Reads are open, and download
links work for anyone who can reach the port. Put it behind a VPN or a reverse
proxy with authentication before exposing it any further.
