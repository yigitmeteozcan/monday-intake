"""Monday.com GraphQL client, scoped to what the intake pipeline needs."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .textnorm import fold, normalize_company

LOG = logging.getLogger(__name__)
API_URL = "https://api.monday.com/v2"
FILE_URL = "https://api.monday.com/v2/file"


class MondayError(RuntimeError):
    pass


class MondayClient:
    def __init__(self, token: str, board_id: str, api_version: str = "2024-10"):
        if not token:
            raise MondayError("Monday API token is empty.")
        self.board_id = str(board_id)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": token,
                "API-Version": api_version,
                "Content-Type": "application/json",
            }
        )
        self._columns: list[dict] | None = None
        self._items: list[dict] | None = None
        self._users: list[dict] | None = None
        self._board_url: str | None = None

    # ---------------------------------------------------------------- core
    def gql(self, query: str, variables: dict | None = None, retries: int = 3) -> dict:
        payload = {"query": query, "variables": variables or {}}
        last_error: Exception | None = None

        for attempt in range(retries):
            try:
                response = self.session.post(API_URL, json=payload, timeout=60)
            except requests.RequestException as exc:  # network blip
                last_error = exc
                time.sleep(2 ** attempt)
                continue

            # 429 = rate limited, 5xx = transient. Both are worth a retry.
            if response.status_code in (429, 500, 502, 503, 504):
                wait = int(response.headers.get("Retry-After", 2 ** attempt))
                LOG.warning("Monday %s, retrying in %ss", response.status_code, wait)
                time.sleep(min(wait, 30))
                last_error = MondayError(f"HTTP {response.status_code}")
                continue

            if response.status_code >= 400:
                raise MondayError(f"HTTP {response.status_code}: {response.text[:400]}")

            data = response.json()
            if data.get("errors"):
                messages = "; ".join(
                    e.get("message", str(e)) for e in data["errors"]
                )
                raise MondayError(messages)
            return data.get("data") or {}

        raise MondayError(f"Monday API unreachable: {last_error}")

    # ------------------------------------------------------------- metadata
    def board_meta(self) -> dict:
        data = self.gql(
            """
            query ($board: [ID!]) {
              boards(ids: $board) {
                id name url
                groups { id title }
                columns { id title type settings_str }
              }
            }
            """,
            {"board": [self.board_id]},
        )
        boards = data.get("boards") or []
        if not boards:
            raise MondayError(f"Board {self.board_id} not found, or the token cannot see it.")
        board = boards[0]
        self._columns = board.get("columns") or []
        self._board_url = board.get("url")
        return board

    def columns(self) -> list[dict]:
        if self._columns is None:
            self.board_meta()
        return self._columns or []

    def column_type(self, column_id: str) -> str:
        for column in self.columns():
            if column["id"] == column_id:
                return column.get("type", "text")
        return "text"

    def status_labels(self, column_id: str) -> list[str]:
        """Allowed labels for a status/dropdown column.

        Monday rejects a write with an unknown label, so we check first and
        degrade to a note in the update rather than failing the whole intake.
        """
        for column in self.columns():
            if column["id"] != column_id:
                continue
            try:
                settings = json.loads(column.get("settings_str") or "{}")
            except json.JSONDecodeError:
                return []
            labels = settings.get("labels") or {}
            if isinstance(labels, dict):
                return [str(v) for v in labels.values() if v]
            if isinstance(labels, list):
                return [str(x.get("name", "")) for x in labels if x.get("name")]
        return []

    def match_label(self, column_id: str, value: str) -> str | None:
        """Find the real label matching what the user typed, Turkish-safe."""
        wanted = fold(value)
        for label in self.status_labels(column_id):
            if fold(label) == wanted:
                return label
        for label in self.status_labels(column_id):
            if wanted and wanted in fold(label):
                return label
        return None

    def users(self) -> list[dict]:
        if self._users is None:
            data = self.gql("query { users(limit: 200) { id name email } }")
            self._users = data.get("users") or []
        return self._users

    def find_user(self, needle: str) -> dict | None:
        wanted = fold(needle)
        if not wanted:
            return None
        for user in self.users():
            if fold(user.get("email", "")) == wanted or fold(user.get("name", "")) == wanted:
                return user
        for user in self.users():
            name = fold(user.get("name", ""))
            email = fold(user.get("email", ""))
            if wanted in name or email.startswith(wanted):
                return user
        return None

    # ---------------------------------------------------------------- items
    def all_items(self, refresh: bool = False) -> list[dict]:
        """Every item on the board (id + name), cached for the run.

        Deduplication happens locally rather than through the API because
        matching "ACME?" to "Acme A.Ş." needs Turkish-aware normalisation
        that Monday's `contains_text` operator cannot do.
        """
        if self._items is not None and not refresh:
            return self._items

        items: list[dict] = []
        cursor: str | None = None
        while True:
            data = self.gql(
                """
                query ($board: [ID!], $cursor: String) {
                  boards(ids: $board) {
                    items_page(limit: 500, cursor: $cursor) {
                      cursor
                      items { id name }
                    }
                  }
                }
                """,
                {"board": [self.board_id], "cursor": cursor},
            )
            boards = data.get("boards") or []
            if not boards:
                break
            page = boards[0].get("items_page") or {}
            items.extend(page.get("items") or [])
            cursor = page.get("cursor")
            if not cursor:
                break

        self._items = items
        return items

    def find_item_by_name(self, name: str) -> dict | None:
        return match_item(self.all_items(), name)

    def create_item(
        self, name: str, group_id: str = "", column_values: dict | None = None
    ) -> dict:
        variables: dict[str, Any] = {
            "board": self.board_id,
            "name": name,
            "values": json.dumps(column_values or {}, ensure_ascii=False),
        }
        group_clause = ""
        if group_id:
            group_clause = "group_id: $group,"
            variables["group"] = group_id

        query = f"""
            mutation ($board: ID!, $name: String!, $values: JSON!{', $group: String!' if group_id else ''}) {{
              create_item(
                board_id: $board,
                {group_clause}
                item_name: $name,
                column_values: $values,
                create_labels_if_missing: false
              ) {{ id name }}
            }}
        """
        data = self.gql(query, variables)
        item = data.get("create_item")
        if not item:
            raise MondayError("create_item returned nothing.")
        if self._items is not None:
            self._items.append(item)
        return item

    def update_item_columns(self, item_id: str, column_values: dict) -> None:
        if not column_values:
            return
        self.gql(
            """
            mutation ($board: ID!, $item: ID!, $values: JSON!) {
              change_multiple_column_values(
                board_id: $board, item_id: $item, column_values: $values
              ) { id }
            }
            """,
            {
                "board": self.board_id,
                "item": str(item_id),
                "values": json.dumps(column_values, ensure_ascii=False),
            },
        )

    def rename_item(self, item_id: str, new_name: str) -> None:
        self.gql(
            """
            mutation ($board: ID!, $item: ID!, $values: JSON!) {
              change_multiple_column_values(
                board_id: $board, item_id: $item, column_values: $values
              ) { id }
            }
            """,
            {
                "board": self.board_id,
                "item": str(item_id),
                "values": json.dumps({"name": new_name}, ensure_ascii=False),
            },
        )
        self._items = None

    def create_update(self, item_id: str, body: str) -> dict:
        data = self.gql(
            """
            mutation ($item: ID!, $body: String!) {
              create_update(item_id: $item, body: $body) { id }
            }
            """,
            {"item": str(item_id), "body": body},
        )
        update = data.get("create_update")
        if not update:
            raise MondayError("create_update returned nothing.")
        return update

    def count_existing_notes(self, item_id: str) -> int:
        """How many !note updates this item already carries, across every run.

        A follow-up email weeks later still needs to continue the numbering
        ("Note Update 3") rather than restart at 1, so the count has to come
        from Monday itself rather than anything tracked locally.
        """
        data = self.gql(
            """
            query ($item: [ID!]) {
              items(ids: $item) { updates(limit: 200) { body } }
            }
            """,
            {"item": [str(item_id)]},
        )
        items = data.get("items") or []
        if not items:
            return 0
        updates = items[0].get("updates") or []
        return sum(1 for u in updates if "📝 Note" in (u.get("body") or ""))

    def add_file_to_update(
        self, update_id: str, filename: str, content: bytes, mime_type: str
    ) -> None:
        """Multipart upload. Monday wants the file under a name referenced by
        `map`, which is why this cannot go through the normal JSON endpoint."""
        query = (
            "mutation ($file: File!) {"
            f" add_file_to_update(update_id: {int(update_id)}, file: $file) {{ id }} }}"
        )
        headers = {
            "Authorization": self.session.headers["Authorization"],
            "API-Version": self.session.headers["API-Version"],
        }
        response = requests.post(
            FILE_URL,
            headers=headers,
            data={"query": query, "map": json.dumps({"image": "variables.file"})},
            files={"image": (filename, content, mime_type or "application/octet-stream")},
            timeout=120,
        )
        if response.status_code >= 400:
            raise MondayError(f"file upload HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if payload.get("errors"):
            raise MondayError(
                "; ".join(e.get("message", str(e)) for e in payload["errors"])
            )

    def add_file_to_column(
        self, item_id: str, column_id: str, filename: str, content: bytes, mime_type: str
    ) -> None:
        """Upload a file into a "file"-type column (e.g. Pitch Deck (PDF)),
        separate from `add_file_to_update` which attaches to the activity
        feed. The two are independent — attaching to one does not remove it
        from the other, so a deck can sit in both places at once."""
        query = (
            "mutation ($file: File!) {"
            f" add_file_to_column(item_id: {int(item_id)}, column_id: \"{column_id}\","
            " file: $file) { id } }"
        )
        headers = {
            "Authorization": self.session.headers["Authorization"],
            "API-Version": self.session.headers["API-Version"],
        }
        response = requests.post(
            FILE_URL,
            headers=headers,
            data={"query": query, "map": json.dumps({"image": "variables.file"})},
            files={"image": (filename, content, mime_type or "application/octet-stream")},
            timeout=120,
        )
        if response.status_code >= 400:
            raise MondayError(f"file upload HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if payload.get("errors"):
            raise MondayError(
                "; ".join(e.get("message", str(e)) for e in payload["errors"])
            )

    # ----------------------------------------------------------------- misc
    def item_url(self, item_id: str) -> str:
        if self._board_url is None:
            try:
                self.board_meta()
            except MondayError:
                return ""
        if not self._board_url:
            return ""
        return f"{self._board_url}/pulses/{item_id}"


def match_item(items: list[dict], name: str) -> dict | None:
    """Find the board item a name refers to.

    Exact match on the normalised key first, then a prefix relationship, which
    is what makes a follow-up forward ("ACME follow up") land on the deal
    created by the first one ("ACME") instead of spawning a duplicate.
    """
    key = normalize_company(name)
    if not key:
        return None

    for item in items:
        if normalize_company(item.get("name", "")) == key:
            return item

    if len(key) < 4:
        return None

    # Prefer the longest existing name that the key extends, so "Northwind Co Seed"
    # matches "Northwind Co" rather than a shorter "Northwind" if both exist.
    best: dict | None = None
    best_length = 0
    for item in items:
        other = normalize_company(item.get("name", ""))
        if not other or len(other) < 4:
            continue
        if key.startswith(other + " ") or other.startswith(key + " "):
            if len(other) > best_length:
                best, best_length = item, len(other)
    return best


def encode_column_value(column_type: str, value: Any) -> Any:
    """Shape a Python value the way Monday expects for that column type."""
    if value in (None, "", []):
        return None

    if column_type in ("status", "color"):
        return {"label": str(value)}
    if column_type == "dropdown":
        labels = value if isinstance(value, list) else [value]
        return {"labels": [str(v) for v in labels]}
    if column_type == "email":
        return {"email": str(value), "text": str(value)}
    if column_type in ("link", "url"):
        url = str(value)
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return {"url": url, "text": str(value)}
    if column_type == "date":
        return {"date": str(value)}
    if column_type == "phone":
        digits = "".join(ch for ch in str(value) if ch.isdigit() or ch == "+")
        return {"phone": digits, "countryShortName": "TR"}
    if column_type in ("people", "multiple-person"):
        ids = value if isinstance(value, list) else [value]
        return {
            "personsAndTeams": [{"id": int(i), "kind": "person"} for i in ids]
        }
    if column_type == "long_text":
        return {"text": str(value)[:2000]}
    if column_type in ("numbers", "numeric"):
        return str(value)
    if column_type == "checkbox":
        return {"checked": "true" if value else "false"}
    return str(value)
