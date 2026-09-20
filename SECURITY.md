# Security Policy

Security and privacy fixes are accepted for the latest release.

Please use GitHub's private vulnerability reporting flow from the repository Security
tab. Do not place API keys, resumes, contact details, addresses, browser data or exploit
details in a public Issue. Include the affected version, impact, reproduction steps and
a minimal synthetic example.

The maintainers will acknowledge a valid report when available, investigate it, and
coordinate a fix before public disclosure. This community project cannot guarantee a
specific response time.

## Local trust boundary

The dashboard is a single-user loopback service, not an authentication boundary
between operating-system users or untrusted programs running on the same computer.
Explicit foreign browser origins and cross-site Fetch Metadata are rejected. Paired
extensions use an independent Agent Token and a restricted set of endpoints; a token
does not grant foreign-origin access to the dashboard or pairing/settings pages.

These checks do not establish a private desktop session and must not be advertised
as protection against a privileged extension or local client able to omit browser
metadata. Do not expose the service through a reverse proxy, LAN bind, port forward,
or public tunnel. Only install trusted browser extensions and keep pairing tokens
private. Full private-session authentication for sensitive reads remains planned.
