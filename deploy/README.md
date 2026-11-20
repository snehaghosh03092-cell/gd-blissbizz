# blissbiz.biz platform release: build checkpoint

This directory replaces the old monolith deployment **in a separate checkout/project**. It does
not update or remove `blissbiz-vps` containers, databases, credentials or uploads. Do not copy it
over `~/blissbiz/deploy`, and do not reuse the old project's Compose wrapper.

The operator confirmed the old backend at migration 0004 and a checksummed backup at
`/home/samanwaydey/blissbiz-backups/20261120T110514Z`. A restore drill is still pending. Its
0004 lineage differs from this platform. Use a new database; do not stamp the old one.

## Copy the prepared source from the Mac

```sh
cd /Users/samanwaydey/Personal/gddy_hackathon/blissbizz
scp var/releases/blissbiz-next-20261120-platform-01.tar.gz \
    var/releases/blissbiz-next-20261120-platform-01.tar.gz.sha256 \
    samanwaydey@97.74.89.213:~/
```

The archive includes current source changes, a per-file SHA256 manifest and the deployment
files. It excludes local environment files, keys, certificates, database contents, local runtimes,
node_modules and build outputs. It does **not** transfer the Claude key or demo dataset.

## Extract into a NEW directory on the VPS

Run each command separately; stop on failure.

```sh
cd ~
sha256sum -c blissbiz-next-20261120-platform-01.tar.gz.sha256
```

Only if `~/blissbiz-next` does not already exist:

```sh
test ! -e "$HOME/blissbiz-next" && tar -xzf blissbiz-next-20261120-platform-01.tar.gz
cd ~/blissbiz-next
```

If the directory exists, inspect it first; do not overwrite another release blindly.

## Generate server-only settings and build

Find the actual existing certificate directory without reading or copying private keys:

```sh
TLS_DIR=$(sudo docker inspect --format '{{range .Mounts}}{{if eq .Destination "/tls"}}{{.Source}}{{end}}{{end}}' blissbiz-vps-proxy-1)
test -n "$TLS_DIR" && echo 'Existing certificate directory found'
sudo python3 scripts/release-init.py --tls-dir "$TLS_DIR"
sudo sh scripts/release-compose.sh config --quiet
sudo sh scripts/release-compose.sh build migrate proxy
```

Initialization creates a new mode-0600 `deploy/release.env`. It refuses to overwrite an existing
file, uses fresh independent DB/signing secrets, checks that the certificate files exist and
are non-empty, and defaults to loopback ports 18080/18443. Existence does not validate certificate
expiry, chain trust or wildcard coverage; inspect those before any cutover. Do not paste the
environment file or full `docker compose config` output in chat.

`migrate` builds one shared Python image containing all eight service packages plus the shared
library and Guide knowledge file. `proxy` builds Vue with `VITE_BASE_DOMAIN=blissbiz.biz` and
packages it with Nginx. Building starts no database/service/proxy and takes no public port.
Internal APIs and MySQL never publish host ports. Only the agent receives `ANTHROPIC_API_KEY`; set it
privately in the server's environment file before its startup if desired. No real payment or
notification provider is introduced by this release.

**Stop at the successful build checkpoint.** Report build errors or these image listings:

```sh
sudo docker image ls --format '{{.Repository}}:{{.Tag}}' | grep '20261120-platform-01'
```

The next operator checkpoint creates only the `blissbiz-next` DB and runs its migrations through
0016. Its Docker-managed `blissbiz-next_database` / `blissbiz-next_uploads` volumes are independent
of the legacy project. This demo MySQL instance disables binary logging (no point-in-time recovery)
so its schema owner can create ledger triggers without remote-root/SUPER privileges. Logical
backups and a restore drill are required; revisit this setting for real-customer production.

Data preparation/import, hostname remapping, replacement of all public seed passwords, live
HTTPS/cookie/tenant tests and the final 80/443 traffic switch are separate reviewed checkpoints.
The development reset command must never target the legacy database. Keep the expiry worker off
until imported demo orders have been reviewed; `SWEEP_INTERVAL_SECONDS=0` is deliberate.

