"""Extracting destination URLs and their anchor text from a message.

Mail's `content` is a plain-text rendering with every hyperlink stripped, so an opportunity
email arrives with the prose but not the link — the exact friction Synth exists to remove.
The raw RFC822 source keeps the HTML part, but it is also where multi-megabyte base64
attachments live, so the source is parsed as MIME and only the HTML parts are read.
"""
from __future__ import annotations

import email
import email.policy
import re
from html.parser import HTMLParser

from synth.applekit import call

# Tracking, unsubscribe and image-beacon links are noise in a brief.
NOISE = re.compile(
    r"(unsubscribe|list-manage|mailchimp|sendgrid|constantcontact|googleusercontent|"
    r"\.(png|jpe?g|gif|css|js)(\?|$)|/track/|/wf/open|utm_medium=email&?$)", re.I)


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            self._href = href if href.startswith(("http://", "https://")) else None
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.found.append((" ".join("".join(self._text).split())[:120], self._href))
            self._href, self._text = None, []


def html_parts(source: str) -> list[str]:
    msg = email.message_from_string(source, policy=email.policy.default)
    out = []
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        if part.get_filename():          # an attached .html, not the body
            continue
        try:
            out.append(part.get_content())
        except Exception:
            payload = part.get_payload(decode=True)
            if payload:
                out.append(payload.decode("utf-8", errors="replace"))
    return out


def extract(account: str, index: int, message_id: str, mailbox: str = "INBOX",
            max_bytes: int = 3_000_000, limit: int = 40) -> dict:
    src = call("mail_source", account=account, index=index, messageId=message_id,
               mailbox=mailbox, maxBytes=max_bytes, timeout=900)
    parts = html_parts(src["source"])
    anchors: list[tuple[str, str]] = []
    for part in parts:
        p = _Anchors()
        try:
            p.feed(part)
        except Exception:
            continue
        anchors.extend(p.found)

    if not anchors:
        # Some senders ship plain text only; bare URLs still count.
        for part in parts or [src["source"][:200_000]]:
            for u in re.findall(r"https?://[^\s<>\"')]+", part):
                anchors.append(("", u))

    seen, links = set(), []
    for text, url in anchors:
        url = url.strip().rstrip(".,);")
        if url in seen or NOISE.search(url):
            continue
        seen.add(url)
        links.append({"text": text, "url": url})
        if len(links) >= limit:
            break
    return {"messageId": message_id, "truncated": src["truncated"],
            "source_bytes": src["bytes"], "html_parts": len(parts), "links": links}
