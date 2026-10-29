# AgriCtec API

The backend for the AgriCtec Next.js app, which lives in its own repository (clone the two side by
side; the app's `deploy/` folder builds this one from `../../agrictec-backend`). It's built with
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
| `records.service`, `momo.service`                        | `GET/POST/PUT /me/records`, `PATCH/DELETE /me/records/{id}`. The same under `/me/momo` |
| `diagnoses.service`, `chat.service`, `settings.service`  | `/me/diagnoses`, `/me/chat`, `/me/settings`                         |
| `loans.service`                                          | `GET/POST /me/loans/applications`, `POST /me/loans/eligibility`, `GET /platform/lenders` |
| `platform.service`                                       | `GET /platform` (one snapshot), `GET /me/announcements`, `POST /me/announcements/{id}/dismiss`, `GET/POST /me/verifications` |
| `subscription.service`                                   | `GET/POST /me/subscription`, `POST /me/subscription/checkout`, `/me/subscription/confirm`. Credits are spent by `POST /me/assistant` and `POST /me/crop-doctor` |
| `notifications.service` (top-bar bell)                   | `GET /me/notifications`, `POST /me/notifications/read`              |
| `use-farm-stats` (score, readiness)                      | `GET /me/credit`                                                    |
| `lib/ai/*`, yield predictor                              | `POST /me/assistant`, `POST /me/crop-doctor` (multipart), `POST /me/yield` |
| `lib/data/backup.ts`                                     | `GET /me/export`, `POST /me/import`, `POST /me/delete-data`         |
| `market.service`, `weather.service`, `geocoding.service` | `GET /platform/market/prices`, `/platform/market/trends`, `POST /platform/weather`, `/platform/market/research`, `GET /geocoding/search?q=` |
| `admin-auth.service`                                     | `POST /admin/auth/login`, `/logout`, `/touch`, `/change-password`, `PATCH /admin/auth/me`, `GET /admin/auth/session`, `GET /admin/auth/lockout` |
| overview (`lib/admin/overview.ts`)                       | `GET /admin/overview?range=30d\|90d\|12m\|all` (includes the badge counts) |
| `farmers.service`                                        | `GET /admin/farmers`, `/admin/farmers/{id}`, `/{id}/detail`, and `POST` `/{id}/suspend`, `/reactivate`, `/delete`, `/notes`, `/reset-password`, `PUT /{id}/tags`, CSV exports |
| `verifications.service`                                  | `GET /admin/verifications`, `POST /{id}/approve`, `/{id}/reject`, `/approve-many` |
| `loan-applications.service`                              | `GET /admin/loan-applications` (filters), `GET /{id}`, `/{id}/review`, `POST /{id}/status`, `/export` |
| `lenders.service`                                        | `GET/POST /admin/lenders`, `PATCH/DELETE /{id}`, `POST /{id}/toggle` |
| `config.service`                                         | `PUT /admin/credit-config`, `POST /publish`, `/restore`, `/impact`, `GET /history`, `PUT /admin/settings/flags`, `/plans` |
| `content.service`                                        | `GET/PUT /admin/content/market\|crops\|assistant\|diagnoses`, `PUT /weather-override`, `GET /meta`, `POST /assistant/test` |
| `announcements.service`                                  | `GET/POST /admin/announcements`, `PATCH/DELETE /{id}`, `POST /{id}/end`, `/reach` |
| `risk.service`                                           | `POST /admin/risk/scan`, `GET /admin/risk/flags`, `/{id}/related`, `POST /{id}/dismiss`, `/{id}/confirm`, `GET /admin/risk/rules` |
| `team.service`                                           | `GET/POST /admin/team`, `POST /{id}/role`, `/disable`, `/enable`, `/reset-password`, `GET /admin/roles` |
| `audit.service`                                          | `GET /admin/audit` (filters + paging), `/audit/actors`, `POST /audit/export` |
| `reports.service`                                        | `GET /admin/reports`, `/admin/reports/{id}`, `POST /{id}/export?format=csv\|pdf`, `/builder/preview`, `/builder/export`; schedules: `GET/POST /admin/reports/schedules`, `PATCH/DELETE /schedules/{id}` |
| `backup.service`, system status                          | `POST /admin/system/backup`, `/backup/summary`, `/restore`, `/reset-demo`, `/delete-farmers`, `GET /admin/system/status` |
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
  - admin sign-in 30 per 15 minutes, on top of the per-email lockout
  - assistant 30, crop doctor 10, weather 60 and place search 30 per minute
  `X-Forwarded-For` is believed only from `TRUSTED_PROXIES`.
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
  - **Without a key,** everything works as before, and the crop doctor says its results are a
    demonstration.
- **Scheduled reports** go out every Monday or every 1st of the month at 07:00 Ghana time, with
  the report's CSV attached (PDFs are drawn in the browser, so they can't be emailed).
  - A background thread in each worker checks every minute. It claims due rows with
    `FOR UPDATE SKIP LOCKED`, so several workers send each report once.
  - Without email set up, a due report is recorded as failed and moves on to its next time.
- **Several API workers** share live events through PostgreSQL `LISTEN/NOTIFY`
  (channel `agrictec_events`), so `uvicorn --workers N` or several hosts work without Redis.