## Local validation

- `./.tools/node/bin/node --test frontend/tests/hosts.test.mjs`
- `backend/.venv/bin/python3 -m pytest scripts/test_release.py -q`
- `backend/.venv/bin/python3 -m pytest services/gateway/tests -q`
- `VITE_BASE_DOMAIN=blissbiz.biz ./scripts/npm.sh --prefix frontend run build`

The `scripts/release-bundle.py` helper builds the private local transfer archive. Rebuild it after
any source edits; never replace a deployed release artifact silently. Fresh schema migration,
full downgrade/upgrade, metadata drift and eight-service health were checked in a disposable local
Compose project. This does not establish server compatibility or constitute public deployment.

Local Nginx runtime verification also passed: verified TLS to blissbiz.biz served Vue HTML and
proxied `/api/ready` to the healthy gateway. This used a short-lived explicitly trusted test
certificate and the host-built production frontend. The standard frontend multi-stage Docker
build was blocked by this Mac's npm certificate-chain trust (`UNABLE_TO_VERIFY_LEAF_SIGNATURE`);
certificate verification was not disabled. The VPS must pass its actual `build migrate proxy`
command before any service/data/traffic transition.

## Migration image permission repair

If the first migration reports PermissionError reading pyproject.toml, copy the corrected
services.Dockerfile from the Mac to the existing release deploy directory and rebuild with
`sudo sh scripts/release-compose.sh build migrate`. The repair normalizes application source
readability inside the image, then checks Alembic heads as the unprivileged runtime user.
It does not change server environment-file permissions. Retry the migration and inspect current;
do not reset the database or use root to bypass the error. This explicit patch leaves the original
platform-01 transfer archive unchanged; its manifest describes the original pre-patch source.

## Standard synthetic demo import (operator selected recreation)

Only after private HTTPS readiness and Vue HTTP 200 pass, copy `scripts/release-demo.py`
separately from the Mac into `~/blissbiz-next/scripts/`. This is an explicit post-bundle addition;
the original transfer archive and manifest are unchanged. No image rebuild is needed for this tool.
The operator chose to regenerate standard sample shops/orders, not copy local modifications.
Run these on the VPS individually; stop on any failure:

```sh
cd ~/blissbiz-next
sudo sh scripts/release-compose.sh stop proxy gateway agent billing catalog identity sales store supply
sudo install -d -m 0700 -o 10001 -g 10001 deploy/demo-private
sudo sh scripts/release-compose.sh run --rm --no-deps \
  -v "$PWD/scripts/release-demo.py:/opt/demo-seed.py:ro" \
  -v "$PWD/deploy/demo-private:/run/demo-private" \
  migrate python3 /opt/demo-seed.py \
  --confirm-empty-demo blissbiz_next \
  --credentials-dir /run/demo-private \
  --anchor "$(date -u +%Y-%m-%dT%H:%M:%S)"
```

The script requires MySQL host db, database blissbiz_next, schema 0016 and user blissbiz.
An advisory lock rejects concurrent importers; every application table must be empty. It never
truncates/resets tables, weakens triggers, disables foreign keys or changes the dev loader guard.
All inserts and 24 integrity checks run in one transaction. On failure, stop and inspect; do not
wipe the DB or blindly retry. A credentials file may exist after a rolled-back import and the
script deliberately refuses to overwrite it. Keep new app services stopped during import.

Each owner/platform account gets a unique random password. One shopper per business gets a
recorded random login; the remaining synthetic shoppers receive an unrecorded random password
hash so the known dev password cannot be used. Credentials live in the private bind directory,
mode 0600 (directory owned by uid 10001, mode 0700), outside uploads, Git, image and source bundle.
Never paste their contents into chat. Preserve this directory as a secret in secure server backups.
Subdomains are mapped to blissbiz.biz; the fake custom domain is omitted. DNS/wildcard certificate
coverage must be checked before public use. This does not provision DNS/certificates.

