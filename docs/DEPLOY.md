# Deploying to a server

One Linux server, one domain, Docker. Caddy terminates TLS (certificates are obtained and renewed automatically), gunicorn serves Django, a Celery worker runs the retry queue, Postgres and Redis run in containers. **Use Stripe test keys only.**

## What was and was not tested

Tested locally: the Docker image builds, `manage.py check --deploy` runs inside it, and the stack (Postgres, Redis, migrations, gunicorn, Celery worker) starts. Against it: HTTP redirects to HTTPS when behind the proxy header, admin static files are served, a wrong `Host` header is refused with 400, and JWT login and the dashboard work.

**Not tested:** Caddy itself and the automatic certificate (it needs a public domain and ports 80 and 443), and Stripe delivering webhooks to a public URL. Do the verification steps below on first deploy.

## Prerequisites

- A server with Docker and the Compose plugin.
- A DNS `A` (and `AAAA`) record for your domain pointing at the server. Ports 80 and 443 reachable.
- Stripe test keys.

## Steps

1. **Copy the project** to the server (no git remote is needed):
   ```bash
   rsync -av --exclude .venv --exclude .git --exclude '.env' --exclude '.env.*' \
     ./ user@server:~/payment_app/
   ```
2. **Configure.** On the server:
   ```bash
   cd ~/payment_app
   cp .env.prod.example .env.prod && chmod 600 .env.prod
   python3 -c "import secrets; print(secrets.token_urlsafe(50))"   # use as DJANGO_SECRET_KEY
   ```
   Fill in `.env.prod`: `DOMAIN`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` (with `https://`), a database password, and the Stripe keys. Leave `ALLOW_REGISTRATION=0` so that strangers cannot create accounts on a payment page.
3. **Start it:**
   ```bash
   make prod-up        # docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build
   ```
   The first build takes a few minutes. Migrations run automatically (the `migrate` service).
4. **Register the webhook** in the Stripe dashboard (test mode): Developers → Webhooks → Add endpoint.
   - URL: `https://YOUR_DOMAIN/webhooks/stripe/`
   - Events: `payment_intent.succeeded`, `payment_intent.payment_failed`
   - Reveal the endpoint's **signing secret** and put it in `.env.prod` as `STRIPE_WEBHOOK_SECRET`. (This is a different secret from the one `stripe listen` prints locally.)
   - Apply it: `make prod-up` again, which recreates `web` and `worker`.
5. **Create accounts** (registration is closed):
   ```bash
   docker compose --env-file .env.prod -f docker-compose.prod.yml exec web python manage.py seed_demo
   ```
   It prints a generated password once for the buyer `demo` and the staff user `staff`. Send the password to the reviewer separately from the link.
6. **Verify:**
   - `curl -I https://YOUR_DOMAIN/login/` returns 200, and `http://YOUR_DOMAIN/login/` redirects to HTTPS.
   - Log in, pay with `4242 4242 4242 4242`, and watch the order reach `SUCCESS`.
   - In Stripe, the webhook endpoint's delivery log shows `200` responses.
   - `docker compose ... logs worker` shows `ready`.

## Operating it

```bash
DC="docker compose --env-file .env.prod -f docker-compose.prod.yml"
$DC logs -f web worker          # logs
$DC exec web python manage.py poll_pending     # recover lost webhooks / re-queue stuck orders
$DC exec web python manage.py reconcile        # compare with Stripe
$DC exec -T db pg_dump -U payments payments > backup.sql   # backup
```

Run `poll_pending` (every 5 minutes) and `reconcile` (nightly) from the host's crontab with `exec -T`.

To update: `rsync` again (step 1), then `make prod-up`. To stop: `make prod-down`. To remove all data: `$DC down -v`.

## Security notes

- Test keys only. Anyone who can reach the checkout can create test payments in your Stripe test account, which is harmless but noisy. Keep the URL private or add basic auth in `deploy/Caddyfile`.
- HSTS is on for one hour, without subdomains or preload, so a mistake is easy to undo. Raise it once you are sure.
- `/admin/` is reachable by staff users. Restrict it by IP in `deploy/Caddyfile` if the server stays up after the demo.
- There is no rate limiting. Put one in front (Caddy rate-limit plugin or a firewall rule) for anything beyond a short-lived demo.
- Secrets exist only in `.env.prod` (mode 600) on the server. They are never in the image or the repository.
- Single host, no high availability, backups are manual.
