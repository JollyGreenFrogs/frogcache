# Security Policy

## Supported Versions

Only the latest commit on `main` of `frogcache` receives security fixes.

Version | Supported
------- | ---------
latest on `main` | yes
older | no

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report security issues privately via [GitHub's private vulnerability reporting](https://github.com/JollyGreenFrogs/frogcache/security/advisories/new).

Include as much detail as possible:

- A clear description of the vulnerability
- Steps to reproduce or a proof-of-concept
- The potential impact
- Any suggested mitigations if known

We will acknowledge receipt within **48 hours** and aim to release a fix within **14 days** for critical issues, depending on severity and complexity.

## Scope

frogcache is a self-hosted LAN artifact cache: an nginx autoindex over cached
downloads plus an updater that fetches upstream artifacts. Of particular
interest:

- Path traversal / arbitrary file write via artifact names or upstream responses
- SSRF via attacker-controlled `seed/artifacts.json` source URLs
- Secret leakage (ntfy token, GITHUB_TOKEN) via logs, the manifest, or the HTTP index
- Cache poisoning (wrong file served for a pinned name/version)

## Out of Scope

- Vulnerabilities in the upstream artifacts being cached (report those upstream)
- Issues requiring physical access to the host
- Social engineering attacks
