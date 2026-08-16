# Security

## Secrets

Keep Instagram access tokens in environment variables or a secret manager. Never commit `.env`, SQLite databases, media files, certificates, private keys, API responses, or process logs.

Public media URLs are fetched by Meta. Use unguessable object names or short-lived signed URLs when the content must not remain broadly discoverable.

## Platform policy

Use this project only with accounts and Meta apps you control. The code intentionally avoids password login automation, scraping, CAPTCHA bypasses, follow/unfollow automation, and unsolicited mass messaging.

## Reporting

Please open a private security advisory on GitHub for suspected vulnerabilities. Do not include real tokens or customer data in the report.
