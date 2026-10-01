# Forges

smackcicd talks to **Gitea**, **Forgejo** and **GitHub** (github.com and
Enterprise Server). Each gets:

- **New tags**, via polling (always) and webhooks (optional, instant).
- **Releases**: created or updated per tag, with release notes, the APK and
  the build record attached, and download links for the big zips.
- **Commit status**: pending while building, then success or failure.

With `forge = "none"`, tags come from `git ls-remote` and nothing is
published. Builds still run and appear on the dashboard.

`init` detects the forge. github.com is recognised by name; for any other
host it asks the server, since a self-hosted forge can live at any address.

## Tokens

Put the token in `secrets.env` as `SMACKCICD_FORGE_TOKEN`.

### GitHub

Create a **fine-grained personal access token** (or a GitHub App token) for
the repository with:

- Contents: **Read and write** (releases, assets, tags)
- Commit statuses: **Read and write**
- Metadata: Read (granted automatically)

For a private repository the build machine also has to fetch it. Either give
the runner's account git credentials, or set `repo.git_user = "x-access-token"`
so git fetches with the same token. `init` does this for GitHub.

GitHub caps a release asset at just under 2 GiB. Larger files are never
attached; they are linked from the release notes instead.

### Gitea / Forgejo

Create a token under **Settings → Applications** with the `write:repository`
scope. To fetch a private repository over HTTPS with it, set `repo.git_user`
to your Gitea user name.

Gitea's attachment size limit defaults to just a few MB. Raise it in
`app.ini` if you attach anything large (by default only the APK and small
files are attached):

```ini
[attachment]
MAX_SIZE = 4096       ; MB
```

## Webhooks

Polling finds new tags within `poll_seconds`. A webhook makes builds start at
once. `smackcicd webhook` prints the values to enter.

| Field | Value |
| --- | --- |
| URL | `http://<build-machine>:9099/webhook` |
| Content type | `application/json` |
| Secret | `SMACKCICD_WEBHOOK_SECRET` from `secrets.env` (`smackcicd webhook --show-secret`) |
| Events | GitHub: *Branch or tag creation* and *Releases*. Gitea: *Create*, *Push* and *Release*. |

Signatures are checked for all three forges: GitHub's `X-Hub-Signature-256`,
and Gitea's and Forgejo's own headers. Tag deletions are ignored.

### Gitea refuses to deliver to a LAN address

Since 1.17, Gitea and Forgejo refuse webhooks to private addresses unless
allowed. Deliveries then fail before they are sent. Add to `app.ini` and
restart Gitea:

```ini
[webhook]
ALLOWED_HOST_LIST = private
```

### "no route to host" in the delivery log

The forge cannot reach the build machine: a firewall on either side, or the
two are on different networks. Test from the forge's host with
`curl http://<build-machine>:9099/healthz`. Polling keeps working meanwhile.

### GitHub cannot reach a build machine on your LAN

github.com cannot call into a private network. Rely on polling (the default),
or expose only the webhook route through a tunnel or reverse proxy.
