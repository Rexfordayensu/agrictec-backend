# AgriCtec API

The backend for the AgriCtec Next.js app, which lives in its own repository (clone the two side by
side; the app's `deploy/` folder builds this one from `../../backend`). It's built with
**FastAPI**, **PostgreSQL** (via SQLAlchemy 2 and Alembic) and **Python 3.12+**. It owns every piece
of data the browser used to keep in localStorage. It authenticates every request with httpOnly
sessions, hashes passwords with argon2, and enforces the admin role matrix on the server. It
writes the audit log in the same transaction as each change.

Every rule from the frontend's `lib/` folder runs here, and none of them are approximations:

- the single credit score and the eligibility checks
- amortized repayments and the loan pipeline transitions
- the seven risk rules
- announcement audiences and the assistant's keyword router
- the yield model, the reports and the report builder
- the deterministic demo seed

JSON is **camelCase**, with the same shapes as the app's `types/index.ts` and `types/admin.ts`.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
python -m pip install -r requirements-dev.txt
copy .env.example .env            # then set DATABASE_URL (and the rest) in .env
createdb -U postgres agrictec     # or create the database in pgAdmin
alembic upgrade head              # creates the tables and the audit-log trigger
uvicorn app.main:app --reload     # http://localhost:8000/docs
```

With `SEED_DEMO_DATA=true`, the first start builds the demo world. It has about 60 farmers in all
16 regions, 25 loan applications, 12 pending verifications, risk flags from the planted cases, 3
announcements and the team:

| Who                     | Email                         | Password                                   |
| ----------------------- | ----------------------------- | ------------------------------------------ |
| Super Admin (no `.env`) | `superadmin@agrictec.app`     | `Admin@2026`, which must be changed first |
| Admin                   | `efua.asantewaa@agrictec.app` | `Team@2026`                                |
| Support                 | `kofi.adjei@agrictec.app`     | `Team@2026`                                |
| Analyst                 | `yaw.darko@agrictec.app`      | `Team@2026`                                |
| Any seeded farmer       | from `GET /admin/farmers`     | `Farmer@2026`                              |

Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` to use your own Super Admin. They are applied once per
value, so a password you later change in the console is kept.

### Tests

```bash
createdb -U postgres agrictec_test
set TEST_DATABASE_URL=postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5432/agrictec_test
python -m pytest
```

There are 78 tests. Each one runs against a database built by the real migrations and emptied
before every test. Outside services are replaced with recorded responses, so the tests never
need the network. **The test database is wiped, so never point it at real data.**

## Production

`ENVIRONMENT=production` refuses to start unless all of these hold:
- `SECRET_KEY` is random and 32+ characters
- `COOKIE_SECURE` is on
- demo seeding is off
- `ADMIN_EMAIL` and `ADMIN_PASSWORD` are set, so the demo super admin can't exist

`TRUSTED_PROXIES` takes addresses or networks (e.g. `172.28.0.0/16`). Several workers can start
at once: the startup defaults are created under a PostgreSQL advisory lock. `Dockerfile` here
and the app repository's `deploy/` folder package it all for a server.

### On Render

`render.yaml` is a Render Blueprint for the API on the free plan in Frankfurt. The database is
Supabase's free PostgreSQL, because Render's free database is deleted after 30 days.

To set it up, choose **New > Blueprint** in Render and pick this repository. Render asks for:
- `DATABASE_URL`: Supabase's **Session pooler** string, found under Connect > Session pooler.
  It works over IPv4, and session mode keeps LISTEN/NOTIFY and advisory locks working.
- `ADMIN_EMAIL` and `ADMIN_PASSWORD`.

Render generates `SECRET_KEY` itself. `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` keep one worker
under the session pooler's limit of about 15 connections.

The free plan is for trying the app out:
- The API sleeps after 15 minutes idle and takes about a minute to wake. An uptime monitor
  pinging `/api/v1/health` keeps it awake.
- Scheduled reports don't run while it sleeps.
- Ports 465 and 587 are blocked, so set `SMTP_PORT=2525` for Brevo.

The `starter` plan lifts all of these. Set `PUBLIC_APP_URL` and `CORS_ORIGINS` to the frontend's
address once it's online.

### Backups

Supabase's free plan keeps no backups, so `.github/workflows/backup.yml` makes one every night at
01:30 UTC. It has four steps:
1. It copies the `public` schema with `pg_dump`.
2. It restores the copy into a throwaway PostgreSQL of the same version, to prove it works.
3. It encrypts the copy with GPG (AES-256).
4. It keeps the copy as a workflow artifact for 30 days.

It needs two repository secrets:
- `BACKUP_DATABASE_URL`: the same string as `DATABASE_URL`.
- `BACKUP_PASSPHRASE`: without it, no backup can be opened, so keep a copy somewhere safe.

GitHub emails the owner when a run fails. **Run workflow** on the Actions tab makes a backup on
demand.

To restore one:
1. Download the artifact from the run's page and unzip it.
2. Decrypt it:

   ```sh
   gpg --output agrictec.dump --decrypt agrictec-YYYY-MM-DD.dump.gpg   # asks for the passphrase
   ```

3. Stop the API (Render: **Suspend**), so nothing writes while you restore.
4. Replace the schema. The copy creates the `public` schema itself:

   ```sh
   psql "$DATABASE_URL" -c "drop schema public cascade"
   pg_restore --dbname="$DATABASE_URL" --no-owner --no-privileges --exit-on-error agrictec.dump
   ```

5. Resume the API.

Test a restore against a scratch database first, never straight on production. Use a
`pg_restore` at least as new as the server's PostgreSQL.

### Error alerts

Admins who can manage settings can get a phone notification when something breaks. They turn it
on per device under **System settings > Error alerts**.

Two things send an alert:
- an unhandled error in a request
- a failure of the background jobs

Each kind of error is sent at most once every 15 minutes. An alert names the route and the error's
type, never request data. The full traceback stays in the server log.

It is standard web push (`app/integrations/push.py`) through the browser's own push service, with
no account anywhere:
- The VAPID key is derived from `SECRET_KEY`. Rotating `SECRET_KEY` means turning alerts on again.
- On iPhone, alerts need AgriCtec added to the Home Screen and opened from there.
- An alert can't arrive while the API itself is down or asleep. That is the uptime monitor's job.

## Layout

```
app/
  main.py          FastAPI app, CORS, error format, startup bootstrap
  config.py        settings from the environment / .env
  models.py        the tables
  domain/          pure business rules (ports of the frontend's lib/)
  schemas/         request/response bodies (camelCase)
  services/        database work, one module per frontend service; every write audited
  routers/         thin HTTP routes
  integrations/    outside services: weather, market prices (MoFA, WFP), Paystack, SMS/email, Smile ID
  core/            sessions, permissions dependency, audit helper, errors, rate limits, live events
alembic/           migrations (0001 creates everything, plus the append-only audit trigger;
                   0002 adds payments, notifications and rate limits)
tests/
```

## How the frontend connects

The Next.js app's services (`services/*.ts`) call this API through `services/api.ts`, on the
app's own origin: `next.config.ts` rewrites `/api/v1/*` to `BACKEND_URL` (default
`http://127.0.0.1:8000`). The session cookies therefore belong to the app's site, and `proxy.ts`
can see them for its redirects. To serve the API on another origin instead, set
`NEXT_PUBLIC_API_URL` in the app and list the app in `CORS_ORIGINS` here.

