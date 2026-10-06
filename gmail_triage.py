"""
Gmail Triage MCP server
=======================

A small MCP server that lets Claude sort your Gmail inbox into triage labels
(Triage/Needs reply, Triage/FYI, ...). Emails stay in your inbox; they just get tagged.

Run modes:
    python gmail_triage.py auth   -> one-time Google sign-in, saves token.json
    python gmail_triage.py        -> starts the MCP server (Claude launches this)

Note: in server mode, never print() to stdout. Claude talks to the server over
stdout, so stray prints corrupt the conversation. Use stderr for debugging.
"""

import base64
import html
import re
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from mcp.server.mcpserver import MCPServer  # MCP Python SDK v2+

# ---------------------------------------------------------------------------
# Config — edit these to change your buckets
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
CREDS_FILE = HERE / "credentials.json"   # downloaded from Google Cloud
TOKEN_FILE = HERE / "token.json"         # created by `python gmail_triage.py auth`
RULES_FILE = HERE / "triage_rules.md"    # your plain-English triage rules

# gmail.modify = read + change labels + archive. (No permanent delete.)
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

PARENT_LABEL = "Triage"
CATEGORIES = ["Needs reply", "FYI", "Receipts", "Newsletters", "Low priority"]
# Categories that also get archived (removed from the inbox) when triaged.
# Empty = tag only, nothing leaves your inbox. To archive a bucket later,
# add it here, e.g. {"Newsletters"}.
ARCHIVE_CATEGORIES: set[str] = set()

mcp = MCPServer("gmail-triage")


# ---------------------------------------------------------------------------
# Gmail plumbing
# ---------------------------------------------------------------------------

def authorise() -> None:
    """One-time browser sign-in. Saves a refresh token to token.json."""
    if not CREDS_FILE.exists():
        sys.exit(f"Missing {CREDS_FILE.name}. Download it from Google Cloud first (see README).")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"Signed in. Saved {TOKEN_FILE.name}.")


_service = None

def gmail():
    """Return a cached Gmail API client, refreshing the token if needed."""
    global _service
    if _service is not None:
        return _service
    if not TOKEN_FILE.exists():
        raise RuntimeError("Not signed in yet. Run: python gmail_triage.py auth")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise RuntimeError("Sign-in expired. Run: python gmail_triage.py auth")
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


_labels: dict[str, str] = {}  # label name -> label id

def label_id(name: str) -> str:
    """Look up a label's id by name, creating the label if it doesn't exist."""
    svc = gmail()
    if not _labels:
        for lab in svc.users().labels().list(userId="me").execute().get("labels", []):
            _labels[lab["name"]] = lab["id"]
    if name not in _labels:
        # Create the parent first so "Triage/X" shows up nested in Gmail.
        if "/" in name:
            label_id(name.rsplit("/", 1)[0])
        lab = svc.users().labels().create(
            userId="me",
            body={"name": name, "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
        _labels[name] = lab["id"]
    return _labels[name]


def triage_label_ids() -> dict[str, str]:
    """Category name -> Gmail label id for every triage bucket."""
    return {c: label_id(f"{PARENT_LABEL}/{c}") for c in CATEGORIES}


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def extract_text(payload: dict) -> str:
    """Pull readable text out of a Gmail message payload (prefers text/plain)."""
    plain, htm = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            if mime == "text/plain":
                plain.append(text)
            elif mime == "text/html":
                htm.append(text)
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if htm:  # crude HTML -> text fallback
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(htm))
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()
    return ""


# ---------------------------------------------------------------------------
# MCP tools — these are what Claude can call
# ---------------------------------------------------------------------------

@mcp.tool()
def list_untriaged(max_results: int = 20, query: str = "in:inbox") -> list[dict]:
    """List inbox emails that don't have a Triage label yet.

    Returns id, sender, subject, date, a short snippet and whether it's unread.
    `query` uses Gmail search syntax, e.g. "in:inbox newer_than:7d".
    """
    svc = gmail()
    triage_ids = set(triage_label_ids().values())
    resp = svc.users().messages().list(
        userId="me", q=query, maxResults=min(max_results * 2, 100)
    ).execute()

    out = []
    for ref in resp.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe"],
        ).execute()
        if triage_ids & set(msg.get("labelIds", [])):
            continue  # already triaged
        out.append({
            "id": msg["id"],
            "from": header(msg, "From"),
            "subject": header(msg, "Subject"),
            "date": header(msg, "Date"),
            "snippet": msg.get("snippet", ""),
            "unread": "UNREAD" in msg.get("labelIds", []),
            "is_mailing_list": bool(header(msg, "List-Unsubscribe")),
        })
        if len(out) >= max_results:
            break
    return out


