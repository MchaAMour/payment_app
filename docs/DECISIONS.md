# Design decisions

What I chose, why, what I left out on purpose, and how I verified it. For how the pieces fit together, see [`ARCHITECTURE.md`](ARCHITECTURE.md).

**Core design:** a PSP-hosted payment form, payment event → payment orders, a double-entry ledger, a wallet, reconciliation in three classes, a retry queue plus a dead letter queue, exactly-once as at-least-once plus idempotency, and a security checklist. Deliberate trade-offs are noted below.

---

## Scope

| Area | Choice | Why |
|---|---|---|
| Money flows | **Pay-in only** | Pay-out needs Stripe Connect and connected accounts; it would double the surface without showing a new idea. |
| Checkout shape | **One store, one payment order per checkout** | The payment event → orders model stays in the schema, so several sellers per checkout is a data change, not a redesign. |
| PSP | **Stripe, test mode** | Real idempotency keys, real signed webhooks, real failures to recover from. |
| Database | **PostgreSQL** | ACID transactions and row locks are what correctness depends on. At ~10 TPS, throughput is not the problem. |
| Auth | **JWT** (`djangorestframework` + `simplejwt`) | Stateless API auth that a separate front end or mobile client could use unchanged. |
| Failure handling | **Celery retry queue + dead letter table** | Applied to the one call that fails transiently. |
| Goal | A convincing demo, not a production system | Several things below are consciously left out (section "Deliberately left out"). |

## Payments core

- **Amounts are integer minor units (cents), not strings.** Some designs send amounts as strings so floats never round them. Stripe uses integer minor units, so an integer is lossless and avoids parsing. The API field is therefore `amount_minor`.
- **PaymentIntent plus Stripe's Payment Element, not Checkout Session.** It follows the usual flow (register the payment, get a token, show a PSP-hosted form) while the checkout page stays ours. Card data goes from the browser to Stripe and never touches this server.
- **The webhook is the source of truth; the browser is not trusted.** After paying, the page polls our API, and only a signed webhook (or the recovery jobs, which ask Stripe directly) moves an order to `SUCCESS`.
- **Wallet and ledger updates happen in one database transaction** with the status change. A larger system would update them as separate service calls; here they share a database, so one transaction is simpler and safer. I kept the `wallet_updated` and `ledger_updated` flags so the design still works if they become separate services.
- **Idempotency is layered:**
  - `checkout_id` is the primary key of the payment event, so a repeated request returns the existing order;
  - `payment_order_id` is sent to Stripe as the `Idempotency-Key`, so even a repeated call to Stripe cannot create a second PaymentIntent;
  - webhook events are deduplicated by event id **in the same transaction** as their handling, so a crash rolls both back and Stripe's retry reprocesses;
  - `mark_success` takes a row lock and is safe to run twice; a late failure never overwrites a success.
- **A concurrent duplicate request returns the existing state**, rather than returning `429`. That is friendlier to a double-clicking user and just as safe: the unique constraint, the idempotency key and the row lock make the duplicate harmless.
- **The same `checkout_id` with a different payload is refused with `409`**, as Stripe does with idempotency keys, rather than silently returning the old order.
- **The buyer always comes from the JWT**, never from the request body. A buyer cannot read another buyer's orders or reuse their `checkout_id`; both return 404, so the other order's existence is not revealed.
- **A failed card can be retried to success** on the same order (Stripe lets the same PaymentIntent be confirmed again), so `FAILED` is not terminal unless the order is dead-lettered.

## Failure handling

