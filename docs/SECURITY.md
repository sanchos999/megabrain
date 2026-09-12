# Security

- Bind to localhost by default; expose externally only behind TLS and an authenticated reverse proxy.
- Set `MEGABRAIN_API_TOKEN` or inject it from a secret file. Never commit tokens, database URLs, Redis passwords or `.env` files.
- Use a dedicated PostgreSQL role with only the required database privileges.
- Treat event payloads as potentially sensitive PII. Do not log raw payloads, capsules, prompts or search results.
- Restrict filesystem permissions on data, model and secret directories.
- Back up encrypted PostgreSQL and configuration secrets separately. Test restore before relying on it.
- Metrics contain counts and latency only; they do not contain memory content.
- Keep PostgreSQL, Redis and API on a private network. Add rate limiting at the reverse proxy.
- Report vulnerabilities privately to the repository owner before public disclosure.

LICENSE_DECISION_REQUIRED is intentional; no license is implied by this document.
