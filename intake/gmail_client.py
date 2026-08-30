"""Gmail access via the official API.

Read-and-label only, plus optional confirmation replies. One OAuth scope,
`gmail.modify`, which covers reading messages, applying labels and sending —
but not permanent deletion. Nothing here scrapes IMAP or touches a password.
"""

from __future__ import annotations

import base64
import logging
import os
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

LOG = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Stamped on every reply we send, and refused on the way back in.
LOOP_HEADER = "X-Monday-Intake"


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str, user_id: str = "me"):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.user_id = user_id
        self._service = None
        self._label_cache: dict[str, str] = {}

    # ------------------------------------------------------------------ auth
    def authorize(self, interactive: bool = True):
        creds: Credentials | None = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)

        if creds and creds.valid:
            pass
        elif creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif interactive:
            if not os.path.exists(self.credentials_file):
                raise FileNotFoundError(
                    f"{self.credentials_file} not found. Download the OAuth client "
                    "(Desktop app) from Google Cloud Console first — see README."
                )
            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError(
                "No valid Gmail token. Run `python -m intake.cli auth` once."
            )

        with open(self.token_file, "w", encoding="utf-8") as handle:
            handle.write(creds.to_json())
        os.chmod(self.token_file, 0o600)
        return creds

    @property
    def service(self):
        if self._service is None:
            creds = self.authorize(interactive=False)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def profile_address(self) -> str:
        return self.service.users().getProfile(userId=self.user_id).execute().get(
            "emailAddress", ""
        )

    # ---------------------------------------------------------------- labels
    def _labels(self, refresh: bool = False) -> dict[str, str]:
        if self._label_cache and not refresh:
            return self._label_cache
        response = self.service.users().labels().list(userId=self.user_id).execute()
        self._label_cache = {
            label["name"]: label["id"] for label in response.get("labels", [])
        }
        return self._label_cache

    def ensure_label(self, name: str) -> str:
        labels = self._labels()
        if name in labels:
            return labels[name]
        created = (
            self.service.users()
            .labels()
            .create(
                userId=self.user_id,
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._label_cache[name] = created["id"]
        return created["id"]

    def add_label(self, message_id: str, label_name: str) -> None:
        label_id = self.ensure_label(label_name)
        self.service.users().messages().modify(
            userId=self.user_id, id=message_id, body={"addLabelIds": [label_id]}
        ).execute()

    # -------------------------------------------------------------- messages
    def search(self, query: str, limit: int = 50) -> Iterator[str]:
        request = (
            self.service.users()
            .messages()
            .list(userId=self.user_id, q=query, maxResults=min(limit, 100))
        )
        seen = 0
        while request is not None and seen < limit:
            response = request.execute()
            for message in response.get("messages", []):
                yield message["id"]
                seen += 1
                if seen >= limit:
                    return
            request = (
                self.service.users()
                .messages()
                .list_next(previous_request=request, previous_response=response)
            )

    def get_message(self, message_id: str) -> dict:
        raw = (
            self.service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="full")
            .execute()
        )
        return parse_message(raw)

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId=self.user_id, messageId=message_id, id=attachment_id)
            .execute()
        )
        return base64.urlsafe_b64decode(response.get("data", ""))

    # ----------------------------------------------------------------- reply
    def reply(self, message: dict, body: str, subject: str = "", sender: str = "") -> str:
        """Reply in-thread so the confirmation sits under the original forward.

        Passing `threadId` is what actually keeps this in the same Gmail
        conversation — the subject line does not have to match for that, so a
        clean custom `subject` (e.g. "✅ ACME added to the deal pipeline")
        can replace the noisy "Re: <original subject with the command in it>"
        without breaking the threading.

        Returns the sent message id. The reply carries a marker header and is
        flagged as an auto-reply so that it can never be mistaken for new work
        if it ever lands back in the watched mailbox.
        """
        to_address = parseaddr(message["headers"].get("from", ""))[1]
        if not to_address:
            return ""

        mail = EmailMessage()
        mail["To"] = to_address
        if sender:
            mail["From"] = sender
        original_subject = message["headers"].get("subject", "")
        mail["Subject"] = subject or (
            original_subject
            if original_subject.lower().startswith("re:")
            else f"Re: {original_subject}"
        )
        original_id = message["headers"].get("message-id")
        if original_id:
            mail["In-Reply-To"] = original_id
            mail["References"] = original_id
        mail[LOOP_HEADER] = "reply"
        mail["Auto-Submitted"] = "auto-replied"
        mail.set_content(body)

        encoded = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        try:
            sent = (
                self.service.users()
                .messages()
                .send(
                    userId=self.user_id,
                    body={"raw": encoded, "threadId": message.get("thread_id")},
                )
                .execute()
            )
            return sent.get("id", "")
        except HttpError as exc:
            LOG.warning("Could not send confirmation reply: %s", exc)
            return ""


# --------------------------------------------------------------------------
# MIME walking
# --------------------------------------------------------------------------
def _decode(data: str) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _walk(part: dict, out: dict) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body", {}) or {}
    filename = part.get("filename") or ""

    if filename and body.get("attachmentId"):
        out["attachments"].append(
            {
                "filename": filename,
                "mimeType": mime,
                "attachmentId": body["attachmentId"],
                "size": body.get("size", 0),
            }
        )
    elif mime == "text/plain" and body.get("data"):
        out["plain"] += _decode(body["data"])
    elif mime == "text/html" and body.get("data"):
        out["html"] += _decode(body["data"])

    for child in part.get("parts", []) or []:
        _walk(child, out)


def parse_message(raw: dict) -> dict:
    """Flatten the Gmail payload into headers, bodies and attachments."""
    payload = raw.get("payload", {}) or {}
    headers = {
        header.get("name", "").lower(): header.get("value", "")
        for header in payload.get("headers", []) or []
    }
    out: dict = {"plain": "", "html": "", "attachments": []}
    _walk(payload, out)

    return {
        "id": raw.get("id"),
        "thread_id": raw.get("threadId"),
        "label_ids": raw.get("labelIds", []),
        "internal_date": raw.get("internalDate"),
        "snippet": raw.get("snippet", ""),
        "headers": headers,
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "from_email": parseaddr(headers.get("from", ""))[1].lower(),
        "from_name": parseaddr(headers.get("from", ""))[0],
        "date": headers.get("date", ""),
        "plain": out["plain"],
        "html": out["html"],
        "attachments": out["attachments"],
    }
