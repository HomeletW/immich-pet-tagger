# Multi-account pet tagging

This fork adds an opt-in mode for **one Immich server, several owners, one administrator, and shared pet enrollment**. It preserves Immich ownership, partner sharing, timelines and phone synchronization. Optional managed albums share each pet’s matches across the configured accounts.

## What is shared

- One YOLO worker and one CLIP worker, loaded for a scan and released afterward.
- One classifier trained per scan cycle from all enrolled references and negatives.
- A single pet identity, name, date range and reference collection. Each reference records its source account.
- One embedding cache. Asset UUIDs are unique within the single configured Immich server.

Each account has its own API key, verified Immich owner, person ID for each pet, review-tag ID, discovery watermark and durable asset jobs. Models are never multiplied by the number of accounts. `GPU_WORKERS` is forced to `1` in this mode; CPU thumbnail concurrency is controlled separately by `SCAN_WORKERS`.

## Configuration

Set `IMMICH_ACCOUNTS_FILE` to a secret JSON file:

```json
[
  {"id": "user_a", "api_key": "REPLACE_WITH_USER_A_KEY"},
  {"id": "user_b", "api_key": "REPLACE_WITH_USER_B_KEY"},
  {"id": "shared", "api_key": "REPLACE_WITH_SHARED_KEY"}
]
```

The file must be readable by the container, should have restrictive filesystem permissions, and must not be committed. Alternatively, use `api_key_env` as illustrated in `accounts.example.json` and pass those environment variables into the container. The supplied Compose example uses literal keys in the secret file; it does not forward arbitrary host environment variables.

Use one API key from **each library owner's account**, with the permissions in the upstream README, including `user.read`. All accounts must use the same `IMMICH_URL`. Account IDs are stable labels: replacing a key with another owner's key under the same label is rejected, including after restart. Two labels resolving to the same owner are rejected. Key rotation for the same owner works after restarting the service.

For a local NVIDIA CUDA build:

```sh
# Create accounts.json with your three keys first.
chmod 600 accounts.json
export IMMICH_URL=http://YOUR_IMMICH_HOST:2283
docker compose -f docker-compose.multi.yml build
docker compose -f docker-compose.multi.yml up -d
```

This starts a separate tagger only; it does not stop or alter Immich. For CPU use, set the build argument `CUDA` to `false` and remove the GPU reservation. Use one container and one web-server process; multiple replicas/workers sharing a data directory are unsupported.

The example binds port 2287 to localhost. For trusted LAN administration, change the binding to `0.0.0.0:2287:8000` and configure any host/WSL forwarding separately. The administrator UI has no authentication and can access references from **all configured accounts**. It is not an end-user gallery or a privacy boundary between family members.

## Enrollment and scanning

1. Open the tagger UI and add a pet once.
2. Select its enrollment, choose the source account, and paste an Immich photo URL or asset UUID. References can come from any configured owner. Partner-owned assets are rejected even when the selected key can read them.
3. Add clear references and negative examples through “Not my pets / negatives”. For a photo containing several animals, supply the pet's normalized bounding box (`x1,y1,x2,y2`); otherwise automatic animal detection is used.
4. Start a dated scan of a small interval to verify results. The first scan creates a distinct person record under each participating account, all mapped to the one enrolled pet.
5. Start a scan without dates for the backlog and newly discovered photos. Initially the discovery watermark is 1970, so existing external-library assets are included. Polling repeats hourly by default.
6. After changing references, use a **dated rescan** to re-evaluate completed photos. Existing face assignments are preserved; a rescan does not automatically remove earlier false positives. Reset that pet in the relevant account first if a clean retag is needed.

The multi-account UI currently provides manual reference enrollment, shared negatives, pet editing, per-account reset, global/local deletion, scan/stop, queue counts, and recent low-confidence matches. A live progress bar shows the current run's attempted work, completed assets, retries, elapsed time, phase, and active account. Discovery is indeterminate until the queue is known; the per-account table shows persistent queue totals across runs. Progress refreshes every five seconds. The upstream smart suggestions, import-from-person and accuracy benchmark screens are not yet ported to multi-account mode. Single-account mode retains those original screens and behavior.

Saving an uncertain match as a pet reference or "Not my pets" removes it from the review list, including after a refresh. Negative references are visible under the corresponding Enrollment selection. Removing that reference makes the old uncertain result eligible for review again. Feedback affects subsequent training and does not delete existing Immich face assignments; use a dated rescan to re-evaluate photos. Enrollment controls are disabled during scans: stop the scan and wait for it to finish before reviewing or editing references.

