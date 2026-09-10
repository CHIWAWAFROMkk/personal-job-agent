# Privacy

Personal Job Agent is local-first. A normal installation stores the user's profile,
resume copies, API keys, job database, application records and generated materials
under `%LOCALAPPDATA%\PersonalJobAgent`, outside the application directory.

## What this repository contains

- Application source code and synthetic tests.
- Empty `data/private`, `data/inbox` and `data/output` sentinels.
- No maintainer resume, profile, contact details, address, API key, browser session,
  job history or local database.
- A privacy audit that runs in continuous integration and before release.

## External services

Users decide whether to configure an AI, search or map provider. Text is sent to a
provider only when the corresponding feature is used. Provider terms and retention
rules apply. Secrets are stored locally and are never returned by the Dashboard API.

## Contributor rule

Never submit real resumes, personal job records, contact details, addresses, API
credentials, browser profiles or private JD collections. Use synthetic fixtures. Run:

```powershell
py -3.13 .\scripts\privacy_audit.py .
```

If personal data is committed, stop sharing the affected revision and report it using
GitHub's private vulnerability reporting flow. Removing a file in a later commit does
not remove it from Git history.