The import omits sessions, onboarding drafts/tokens, agent/voice/audit/error history, message
queues, webhook history and integration configs. Provider behavior stays fake/console as configured
by the app. Sample purchase document metadata contains seed:// references, not actual uploaded
supplier PDFs; generated sales invoice records/lines remain available for existing exports.
No inference call or government verification occurs during this import. Worker expiry stays off.

After the import reports status imported and 24 checks passed, restart the private app:

```sh
sudo sh scripts/release-compose.sh up -d --wait
```

Then inspect private owner/shopper logins, tenant boundaries and exports before any public switch.
The legacy blissbiz-vps deployment and volumes stay unchanged throughout this stage.

## Authenticated pre-cutover checks

Copy scripts/release-smoke.py from the Mac to the release scripts directory on the VPS.
Run while the new private deployment is up:

```sh
sudo sh scripts/release-compose.sh run --rm --no-deps \
  -v "$PWD/scripts/release-smoke.py:/opt/release-smoke.py:ro" \
  -v "$PWD/deploy/demo-private:/run/demo-private:ro" \
  migrate python3 /opt/release-smoke.py \
  --credentials /run/demo-private/credentials.json
```

The stdlib-only tool connects to proxy:443 with the actual domain as TLS SNI, certificate hostname
and HTTP Host; system certificate verification stays enabled. It checks readiness, owner login,
Secure/HttpOnly/host-only cookies, inventory access, cross-business denial, storefront products,
shopper login/isolation, invoice and inventory XLSX contents and download authorization. It logs
out created authenticated sessions and never prints passwords/tokens or calls inference. It
creates session/rate-limit records through normal APIs but no orders or stock changes. An optional
--ca-file is for explicitly trusted disposable-test CAs only. No verification bypass exists.

## Public port switch and rollback

Only after the authenticated check prints ALL CHECKS PASSED and the operator intends to replace
old-site accounts/catalog with the recreated demo platform, perform this switch. DNS stays the
same. Accounts/orders unique to the legacy DB are not migrated and remain only in its DB. Keep
legacy backend/db running and retain all backups/images/volumes.

Run each block separately and stop on error:

```sh
cd ~/blissbiz-next
sudo cp -p deploy/release.env deploy/demo-private/release.env.preview-backup
sudo sed -i \
  -e 's/^BIND_ADDRESS=.*/BIND_ADDRESS=0.0.0.0/' \
  -e 's/^HTTP_PORT=.*/HTTP_PORT=80/' \
  -e 's/^HTTPS_PORT=.*/HTTPS_PORT=443/' \
  deploy/release.env
sudo sh scripts/release-compose.sh config --quiet
```

```sh
sudo docker stop blissbiz-vps-proxy-1
sudo sh scripts/release-compose.sh up -d --no-deps proxy
```

```sh
curl --fail --show-error --retry 5 --retry-connrefused --retry-delay 2 \
  --resolve blissbiz.biz:443:127.0.0.1 https://blissbiz.biz/api/ready
```

Check the actual public domain from the Mac/browser too (root/admin/threads-co), sign in with
private generated credentials and verify dashboard/catalog. Existing browsers may need a fresh
login because the new deployment has separate sessions. Claude is optional; if a key was supplied,
check a Guide answer separately after the traffic switch without exposing the key.

If the public app fails, immediately restore old traffic:

```sh
sudo sh scripts/release-compose.sh stop proxy
sudo docker start blissbiz-vps-proxy-1
```

After old traffic is restored, restore only the new release's private binding settings:

```sh
sudo cp -p deploy/demo-private/release.env.preview-backup deploy/release.env
```

The new DB remains intact on rollback. New-app orders/data created after switching will not be
visible in the old application; keep both databases and reconcile before switching again. Do not
run down --volumes or change DNS. The earlier legacy backup has verified hashes but no restore
drill yet; perform a restore drill before retiring any legacy resources.
