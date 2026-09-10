# Contributing

Contributions are welcome through Issues, Discussions and pull requests.

## Ground rules

1. Open an Issue for behavior changes so the problem and acceptance criteria are clear.
2. Keep pull requests focused and preserve the local-first privacy boundary.
3. Use synthetic people, companies, resumes, jobs and contact details in tests and docs.
4. Never bypass CAPTCHA, anti-automation controls or a site's terms. Final application
   submission must remain a user action.
5. Do not turn unverified profile claims into resume facts.

## Development on Windows

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe .\scripts\privacy_audit.py .
.\.venv\Scripts\python.exe -m pytest -q
```

Before requesting review, describe the user-visible change, tests run, privacy impact
and any migration required. By contributing, you agree that your contribution is
licensed under the MIT License.

