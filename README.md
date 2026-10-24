# Airtime Reward website — screenshot evidence and manual review

## Local run (Windows)

Open PowerShell in this project folder and run:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
flask --app app run --debug
```

Then open http://127.0.0.1:5000.

The website uses a manual evidence-review workflow. Automated Google Forms eligibility checks and automated airtime delivery are not used in this workflow. No real airtime purchases are made by the app.

## Applicant flow

1. Applicant enters a Nigerian mobile number once.
2. The website embeds the original Google Form and prefills only the phone number.
3. Applicant selects the referrer `Kelechi Ndubueze`, completes and submits the Google Form.
4. Applicant checks a confirmation box on the website to unlock evidence upload.
5. Applicant uploads two screenshots: the selected referrer page and the Google Forms submission confirmation. The phone number is already collected by the website, so no phone-number screenshot is required.
6. Applicant chooses the intended mobile network and submits evidence.
7. Evidence is stored outside the static/public directory, and the submission status becomes `pending`.
8. The owner signs into the private review dashboard at `/review`, downloads the screenshots, and approves or rejects the evidence.
9. The owner handles any approved airtime manually outside the website. Approval in the website does not send airtime.

## Important limitation

The site cannot independently verify a submission in the original Google Form because the owner has not granted API or response-sheet access. The confirmation checkbox only records the applicant's declaration and unlocks upload; it does not prove that Google accepted a response. Screenshots can be altered, so the owner must inspect them and should treat them as evidence, not definitive verification.

## Private review dashboard

A strong `ADMIN_REVIEW_PASSWORD` has been generated in your local `.env` if one was not already configured. Open that local file to copy the value when signing in at `/review`; the value was not printed in chat. Do not share it or commit it to Git. Use HTTPS and a secret key in any deployed environment. Do not deploy without reviewing the security configuration first.

Screenshots are stored in `instance/evidence`, not under `static`. The review-image endpoint requires an authenticated reviewer session and downloads files as attachments. Only PNG, JPG/JPEG, and WebP extensions with matching file signatures are accepted. The total request limit is 12 MiB and each file is limited to 4 MiB.

## Duplicate prevention

The `evidence_submissions.phone` column has a unique database constraint. This prevents a second evidence submission for the same normalized phone number. Existing legacy claim history checks are retained in the earlier code but are not used as proof of eligibility in the manual workflow.

## Tests

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use mock integrations and a temporary evidence directory. They do not access Google, send airtime, or deploy the site.

## Before production

- Configure a strong secret key and reviewer password, HTTPS, backups, monitoring, and retention/deletion rules.
- Consider adding stronger admin authentication (for example, MFA), reviewer audit logs, and a deletion schedule for screenshots containing personal information.
- Do not collect more personal information than necessary; tell users how screenshots are used and how long they are retained.
- Do not switch on automated airtime or provider credentials without a separate, explicit approval and provider validation.


## Production persistence and reviewer protection

The application supports local disk for development or private Cloudflare R2 for production. Set EVIDENCE_STORAGE=r2 and configure R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_BUCKET_NAME. Keep the bucket private. Create an R2 API token limited to that bucket and store its values as deployment secrets. Screenshots are stored as private objects and retrieved only after reviewer authorization.

Use managed PostgreSQL for production by setting DATABASE_URL to the provider's PostgreSQL connection string. The app rejects SQLite when APP_ENV=production and enables SQLAlchemy connection health checks for PostgreSQL. Do not copy local dev.db into production as a substitute for a managed database. Before launch, run a reviewed database migration and configure automated backups and restore testing.

### Cloudflare Access (required for production)

Create a Cloudflare Access self-hosted application for the exact production hostname and path /review* (include /review and all /review/... routes). Create an allow policy limited to the owner's verified email identity. In the app deployment environment, set CLOUDFLARE_ACCESS_TEAM_DOMAIN to the Access team hostname without https:// and CLOUDFLARE_ACCESS_AUD to the Access application's audience tag. Also set APP_ENV=production, SECRET_KEY, ADMIN_REVIEW_PASSWORD, a PostgreSQL DATABASE_URL, and the R2 settings above.

The app validates the signed Cf-Access-Jwt-Assertion JWT (signature, issuer, audience, and expiry) on every reviewer request. Keep the Access application in front of the origin as well; do not expose an alternate unprotected production hostname. The existing reviewer password remains a second check. Production startup intentionally fails if PostgreSQL, R2, or Access settings are absent.

### Deployment platform note

This repository is a conventional Flask application using Pillow and SQLAlchemy. Wrangler is installed and authenticated, but a native Cloudflare Python Worker deployment is not configured; Python Worker package compatibility and persistent storage need a separately validated port. A practical first production topology is a Python host for Flask, managed PostgreSQL, private Cloudflare R2, and Cloudflare Access protecting the reviewer path. Do not set Wrangler build/deploy fields to arbitrary commands or publish before the production environment and database migration are configured.

### Secret hygiene

Never put real passwords, API tokens, database URLs, or R2 keys in this repository. Rotate any real password that was ever committed in .env.example.
