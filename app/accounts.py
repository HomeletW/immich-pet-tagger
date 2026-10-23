"""Explicit account credentials for multi-account mode; never mutate global keys."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import threading

import requests


@dataclass
class Account:
    id: str
    api_key: str = field(repr=False)
    owner_id: str | None = None
    review_tag_id: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def request(self, method, path, **kwargs):
        import immich

        allow_not_found = kwargs.pop("allow_not_found", False)
        response = requests.request(
            method,
            f"{immich.IMMICH_URL}/api/{path.lstrip('/')}",
            headers={"x-api-key": self.api_key, "Accept": "application/json"},
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )
        # Do not include response bodies or credentials in exceptions/status output.
        if response.status_code == 404 and allow_not_found:
            return response
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Account {self.id}: Immich HTTP {response.status_code}")
        return response

    def verify(self):
        with self.lock:
            owner = self.request("GET", "users/me").json().get("id")
            if not owner:
                raise RuntimeError(f"Account {self.id}: could not establish owner")
            if self.owner_id and owner != self.owner_id:
                raise RuntimeError(f"Account {self.id}: API key owner changed")
            self.owner_id = owner
        return owner

    def require_asset(self, asset_id):
        owner = self.owner_id or self.verify()
        asset = self.request("GET", f"assets/{asset_id}").json()
        if asset.get("ownerId") != owner:
            raise ValueError(f"Asset is not owned by account {self.id}")
        return asset

    def review_tag(self, name):
        with self.lock:
            if self.review_tag_id is None:
                payload = self.request("PUT", "tags", json={"tags": [name]}).json()
                for tag in payload:
                    if tag.get("value") == name or tag.get("name") == name:
                        self.review_tag_id = tag["id"]
                        break
                if self.review_tag_id is None:
                    raise RuntimeError(f"Account {self.id}: review tag not returned")
            return self.review_tag_id


_current: ContextVar[Account | None] = ContextVar("immich_account", default=None)


def current():
    return _current.get()


@contextmanager
def using(account):
    token = _current.set(account)
    try:
        yield account
    finally:
        _current.reset(token)


def load_accounts(path: str | Path) -> dict[str, Account]:
    """A secret JSON file, deliberately separate from the enrollment database."""
    entries = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("Accounts file must contain a nonempty JSON array")
    result = {}
    keys = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each account must be a JSON object")
        account_id = entry.get("id", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", account_id):
            raise ValueError("Account IDs must contain 1–64 letters, digits, _ or -")
        if account_id in result:
            raise ValueError(f"Duplicate account ID: {account_id}")
        key = entry.get("api_key") or os.environ.get(entry.get("api_key_env", ""), "")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"Missing API key for {account_id}")
        if key in keys:
            raise ValueError("Duplicate API keys in accounts file")
        keys.add(key)
        result[account_id] = Account(account_id, key)
    return result