## Conventions

- **Base path:** `/api/v1`. Interactive docs are at `/docs`.
- **Errors:** `{"detail": "<the message the UI shows>", "code": "<machine code or null>"}`. The
  messages are the same as the old services threw, so the toasts read the same. Codes the client
  should handle:
  - `not_signed_in` and `session_expired` (401)
  - `account_suspended`, `password_change_required`, `read_only`, `plan_required` and
    `feature_off` (403)
  - `locked` (423)
  - `maintenance` (503)
- **Dates:** timestamps are `2026-10-27T09:30:00.000Z`, the same format as `toISOString()`, and
  plain dates are `yyyy-MM-dd`. Everything is UTC (Ghana keeps GMT).
- **Optional fields:** these are `null` rather than missing.
- **Auth:** browsers use cookies. Send requests with `credentials: 'include'`. Other clients can
  send the `token` from the sign-in response as `Authorization: Bearer <token>`.

## Endpoint map: frontend service → API

| Frontend service                                         | API                                                                 |
| -------------------------------------------------------- | ------------------------------------------------------------------- |
| `auth.service`                                           | `POST /auth/signup`, `/auth/login`, `/auth/guest`, `/auth/logout`, `/auth/replace-temporary-password`, `/auth/password-reset/request`, `/auth/password-reset/confirm`, `GET /auth/session` |
| `profile.service`, `onboarding.service`                  | `GET/PUT /me/profile`, `GET/PUT/DELETE /me/onboarding/draft`, `POST /me/onboarding/complete` |
| `records.service`, `momo.service`                        | `GET/POST/DELETE /me/records` (DELETE clears all), `PATCH/DELETE /me/records/{id}`. The same under `/me/momo` |
| `diagnoses.service`, `chat.service`, `settings.service`  | `GET/DELETE /me/diagnoses`, `GET/DELETE /me/chat` (both written by the API only: `POST /me/crop-doctor` and `POST /me/assistant` save them), `/me/settings` |
| `loans.service`                                          | `GET/POST /me/loans/applications`, `POST /me/loans/eligibility`, `GET /platform/lenders` |
| `platform.service`                                       | `GET /platform` (one snapshot), `GET /me/announcements`, `POST /me/announcements/{id}/dismiss`, `GET/POST /me/verifications` (a phone needs `code`, texted by `POST /me/phone/code`; a farm pin and a Ghana Card wait for review) |
| `subscription.service`                                   | `GET/POST /me/subscription`, `POST /me/subscription/checkout`, `/me/subscription/confirm`. Credits are spent by `POST /me/assistant` and `POST /me/crop-doctor` |
| `notifications.service` (top-bar bell)                   | `GET /me/notifications`, `POST /me/notifications/read`              |
| `use-farm-stats` (score, readiness)                      | `GET /me/credit`                                                    |
| `lib/ai/*`, yield predictor                              | `POST /me/assistant`, `POST /me/crop-doctor` (multipart), `POST /me/yield` |
| `lib/data/backup.ts`                                     | `GET /me/export`, `POST /me/import`, `POST /me/delete-data`         |
| `market.service`, `weather.service`, `geocoding.service` | `GET /platform/market/prices`, `/platform/market/trends`, `POST /platform/weather`, `/platform/market/research`, `GET /geocoding/search?q=` |
| `admin-auth.service`                                     | `POST /admin/auth/login`, `/logout`, `/touch`, `/change-password`, `PATCH /admin/auth/me`, `GET /admin/auth/session` (a lockout answers the sign-in with 423 and `until`) |
| `overview.service` (the console home)                   | `GET /admin/overview?range=30d|90d|12m|all`: every figure and chart, computed here (the console never downloads every farmer) ||90d\|12m\|all` (includes the badge counts) |
| `farmers.service`                                        | `GET /admin/farmers/page` (search, filters, sort and paging done here; `ids=1` for exports), `POST /admin/farmers/lookup` (only the farmers a queue names), `GET /admin/farmers/{id}`, `/{id}/detail`, and `POST` `/{id}/suspend`, `/reactivate`, `/delete`, `/notes`, `/reset-password`, `PUT /{id}/tags`, CSV exports |
| `verifications.service`                                  | `GET /admin/verifications`, `POST /{id}/approve`, `/{id}/reject`, `/approve-many` |
| `loan-applications.service`                              | `GET /admin/loan-applications` (filters), `GET /{id}`, `/{id}/review`, `POST /{id}/status`, `/export` |
| `lenders.service`                                        | `GET/POST /admin/lenders`, `PATCH/DELETE /{id}`, `POST /{id}/toggle` |
| `config.service`                                         | `PUT /admin/credit-config`, `POST /publish`, `/restore`, `/impact` (every farmer re-scored here), `GET /history`, `GET /scoring-inputs/{userId}` (one farmer, for the simulator), `PUT /admin/settings/flags`, `/plans` |
| `content.service`                                        | `GET/PUT /admin/content/market\|crops\|assistant\|diagnoses`, `PUT /weather-override`, `GET /meta`, `POST /assistant/test` |
| `announcements.service`                                  | `GET/POST /admin/announcements`, `PATCH/DELETE /{id}`, `POST /{id}/end`, `/reach`, `GET /admin/announcements/audience` (regions and crops only, to count reach) |
| `risk.service`                                           | `POST /admin/risk/scan`, `GET /admin/risk/flags`, `/{id}/related`, `POST /{id}/dismiss`, `/{id}/confirm`, `GET /admin/risk/rules` |
| `team.service`                                           | `GET/POST /admin/team`, `POST /{id}/role`, `/disable`, `/enable`, `/reset-password`, `GET /admin/roles` |
| `audit.service`                                          | `GET /admin/audit` (filters + paging), `/audit/actors`, `POST /audit/export` |
| `reports.service`                                        | `GET /admin/reports`, `/admin/reports/{id}`, `POST /{id}/export?format=csv\|pdf`, `/builder/preview`, `/builder/export`; schedules: `GET/POST /admin/reports/schedules`, `PATCH/DELETE /schedules/{id}` |
| `backup.service`, system status                          | `POST /admin/system/backup` (encrypted with the admin's passphrase: AES-256-GCM, key by scrypt; never stored), `/backup/summary` and `/restore` (need that passphrase), `/reset-demo`, `/delete-farmers`, `GET /admin/system/status` |
| market research (admin)                                  | `POST /admin/content/market/research`                               |
| Paystack (server to server)                              | `POST /payments/paystack/webhook` (signature checked)               |
| `impersonation.ts`                                       | `POST /admin/farmers/{id}/impersonate`, then `POST /auth/impersonation/claim` in the new tab, sending `X-Impersonation-Token` on its requests; `POST /auth/impersonation/end` |
| `use-platform-sync` (`storage` event)                    | `GET /events` (Server-Sent Events: `platform` / `account` topics)   |

## Security model

- **Sessions.** These are random 256-bit tokens in httpOnly, SameSite=Lax cookies, and the
  database stores only a SHA-256 of each token.
  - The cookie names match what `proxy.ts` already checks (`agrictec_session` and
    `agrictec_admin`), so its redirects keep working.
  - Farmer sessions last 30 days.
  - Admin sessions end after 30 idle minutes (the timeout is logged) and after 12 hours at most.
    Only writes and `POST /admin/auth/touch` count as activity, so a console left open and
    polling still times out.
- **Passwords.** These are hashed with argon2id.
  - Five failed admin sign-ins lock that email for five minutes.
  - Temporary passwords are one-time. A farmer or admin must replace theirs before doing
    anything else, and they never appear in the audit log.
  - Farmers who forget their password get a six-digit code by email (and SMS to a verified
    phone). The request's answer never says whether the email has an account. Codes:
    - are stored only as a keyed hash
    - expire after 15 minutes and allow five attempts
    - are limited to five per account a day

    Using one ends every other session and leaves a notice in the inbox. Without email or
    SMS configured, a development server logs the code instead.
- **Roles.** Every admin route checks a permission from the spec's matrix (`app/domain/permissions.py`).
  The account is re-read on every request, so a role change or a disabled account applies at
  once. Nobody can change their own access, and the last active Super Admin can't be demoted or
  disabled.
- **The audit log.** Each write's audit entry is committed together with the change. A
  PostgreSQL trigger refuses `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log` for everyone,
  including the application.
- **Farmer data.** Every `/me` query is scoped to the signed-in farmer, so another farmer's
  record IDs return 404.
  - Farmers can't verify themselves: the Ghana Card and verification flags change only through
    the verification flow and admin review.
  - Changing the phone number drops its verification.
- **Suspension** ends the farmer's open sessions at once. **Maintenance mode** closes `/me/*`
  with a 503 that carries the admin's message. Admins and the public `/platform` snapshot are
  unaffected.
- **Ghana Cards.** The full number is never stored. Only the mask (`GHA-******789-0`) and a
  SHA-256 fingerprint are kept, computed the same way the browser does.
- **Loans.** Applications are re-checked on the server: a verified card, every eligibility rule
  and the lender's limits and term. The rate, monthly payment and score are computed here, never
  taken from the request.
- **Plans and AI credits** are enforced on the server. Credits are held while a request runs, kept
  only on success, and released automatically if a request dies.
- **CSV exports** neutralize spreadsheet formulas. Backups never contain password hashes.
- **Rate limits** are counted per client IP in PostgreSQL, so they hold across workers:
  - sign-up 10 per hour, farmer sign-in 20 per 15 minutes, guest 10 per hour
  - admin sign-in 30 per 15 minutes, on top of the lockout (five failures lock an email from that
    address for five minutes, so nobody else can lock a real admin out)
  - phone codes 5 per 10 minutes (and 5 per account a day)
  - assistant 30, crop doctor 10, weather 60 and place search 30 per minute
  `X-Forwarded-For` is believed only from `TRUSTED_PROXIES`, and it is read from the right. The
  first entry that isn't a trusted proxy is used, because entries to its left can be made up by
  the visitor. Where a trusted proxy states the visitor's address in a header of its own (Render:
  Cloudflare's `CF-Connecting-IP`), name it in `TRUSTED_CLIENT_IP_HEADER`. Behind a hosted frontend (Vercel), set the same `FRONTEND_PROXY_SECRET` in the API
  and the web app; the app then vouches for each visitor's address. Without it, those visitors
  share the host's address.
- **Payments.** A plan is paid for only when Paystack confirms it. The return page and the
  webhook both re-verify the transaction with Paystack, and the amount and currency must match
  the plan exactly. Confirming twice is harmless. The webhook rejects anything without a valid
  `x-paystack-signature`. With Paystack configured, or in production, paid plans can't be
  switched on without a payment.

## Compared with the browser demo

- The guest is one per *device*, remembered by a signed cookie, not one per browser profile.
- "View as farmer" is per tab, as before: the viewing tab sends `X-Impersonation-Token`. Every
  write in that view returns 403 `Read-only while viewing as farmer`.
- The audit log has no 5,000-entry cap or archive. The database keeps everything, and the
  export covers every entry.
- The platform backup is version 2, a table-by-table JSON file. Version 1 files (localStorage
  dumps) aren't accepted.
- Farmer CSVs are built on the server. For report **PDFs**, the console still renders the
  returned report with `lib/pdf/generate-admin-report.ts`, and the export is logged either way.

## Outside services

| Service | What it does | Needs |
| --- | --- | --- |
| [Open-Meteo](https://open-meteo.com) | The farmer's 7-day forecast and storm warnings, cached 15 minutes per place. An admin's weather override still wins. | Nothing (free, no key) |
| [OpenWeather](https://openweathermap.org) | The backup when Open-Meteo refuses (its free limit is per IP, and Render's shared IPs run out by midday): a 5-day forecast with a chance of rain. Its limit is per key. | `OPENWEATHER_API_KEY` (free account) |
| [MET Norway](https://api.met.no) | The last backup, used without an OpenWeather key or when OpenWeather fails. It gives no chance of rain for Ghana, so the app shows "not given". | Nothing (free, no key) |
| [MoFA wholesale prices](https://fpma.fao.org/giews/fpmat4/) (SRID, via FAO's FPMA tool) and [WFP food prices for Ghana](https://data.humdata.org/dataset/wfp-food-prices-for-ghana) (HDX) | Evidence for the admin's market research: recent market prices by crop and region, cached a day. The admin still reviews and publishes prices. | Nothing (free, no key) |
| [Paystack](https://paystack.com) | Plan payments by MTN MoMo, Telecel Cash, AirtelTigo Money or card, in GHS. | `PAYSTACK_SECRET_KEY`, plus the webhook URL `https://YOUR_DOMAIN/api/v1/payments/paystack/webhook` in the Paystack dashboard |
| [Arkesel](https://arkesel.com) | An SMS copy of each notification to the farmer's phone. | `ARKESEL_API_KEY` and an approved `SMS_SENDER_ID` |
| SMTP | An email copy of each notification, for farmers with an email address. | `SMTP_HOST`, `SMTP_FROM` (and a username/password if the server needs one) |
| [Smile ID](https://usesmileid.com) | Checks a submitted Ghana Card number and name against the NIA register. | `SMILE_ID_PARTNER_ID`, `SMILE_ID_API_KEY`; `SMILE_ID_SANDBOX=false` for live checks |
| [Claude](https://platform.claude.com) (Anthropic) | The assistant's answers, written from the farmer's own figures, and the crop doctor's reading of a photo. | `ANTHROPIC_API_KEY`; `ANTHROPIC_MODEL` (default `claude-opus-5-5`) |

Keep these points in mind:

- **Notifications** are always saved in the app. The farmer sees them under the top-bar bell.
  They're created when a verification is approved or rejected, when a loan application moves
  through the pipeline, and when an account is suspended, reactivated or has its password reset.
  SMS and email are sent in the background only when configured, and a failed send never blocks
  the admin's action. A password-reset notice never contains the temporary password.
- **The Smile ID result is advice, not a decision.** The admin reviewing the card sees it next to
  the request. The card number is sent to Smile ID once and still never stored. The integration
  follows Smile ID's published API for an "ID Verification" job with `GHANA_CARD`, but it hasn't
  been run against a live Smile ID account. Try it in the sandbox before relying on it.
- **Market price coverage is uneven.** Research uses whichever source has the newer price for a
  market. As of October 2026:

  | Source | Markets | Newest prices |
  | --- | --- | --- |
  | MoFA via FAO | Tamale (Northern), Techiman (Bono East), Bolgatanga (Upper East) | May 2026 |
  | MoFA via FAO | Wa (Upper West) | March 2026 |
  | MoFA via FAO | Accra Agbogbloshie (Greater Accra) | December 2024 |
  | MoFA via FAO | Kumasi (Ashanti) | August 2023 |
  | WFP | Bono, Volta, Oti and others | July 2023 |

  MoFA's prices are wholesale per package (e.g. a 100 kg bag of maize). Research says when
  evidence is over a year old. If either source is down, research uses the other.

  Some regions have no current market: Western, Central, Eastern and the rest of the south. For
  those, a paid source such as Esoko can be added behind `app/integrations/market_prices.py`.
- **When a service is unreachable,** weather and research answer `unavailable` and the app
  says so; nothing is invented.
- **Claude.**
  - **The assistant.** Claude gets the farmer's summary figures: score, record totals, Ghana Card
    status, weather when the question needs it, and published prices for their crops. It also
    gets their last ten chat messages, and the knowledge base's answer when one matches, so the
    admin's content still guides it.
  - **The crop doctor.** It sends only the photo, scaled down and re-encoded as JPEG (HEIC has to
    be converted on the phone first). A photo with no plant, or a failed request, costs no
    credits.
  - **Fallbacks.** Answers come back as structured JSON in the app's existing shapes. A declined
    request is retried server-side (`fallbacks: "default"`). When Claude can't answer, the
    assistant uses the knowledge base instead.
  - **Cost.** Each question or scan is one paid API request. Claude Opus 5.5 costs $4 / $20 per
    million input and output tokens. `ANTHROPIC_MODEL=claude-sonnet-5-5` or
    `claude-haiku-5-5` costs less, if their answers are good enough for you.
  - **Without a key,** the assistant answers from the knowledge base and the crop doctor is
    unavailable: it never makes up a diagnosis.
- **Weather and price alerts** (app/services/farm_alerts.py) reach farmers who leave them on in
  Settings > Preferences: in the app's inbox, and by SMS and email when those are configured.
  - Weather, at 06:00 and 16:00 Ghana time (one worker per run): the 7-day forecast at the farm's
    pin (or region). Heavy rain (20 mm+ in a day, 60%+ chance) or a thunderstorm in the next 3 days,
    or a dry week (under 2 mm) in the zone's growing season. No forecast, no alert.
  - Prices, when an admin publishes them: a reviewed price that moved 10%+ against the previous
    observation of the same crop, unit and place, to farmers who grow that crop (a regional price
    only to that region).
  - Each alert goes out once (table `farm_alerts`).
- **Scheduled reports** go out every Monday or every 1st of the month at 07:00 Ghana time, with
  the report's CSV attached (PDFs are drawn in the browser, so they can't be emailed).
  - A background thread in each worker checks every minute. It claims due rows with
    `FOR UPDATE SKIP LOCKED`, so several workers send each report once.
  - Without email set up, a due report is recorded as failed and moves on to its next time.
- **Several API workers** share live events through PostgreSQL `LISTEN/NOTIFY`
  (channel `agrictec_events`), so `uvicorn --workers N` or several hosts work without Redis.


## Private file storage

Lender logos are stored in the existing PostgreSQL database (`stored_files`), using
`DATABASE_URL`. No Cloudflare account, bucket, R2 API token or separate file service is needed.
The API still checks access before returning a logo. Uploads accept PNG, JPEG or WebP up to
2 MB, then resize and re-encode them as WebP. File changes and lender changes commit together.
Files are included in database backups and encrypted platform exports.

Apply migration 0009 before running the updated API:

```sh
alembic upgrade head
```

For a new system, that is all the storage setup required. Existing R2 environment variables
can be removed; the API ignores them. If logos were already uploaded to R2, copy them before
switching the running API and removing the old credentials:

```sh
pip install -r requirements-r2-migration.txt
python -m app.scripts.migrate_r2_logos
```

The one-time command reads the old `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
and `R2_BUCKET` from the environment or `.env`. It copies only referenced logos missing from
PostgreSQL, preserves their keys and URLs, verifies their bytes, and never deletes R2 objects.
It can be rerun after a failure. After a successful copy, deploy the new API, check the logos,
and remove the old credentials. Alternatively, re-upload the original logos in the admin console.
