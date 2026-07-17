# Authorized live smoke run

Do not run this procedure until Kindertales has provided written authorization
for the account, linked children, session reuse, requested date range, and the
configured quotas.

Use a narrow range known to contain at most a few media objects:

```console
kindertales-scraper sync --from YYYY-MM-DD --through YYYY-MM-DD --dry-run --headed
kindertales-scraper sync --from YYYY-MM-DD --through YYYY-MM-DD --headed
kindertales-scraper verify
kindertales-scraper credentials delete
```

Confirm the following before considering the adapter accepted against the live
application:

- cached state is validated and a rejected session causes exactly one fresh
  login;
- MFA can be completed in the headed browser;
- every linked child in the bounded response appears in `index.sqlite3`;
- discovered photos and videos have matching activity relationships;
- request timestamps satisfy every authorized rolling quota;
- source URLs, diagnostics, and sidecars contain no cookies, authorization
  values, bearer tokens, or signed query values;
- source and final hashes verify, and authentic capture/GPS fields are unchanged;
- missing time/GPS values use activity and center context with inference flags;
- a second run is idempotent and uses the configured overlap.

Delete the smoke archive after review unless its retention is part of the
authorization. Do not turn live responses into fixtures. If the observed API
paths or payload shapes differ from the adapter defaults, retain only a
synthetic, redacted reproduction of the shape in Git.
