# Security policy

shipit runs `docker build` on code from the repositories you point it at and controls Docker on its host,
so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through GitHub:
<https://github.com/desnarong/shipit/security/advisories/new>

Include what you found, how to reproduce it, and the impact. You'll get a first response within a few days.

## Supported versions

Only the latest release receives security fixes while the project is pre-1.0.

## Hardening tips for operators

- Run `shipit serve` behind HTTPS (e.g. Caddy) and keep the webhook secret private.
- Only deploy repositories you trust: building an image executes their Dockerfile.
- Run shipit as a dedicated user with access to Docker, and restrict who can reach port 9000.
