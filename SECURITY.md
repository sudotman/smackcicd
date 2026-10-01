# Security

## Reporting a vulnerability

Please report security problems privately, through the repository's private
vulnerability reporting ("Report a vulnerability" under the Security tab), or
by contacting the maintainers directly. Don't open a public issue. Include
what you found, how to reproduce it, and what an attacker could do with it.

## What smackcicd defends, and what it doesn't

smackcicd is designed to run on a build machine inside a trusted network.

- **Webhooks** are authenticated with an HMAC-SHA256 signature when
  `SMACKCICD_WEBHOOK_SECRET` is set (`init` generates one). Without a secret,
  anyone who can reach the port can queue builds of tags that exist.
- **Dashboard actions** (rebuild, cancel, pause, delete) require the admin
  token. Without one configured, they are refused.
- **Dashboard reads and downloads are not authenticated.** Anyone who can
  reach the port can see build history and logs, and download packages. Put
  the port behind a VPN, firewall rules or an authenticating reverse proxy if
  that matters.
- **Delete** only removes paths strictly inside the configured artifact and
  log folders, re-checked just before deletion.
- **Secrets** live in `secrets.env` or the environment, never in the TOML
  config, and are scrubbed from every log line the tool writes.
- **Building runs the project's own code**: build scripts, UBT modules and
  editor commandlets. Only point a runner at repositories you trust.
