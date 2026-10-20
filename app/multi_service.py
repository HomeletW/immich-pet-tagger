"""Single-server, multi-owner pet tagging with shared enrollment and inference.

The administrator UI is trusted to access every configured account. Immich users
retain their own ownership and permissions; this service never publishes albums.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time
from uuid import uuid4

import accounts
import classifier
import embedder as emb
import immich
import inference
from multi_store import Store
from poller import classify_outcome, THRESHOLD, THRESHOLD_FALLBACK

log = logging.getLogger("multi-account")


class BusyError(RuntimeError):
    pass


class Service:
    def __init__(self, configured, root, batch_size=64, workers=8):
        self.accounts = configured
        self.root = Path(root)
        self.store = Store(root)
        self.batch_size = max(1, batch_size)
        self.workers = max(1, workers)
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.thread_lock = threading.Lock()
        self.scan_thread = None
        self.status = {"running": False, "error": None, "accounts": {}}

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise BusyError(
                "A scan or enrollment change is active. Stop the scan before editing enrollment."
            )
        try:
            yield
        finally:
            self.lock.release()

    def account(self, account_id):
        if account_id not in self.accounts:
            raise ValueError("Unknown account")
        account = self.accounts[account_id]
        owner = account.verify()
        self.store.bind(account.id, owner)
        return account

    def public_state(self):
        enrollment = self.store.enrollment()
        status = self.status
        snapshot = {**status, "accounts": status["accounts"].copy()}
        return {
            "accounts": list(self.accounts),
            **enrollment,
            "scan": snapshot,
            "progress": self.store.summary(),
            "review": self.store.review(),
        }

    def create_pet(self, name, description="", since=None, until=None):
        with self.exclusive():
            doc = self.store.enrollment()
            if any(
                p["name"].casefold() == name.casefold() for p in doc["pets"].values()
            ):
                raise ValueError("A pet with this name already exists")
            pet_id = str(uuid4())
            doc["pets"][pet_id] = {
                "name": name,
                "description": description,
                "since": since,
                "until": until,
                "persons": {},
                "refs": [],
            }
            self.store.save(doc)
            return pet_id

    def update_pet(self, pet_id, fields):
        with self.exclusive():
            doc = self.store.enrollment()
            pet = doc["pets"][pet_id]
            if "name" in fields and any(
                k != pet_id and p["name"].casefold() == fields["name"].casefold()
                for k, p in doc["pets"].items()
            ):
                raise ValueError("A pet with this name already exists")
            pet.update(
                {
                    k: v
                    for k, v in fields.items()
                    if k in ("name", "description", "since", "until")
                }
            )
            self.store.save(doc)
            # Remote names are reconciled before scanning; failed accounts remain retryable.

    def add_reference(self, account_id, asset_id, pet_id=None, bbox=None):
        with self.exclusive():
            account = self.account(account_id)
            account.require_asset(asset_id)
            doc = self.store.enrollment()
            refs = doc["pets"][pet_id]["refs"] if pet_id else doc["negatives"]
            ref = {"account": account_id, "asset_id": asset_id, "bbox": bbox}
            if ref not in refs:
                refs.append(ref)
            self.store.save(doc)

    def remove_reference(self, account_id, asset_id, pet_id=None):
        with self.exclusive():
            doc = self.store.enrollment()
            refs = doc["pets"][pet_id]["refs"] if pet_id else doc["negatives"]
            refs[:] = [
                r
                for r in refs
                if (r["account"], r["asset_id"]) != (account_id, asset_id)
            ]
            self.store.save(doc)

    def _remove_person(self, account, person):
        # A previous attempt may have deleted the person before a network interruption.
        response = account.request("DELETE", f"people/{person}", allow_not_found=True)
        return response

    def delete_pet(self, pet_id, *, local_only=False):
        with self.exclusive():
            doc = self.store.enrollment()
            pet = doc["pets"][pet_id]
            if not local_only:
                for account_id, person in list(pet["persons"].items()):
                    self._remove_person(self.account(account_id), person)
                    del pet["persons"][account_id]
                    self.store.save(doc)  # partial success is restart-safe
            del doc["pets"][pet_id]
            self.store.save(doc)

    def reset_pet(self, pet_id, account_id):
        with self.exclusive():
            doc = self.store.enrollment()
            pet = doc["pets"][pet_id]
            person = pet["persons"].get(account_id)
            if person:
                self._remove_person(self.account(account_id), person)
                del pet["persons"][account_id]
                self.store.save(doc)
            # References are independent of the deleted person's ID. Rescan explicitly.

    def ensure_persons(self, account, doc):
        for pet in doc["pets"].values():
            if not pet["refs"]:
                continue
            person = pet["persons"].get(account.id)
            if person:
                response = account.request(
                    "GET", f"people/{person}", allow_not_found=True
                )
                if response.status_code == 404:
                    person = None
                elif response.json().get("name") != pet["name"]:
                    account.request(
                        "PUT", f"people/{person}", json={"name": pet["name"]}
                    )
            if not person:
                person = account.request(
                    "POST", "people", json={"name": pet["name"]}
                ).json()["id"]
                pet["persons"][account.id] = person
                self.store.save(doc)

    @contextmanager
    def reference_context(self, ref):
        account = self.account(ref["account"])
        # Check even cached references: inaccessible originals must not silently train
        # a partial/different classifier, or be read through another owner's key.
        account.require_asset(ref["asset_id"])
        with accounts.using(account):
            yield

    def train(self, doc):
        pets = {key: pet for key, pet in doc["pets"].items() if pet["refs"]}
        if not pets:
            return None
        return classifier.build_classifier(
            list(pets),
            {k: p["refs"] for k, p in pets.items()},
            doc["negatives"],
            reference_context=self.reference_context,
            strict=True,
        )

    def discover(self, account, since=None, until=None):
        with accounts.using(account):
            cursor = self.store.cursor(account.id)
            started_at = datetime.now(timezone.utc).isoformat()
            query = (
                immich.search_params(taken_after=since, taken_before=until)
                if since is not None
                else immich.search_params(created_after=cursor)
            )
            watermark = cursor
            # Persist each page, but advance the discovery watermark only after all
            # pages succeed. A failed/cancelled discovery can safely repeat pages.
            for page in immich._search_metadata_pages(
                query, 1000, "multi-account discovery"
            ):
                if self.cancel.is_set():
                    return
                assets = []
                for asset in page:
                    if asset.get("ownerId") != account.owner_id or not asset.get("id"):
                        continue
                    created = asset.get("createdAt")
                    taken = (
                        asset.get("fileCreatedAt")
                        or asset.get("localDateTime")
                        or created
                    )
                    if not taken or (since is None and not created):
                        continue
                    assets.append((asset["id"], taken))
                    if created:
                        watermark = max(watermark, created)
                self.store.enqueue(account.id, assets, rescan=since is not None)
            if since is None and not self.cancel.is_set():
                # Inclusive boundary + durable dedup handle equal timestamps. Do not
                # advance past the start of discovery while new uploads arrive.
                watermark = min(watermark, started_at)
                self.store.enqueue(account.id, [], cursor=watermark)

    def process(self, account, job, doc, trained):
        if self.cancel.is_set():
            return
        try:
            with accounts.using(account):
                account.require_asset(job["asset"])
                img = emb.fetch_thumbnail(job["asset"])
                if img is None:
                    raise RuntimeError("Thumbnail unavailable")
                before = inference.model_error_count()
                crops = emb.crop_animals(img) or [(None, img)]
                faces = account.request(
                    "GET", "faces", params={"id": job["asset"]}
                ).json()
                existing = {(f.get("person") or {}).get("id") for f in faces}
                outcomes = []
                for bbox, crop in crops:
                    if self.cancel.is_set():
                        return  # leave pending; an interrupted write is reconciled on retry
                    vec = emb.embed_image(crop)
                    if vec is None or inference.model_error_count() != before:
                        raise RuntimeError("Model inference failed")
                    pet_id, probability = classifier.classify(vec, *trained)
                    pet = doc["pets"].get(pet_id, {})
                    outcome = classify_outcome(
                        pet_id,
                        probability,
                        job["taken"],
                        pet,
                        threshold=THRESHOLD if bbox is not None else THRESHOLD_FALLBACK,
                    )
                    if outcome == "confident":
                        person = pet["persons"][account.id]
                        if person not in existing:
                            face = immich.post_face_sync(
                                job["asset"], person, bbox, img.size
                            )
                            if not face:
                                raise RuntimeError("Could not confirm face assignment")
                            existing.add(person)
                        # Retry review-tag failures too, including after a successful face.
                        if immich.REVIEW_TAG:
                            tag = account.review_tag(immich.REVIEW_TAG)
                            response = account.request(
                                "PUT",
                                f"tags/{tag}/assets",
                                json={"ids": [job["asset"]]},
                            ).json()
                            if any(
                                not item.get("success")
                                and item.get("error") != "duplicate"
                                for item in response
                            ):
                                raise RuntimeError("Could not apply review tag")
                    outcomes.append(
                        {"pet": pet_id, "probability": probability, "outcome": outcome}
                    )
                self.store.complete(account.id, job["asset"], outcomes)
        except Exception as exc:
            # Errors are bounded and credentials are never stored in this queue.
            error = (
                str(exc)
                if isinstance(exc, (ValueError, RuntimeError))
                else type(exc).__name__
            )
            self.store.failed(account.id, job["asset"], error)
            log.warning(
                "Account %s: asset processing failed (%s)",
                account.id,
                type(exc).__name__,
            )

    def start_scan(self, selected=None, since=None, until=None):
        with self.thread_lock:
            if self.scan_thread and self.scan_thread.is_alive():
                raise BusyError("A scan is already running")
            if self.lock.locked():
                raise BusyError("An enrollment operation is active")
            self.cancel.clear()

            def run():
                try:
                    self.scan(selected, since, until)
                except BusyError:
                    self.status["error"] = (
                        "Enrollment changed while starting; retry the scan"
                    )

            self.scan_thread = threading.Thread(
                target=run, name="multi-account-scan", daemon=True
            )
            self.scan_thread.start()

    def shutdown(self):
        self.cancel.set()
        if self.scan_thread:
            self.scan_thread.join()
        self.store.close()

    def scan(self, selected=None, since=None, until=None):
        with self.exclusive():
            cycle_started = time.time()
            self.status = {"running": True, "error": None, "accounts": {}}
            try:
                doc = self.store.enrollment()
                if not any(p["refs"] for p in doc["pets"].values()):
                    return
                enabled = []
                for account_id in selected or list(self.accounts):
                    if self.cancel.is_set():
                        break
                    try:
                        account = self.account(account_id)
                        self.ensure_persons(account, doc)
                        self.discover(account, since, until)
                        enabled.append(account)
                        self.store.account_error(account_id, None)
                    except Exception as exc:
                        error = (
                            str(exc)
                            if isinstance(exc, (ValueError, RuntimeError))
                            else type(exc).__name__
                        )
                        self.status["accounts"][account_id] = error
                        self.store.account_error(account_id, error)
                if not enabled or self.cancel.is_set():
                    return
                with inference.inference_session():
                    errors_before = inference.model_error_count()
                    trained = self.train(doc)
                    if inference.model_error_count() != errors_before:
                        raise RuntimeError(
                            "Model failure while preparing shared references"
                        )
                    if trained is None:
                        raise RuntimeError("No usable pet references")
                    while not self.cancel.is_set():
                        processed = False
                        for account in enabled:
                            if self.cancel.is_set():
                                break
                            jobs = self.store.batch(
                                account.id, self.batch_size, ready_at=cycle_started
                            )
                            if not jobs:
                                continue
                            processed = True
                            self.status["current_account"] = account.id
                            # Explicitly bind the account inside every task: ContextVars
                            # do not automatically propagate into ThreadPoolExecutor.
                            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                                list(
                                    pool.map(
                                        lambda job: self.process(
                                            account, job, doc, trained
                                        ),
                                        jobs,
                                    )
                                )
                        if not processed:
                            break
                    emb.save_embed_cache()
            except Exception as exc:
                self.status["error"] = (
                    str(exc)
                    if isinstance(exc, (ValueError, RuntimeError))
                    else type(exc).__name__
                )
                log.exception("Multi-account scan failed")
            finally:
                self.status["running"] = False
                self.status["finished_at"] = datetime.now(timezone.utc).isoformat()
                self.status["cancelled"] = self.cancel.is_set()
