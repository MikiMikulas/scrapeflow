# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through
GitHub: **Security → Report a vulnerability** in this repository
(<https://github.com/MikiMikulas/scrapeflow/security/advisories/new>).

Include what you found, how to reproduce it and which version you run. You'll get an answer as soon as
possible; fixes are released as a new version and credited in the changelog unless you prefer otherwise.

## Supported versions

Security fixes go into the latest release.

## Things to know when running ScrapeFlow

- The **Python code** flow node runs code on the server and is **not a sandbox**. Anyone who can sign
  in can run code with the permissions of the ScrapeFlow process.
- API keys of model providers and storage secrets are stored in the SQLite database in the data volume.
- Expose ScrapeFlow to the internet only behind HTTPS. See [docs/accounts.md](docs/accounts.md).