- **Celery covers only the call that registers the payment with Stripe.** That is the call that fails transiently. Webhook handling stays synchronous: it is already idempotent and Stripe retries failed deliveries, so a queue there would add moving parts without adding safety.
- **Errors are classified explicitly.** Connection errors, Stripe 5xx, rate limits and idempotency conflicts are retryable. Everything else (invalid request, bad key, permissions) is permanent and goes straight to the dead letter table, because retrying cannot help.
- **Backoff is exponential with jitter** (2s, 4s, 8s, … capped at 5 minutes), and every retry reuses the same idempotency key. The limit (`PAYMENT_MAX_RETRIES`, default 5) is read from settings at run time.
- **The dead letter queue is a database table**, not a second queue, so an operator can query, filter and mark items resolved in the admin.
- **Tasks are acknowledged late** (`acks_late`, reject on worker loss), so a killed worker's task is redelivered.
- **If Redis is down**, the order stays `NOT_STARTED`, the request still succeeds, and the buyer's next request retries synchronously. `poll_pending` also re-queues any `NOT_STARTED` order older than a threshold, closing the gap.
- **Recovery jobs are management commands** meant for cron or Celery beat: `poll_pending` for lost webhooks, `reconcile` nightly. The demo does not schedule them.

## Reconciliation

- **It compares our database with the Stripe API**, not with a settlement file. Stripe has no nightly bank-style file, and the API gives the same facts. Mismatches go into three classes: *auto-fixed*, *manual (finance)* and *unclassified (investigate)*. It also checks that the ledger's debits equal its credits.
- **Limits:** it ignores refunds, disputes, fees and net amounts.

## Ledger

- **Append-only is enforced twice:** in Python (`LedgerEntry` refuses update and delete) and by a **Postgres trigger** (migration 0003) that rejects `UPDATE` and `DELETE`, including raw SQL and queryset updates. A row trigger does not block `TRUNCATE`, so the database owner could still wipe the table; true immutability would need role permissions or a separate store.

## Auth and security

- **Access tokens last 30 minutes, refresh tokens 24 hours.** Tokens are kept in `sessionStorage`, which is fine for a demo; production would use httpOnly cookies and rotate or blacklist refresh tokens.
- **Registration is switchable** (`ALLOW_REGISTRATION`: on locally, off in the production example). Open sign-up on a public payment page is a bad default, so a public server uses accounts created by `seed_demo`.
- **The webhook is authenticated by Stripe's signature, not a JWT.** CSRF does not apply to the bearer-token API.
- **Operations data is staff-only:** reconciliation mismatches and dead letters appear only for staff users.
- **The admin is read-only**, because money records are written by the payment flow only. The one exception is the `resolved` flag on a dead letter.
- **Secrets stay out of the repository and the image.** `.env` files are git-ignored and docker-ignored; only `*.example` templates are tracked.

## Store and front end

- **The catalog and prices live on the server** (`payments/catalog.py`). The browser sends only `{id, qty}`; the server validates ids, quantities (1 to 10) and duplicates, and computes the total. A forged price in the request is ignored.
- **The cart lives in the browser's localStorage.** It is only ids and quantities, nothing sensitive, and a guest can fill it before logging in. The order is created only at checkout.
- **Each distinct cart gets its own `checkout_id`**, so a page refresh is idempotent but a changed cart is a new checkout.
- **Orders carry a description** (for example "Babolat Pure Drive Racket ×2"), also sent to Stripe, so both the dashboard and Stripe show what was bought.
- **Dark theme by default, with a light toggle**, applied before first paint; Stripe's card form follows the theme.
- **Product art is inline SVG** drawn in code, so there are no image files or licensing questions. Brand names are product labels only.

## Code and tooling

- **Settings are environment-driven.** Local development works with defaults; with `DJANGO_DEBUG=0` the app refuses to start without a secret key and allowed hosts. It trusts `X-Forwarded-Proto` only when `DJANGO_BEHIND_PROXY=1`.
- **Tests are a package**, one file per concern (flow, auth, retry queue, ledger, concurrency, commands, admin, cart, pages). Stripe's API is mocked, but webhook tests sign payloads with Stripe's real HMAC scheme and go through the real `construct_event`, so signature verification itself is tested.
- **Concurrency is tested with real threads against Postgres**, not database mocks. To check the test itself: removing the row lock from `mark_success` makes eight simultaneous success signals credit a wallet eight times (20000 instead of 2500), so the test does catch a missing lock.
- **Two ways to run locally:** `make dev` (venv + Docker for Postgres/Redis, one terminal for everything) or `make docker-up` (everything in containers, including the Stripe CLI listener).
- **Compose projects have explicit names**, so containers and volumes cannot collide with other stacks on the same machine.
- **Deployment is Docker Compose plus Caddy** on one server: the smallest setup with real HTTPS and a stable webhook URL. Kubernetes would add more to explain than to demonstrate.
- **HSTS is one hour, without subdomains or preload**, so a mistake on a demo domain is easy to undo. Django's `check --deploy` reports this as informational warnings; that is intentional.
- **CI** (GitHub Actions) runs the test suite on Postgres and Redis, pyflakes, a missing-migrations check, and a Docker build with `check --deploy`.