@mcp.tool()
def read_email(message_id: str, max_chars: int = 4000) -> dict:
    """Read the full text of one email (truncated to max_chars).

    Only use this when the snippet isn't enough to decide a category.
    """
    msg = gmail().users().messages().get(userId="me", id=message_id, format="full").execute()
    body = extract_text(msg.get("payload", {}))
    return {
        "id": message_id,
        "from": header(msg, "From"),
        "to": header(msg, "To"),
        "subject": header(msg, "Subject"),
        "date": header(msg, "Date"),
        "body": body[:max_chars] + ("…[truncated]" if len(body) > max_chars else ""),
    }


@mcp.tool()
def apply_triage(assignments: list[dict]) -> dict:
    """Apply triage categories to many emails at once.

    `assignments` is a list like:
        [{"id": "18c...", "category": "Needs reply"},
         {"id": "18d...", "category": "Newsletters"}]

    Valid categories: Needs reply, FYI, Receipts, Newsletters, Low priority.
    Each email gets exactly one Triage label (any old one is removed).
    Emails stay in the inbox unless their category is listed in
    ARCHIVE_CATEGORIES (empty by default). Nothing is ever deleted.
    """
    ids = triage_label_ids()
    all_triage = list(ids.values())

    by_cat: dict[str, list[str]] = {}
    errors = []
    for a in assignments:
        cat = a.get("category")
        if cat not in ids:
            errors.append(f"{a.get('id')}: unknown category {cat!r}")
            continue
        by_cat.setdefault(cat, []).append(a["id"])

    svc = gmail()
    summary = {}
    for cat, msg_ids in by_cat.items():
        remove = [i for i in all_triage if i != ids[cat]]
        if cat in ARCHIVE_CATEGORIES:
            remove.append("INBOX")
        # batchModify handles up to 1000 ids per call
        for start in range(0, len(msg_ids), 1000):
            svc.users().messages().batchModify(
                userId="me",
                body={"ids": msg_ids[start:start + 1000],
                      "addLabelIds": [ids[cat]], "removeLabelIds": remove},
            ).execute()
        summary[cat] = {"count": len(msg_ids), "archived": cat in ARCHIVE_CATEGORIES}

    return {"applied": summary, "errors": errors}


@mcp.tool()
def undo_triage(message_ids: list[str]) -> str:
    """Remove Triage labels from emails (and put them back in the inbox if they'd been archived)."""
    gmail().users().messages().batchModify(
        userId="me",
        body={"ids": message_ids, "addLabelIds": ["INBOX"],
              "removeLabelIds": list(triage_label_ids().values())},
    ).execute()
    return f"Restored {len(message_ids)} email(s) to the inbox."


# ---------------------------------------------------------------------------
# MCP prompt — a ready-made "triage my inbox" instruction, shown in Claude's
# prompt/attachment menu. It reads your rules from triage_rules.md.
# ---------------------------------------------------------------------------

@mcp.prompt()
def triage_inbox(count: int = 30) -> str:
    """Triage the most recent untriaged emails using your rules."""
    rules = RULES_FILE.read_text() if RULES_FILE.exists() else "(no rules file found)"
    return f"""Triage my Gmail inbox.

1. Call list_untriaged with max_results={count}.
2. Sort each email into exactly one category: {", ".join(CATEGORIES)}.
   Use read_email only when the snippet is genuinely ambiguous.
3. Show me a table (sender, subject, proposed category) and WAIT for me to
   approve or correct it.
4. Once I approve, call apply_triage once with all assignments, then tell me
   how many went into each category.

My rules (these override your own judgement):
{rules}
"""


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        authorise()
    else:
        mcp.run()  # stdio transport — Claude Desktop / Claude Code launch this