Names and date ranges are shared. Renames reconcile to each account's person record on its next successful scan. Reset removes only the selected account's person record; references and other accounts' mappings survive. Remote deletion removes linked person records before deleting enrollment, recording partial progress so failed operations can be retried. Review tags already applied to assets are retained by reset/deletion; they record past tagging activity.

## Reliability and account boundaries

- Scan assets must match the API key owner's ID. Owner verification is mandatory in multi-account mode; failure skips that account instead of expanding access.
- The scheduler rotates accounts after `ACCOUNT_BATCH_SIZE` assets (default 64). Discovery streams metadata pages into SQLite before processing; a large initial library can delay the first round, but metadata memory is bounded, discovery can stop between pages, and GPU processing is fair between accounts.
- SQLite persists each discovery page transactionally and advances the watermark only after every page succeeds. Queries retain an inclusive timestamp boundary; the `(account, asset)` key prevents duplicate work or loss of equal-timestamp assets.
- Failed thumbnails, face writes and inference remain queued with backoff. A job is attempted at most once per cycle. A later scheduled/manual scan retries it; failures do not block healthy accounts.
- Missing or inaccessible training references stop training rather than silently producing a different partial classifier. Thus an unavailable account **that supplies required references** can block recognition for all accounts until it recovers or those references are removed. An unavailable account without required references does not block the others.
- Stopping leaves uncompleted jobs queued. Face reads before writes make retry after interruption idempotent. Changes to enrollment are rejected while scanning; stop first.
- Low-confidence matches are shown in the UI (latest 40). Account state and errors are visible, with no API keys returned to the browser.

## Storage and existing installations

Shared enrollment and jobs live in `/data/multi/multi-account.sqlite3`. Model/embedding caches remain under `/data`. This is separate from upstream `config.json`, `pets/` and timestamp files. Nothing is automatically converted or deleted when enabling multi-account mode. For an existing enrolled installation, retain its volume and plan an explicit data migration; the current mode starts with fresh enrollment.

Removing an account from configuration stops its scanning but preserves its records. References from a removed account must be removed or the account restored before training can succeed. Reusing this volume with a different Immich server/database is unsupported; use a fresh volume.

Unset `IMMICH_ACCOUNTS_FILE` to run the unmodified single-account UI/poller using `IMMICH_API_KEY`. Do not run legacy taggers alongside the new service against the same pets while validating it.

## Validation

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
node --check app/static/multi.js
```

Tests exercise ownership checks with a simulated Immich HTTP API, shared training, round-robin scheduling, restart persistence, retries, reference routing, rename/reset/deletion and API validation. ML packages are stubbed in unit tests. Real CUDA inference and actual Immich API compatibility still require a controlled deployment test with a small photo set before a full backfill.

## Managed shared albums (opt-in)

Set `IMMICH_ALBUM_OWNER_ACCOUNT=shared` to a configured account ID. That account owns one album per enrolled pet. All other configured owners are invited as editors so each API key can contribute its own assets. This enables sharing of matched assets across the configured accounts, including existing matches and false positives. No public link is created. Leaving the variable unset preserves tagging without album publication.

Required extra key permissions:

- Each contributor: `albumAsset.create` (and `album.read` for operational verification).
- Album owner, additionally: `album.create`, `album.update`, `album.delete`, `albumAsset.delete`, `albumUser.create`, `albumUser.update`.

Creation and renaming reconcile albums immediately. Both pet deletion actions delete the managed album; enrollment-only deletion preserves person/face records, while full deletion also removes those person records. Neither action deletes source assets. Failed deletion retains an intent record so hourly retries finish cleanup without recreating the album. Unrelated albums, even ones with identical names, are not adopted or deleted. Do not change the managed description marker or album ownership; failures are shown in the UI rather than modifying an unverified album.

Hourly scans synchronize albums before and after tagging. **Sync shared albums** performs an album-only backfill/reconciliation without running ML (`POST /api/multi/albums/sync`). Existing person assignments are the source of membership; all search pages must succeed before removing stale contributions. Partners' assets are excluded from each contributing key by explicit ownership checks. Hidden and trashed assets and shared negative references are excluded. A persistent contribution journal allows removal of formerly managed matches while preserving unrelated manual additions. Removing a face assignment in Immich or saving a negative reference removes its managed album contribution on the next sync. Manually removing an otherwise matching asset from the album alone is not permanent: a subsequent sync restores it.

Negative references suppress sharing for the whole asset across all pet albums, even if it contains multiple animals. They do not themselves remove existing face assignments. Album sync does not retune classification. API errors remain visible per pet and are retried hourly. Additional album viewers or public links block new synchronization until removed; changing the configured owner requires an explicit migration.