## Deliberately left out

| Left out | Why | Where it would go |
|---|---|---|
| Pay-out to sellers | Pay-in only by design | Stripe Connect transfers, a payout ledger, the same status-flag pattern |
| Several sellers per checkout | One store by design | The model already supports several orders per event; a marketplace would use Connect transfers |
| Refunds and chargebacks | Need reversal ledger entries | New order states, reversing entries, `charge.refunded` and dispute webhooks |
| 3D Secure in the UI | Checkout uses `redirect: "if_required"`; the challenge flow is not built | Handle `requires_action` and a return URL |
| Webhook processing on Celery | Already idempotent; Stripe retries | Only if processing becomes slow |
| Scheduled jobs | Need beat or cron | Celery beat or cron running `poll_pending` and `reconcile` |
| Rate limiting, token blacklist | Beyond a demo | Rate limits at the proxy; `simplejwt` blacklist app |
| Metrics, tracing, alerts | Beyond the core payment flow | Prometheus, structured logs, an alert on the dead letter count |
| Currency conversion, regional methods | Beyond the core payment flow | Per-currency minor-unit handling, per-region PSP adapters |
| High availability, backups | Single demo server | Managed Postgres with point-in-time recovery |

## Bugs worth mentioning

- **stripe-python v16 objects are not dicts.** `event["data"]["object"].get(...)` raised `AttributeError` in the webhook. The tests caught it; fixed with `to_dict()`.
- **CSRF blocked the JSON API** at first. It went away for good when the API moved to bearer tokens, where CSRF does not apply.
- **Stopping the dev script left the Stripe listener running**, because the Stripe CLI is a wrapper that spawns a child process. `scripts/dev.sh` now runs each service in its own process group and stops whole groups.

## Verification

**Automated: 69 tests, all passing**: duplicate and conflicting requests, webhook replay, out-of-order events, bad signatures, JWT behaviour and buyer isolation, closed registration, the retry queue (transient vs permanent errors, backoff, exhaustion, broker down), the append-only ledger (Python guard and database trigger), reconciliation buckets, `poll_pending`, `seed_demo`, the read-only admin, and three concurrency scenarios on real Postgres.

**Against real Stripe, test mode:**

- A payment end to end: PaymentIntent created and confirmed, signed webhook delivered by the Stripe CLI, order `SUCCESS`, wallet and ledger correct and balanced (also through the all-Docker stack).
- The same request sent again: same order, exactly one PaymentIntent at Stripe.
- A webhook replayed with `stripe events resend`: nothing changed.
- A declined card, then a good card on the same order: `FAILED`, then `SUCCESS`.
- `reconcile` (0 mismatches) and `poll_pending`.
- The full checkout in a real browser with the test card `4242 4242 4242 4242`, in both themes.

**Retry queue on real Redis and a real Celery worker**, with a simulated Stripe outage (`STRIPE_API_BASE` pointing at a dead port): the API returned 200 with the order `NOT_STARTED`; the worker retried with growing delays; the order became `FAILED` with a `RETRIES_EXHAUSTED` dead letter.

**Production image, locally, without Caddy:** build, `check --deploy`, migrations, gunicorn, worker, HTTPS redirect behind the proxy header, admin static files, host-header rejection, JWT login and dashboard.

**Not verified:** 3D Secure; Caddy's automatic certificate and Stripe calling a public webhook URL; behaviour under load; phone-sized screens by eye.
