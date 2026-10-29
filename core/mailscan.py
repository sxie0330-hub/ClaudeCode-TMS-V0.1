"""Daily mailbox scan: find e-mails about open Loads and let AI propose updates.

Once a day (Cloud Scheduler → /tasks/scan-email/) for every connected mailbox:
  1. Collect the identifiers of open projects' Loads (container, MBL, carrier
     load numbers) and search Gmail for messages since the last scan that
     mention any of them. Messages that mention none are never opened.
  2. Group the new messages by project and run ONE AI extraction per project
     (core.ai.extract with source=GMAIL). Results land in the normal
     "待确认" review; nothing is written until a person confirms.
  3. Remember every message id so nothing is read twice.

Access is read-only (gmail.readonly), granted by each person for their own
mailbox, stored encrypted, and can be disconnected at any time.
"""
import base64
import hashlib
import html
import logging
import re
import time
from datetime import timedelta
from email.utils import parseaddr
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core import signing
from django.utils import timezone

from . import ai
from .models import AiExtraction, GmailConnection, Project, ScannedMessage, Shipment, Status

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
FIRST_SCAN_DAYS = 3  # how far back the very first scan looks
MAX_MESSAGES = 200  # per mailbox per run
MAX_CHARS_PER_MESSAGE = 6000
TIME_BUDGET_SECONDS = 360  # plus one AI call (≤200 s) stays inside the 600 s request timeout
IDS_PER_QUERY = 25
MIN_ID_LEN = 6


class ScanError(Exception):
    pass


# ---------------------------------------------------------------------------
# Token storage and OAuth
# ---------------------------------------------------------------------------
def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(("gmail:" + settings.SECRET_KEY).encode()).digest())
    return Fernet(key)


def encrypt(token):
    return _fernet().encrypt(token.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ScanError("保存的授权无法解密（网站密钥可能变了），请重新连接邮箱") from e


def _client():
    app = settings.SOCIALACCOUNT_PROVIDERS["google"]["APP"]
    if not app["client_id"] or not app["secret"]:
        raise ScanError("Google 登录还没配置（GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET）")
    return app["client_id"], app["secret"]


def auth_url(request, user):
    client_id, _ = _client()
    state = signing.dumps({"u": user.pk}, salt="gmail-connect")
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": request.build_absolute_uri("/mail/callback/"),
        "response_type": "code", "scope": SCOPE, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "false", "login_hint": user.email, "state": state,
    })


def finish_connect(request, user, code, state):
    """Exchange the code, check the mailbox belongs to this user, save the token."""
    try:
        data = signing.loads(state, salt="gmail-connect", max_age=900)
    except signing.BadSignature as e:
        raise ScanError("授权链接已过期，请重新连接") from e
    if data.get("u") != user.pk:
        raise ScanError("授权和当前登录的账号不一致")
    client_id, secret = _client()
    r = requests.post(TOKEN_URL, data={
        "code": code, "client_id": client_id, "client_secret": secret,
        "redirect_uri": request.build_absolute_uri("/mail/callback/"), "grant_type": "authorization_code",
    }, timeout=20)
    if r.status_code != 200:
        raise ScanError(f"Google 拒绝了授权：{r.text[:200]}")
    tok = r.json()
    if SCOPE not in tok.get("scope", "").split():
        raise ScanError("没有勾选“查看您的电子邮件”权限，请重新连接并允许")
    if not tok.get("refresh_token"):
        raise ScanError("Google 没有返回长期授权，请到 myaccount.google.com/permissions 移除 TMS 后重新连接")
    profile = _get(tok["access_token"], "/profile")
    mailbox = profile.get("emailAddress", "").lower()
    if mailbox != (user.email or "").lower():
        raise ScanError(f"只能连接你自己的邮箱（{user.email}），刚才授权的是 {mailbox}")
    conn, _ = GmailConnection.objects.update_or_create(user=user, defaults={
        "email": mailbox, "refresh_token": encrypt(tok["refresh_token"]), "enabled": True, "last_error": "",
    })
    return conn


def disconnect(conn):
    """Revoke at Google (best effort) and forget the token."""
    try:
        requests.post("https://oauth2.googleapis.com/revoke", data={"token": decrypt(conn.refresh_token)}, timeout=10)
    except Exception:  # noqa: BLE001
        log.warning("revoke failed for %s", conn.email)
    conn.delete()


def _access_token(conn):
    client_id, secret = _client()
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id, "client_secret": secret, "refresh_token": decrypt(conn.refresh_token),
        "grant_type": "refresh_token",
    }, timeout=20)
    if r.status_code != 200:
        raise ScanError("邮箱授权已失效，请重新连接邮箱" if "invalid_grant" in r.text else f"刷新授权失败：{r.text[:200]}")
    return r.json()["access_token"]


def _get(token, path, params=None):
    r = requests.get(API + path, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code != 200:
        raise ScanError(f"Gmail 返回错误 {r.status_code}：{r.text[:200]}")
    return r.json()


# ---------------------------------------------------------------------------
# What to look for
# ---------------------------------------------------------------------------
def open_loads():
    """Loads of projects that are still running."""
    return (Shipment.objects.exclude(status__in=[Status.CLOSED, Status.CANCELLED])
            .filter(project__status__in=[Project.Status.PENDING, Project.Status.ACTIVE, Project.Status.DELIVERED])
            .select_related("project", "master_bl").prefetch_related("legs"))


def identifiers(loads):
    """{IDENTIFIER: set(project ids)} for every searchable number on these Loads."""
    out = {}
    for s in loads:
        ids = [s.container_number, s.master_bl.number if s.master_bl_id else ""]
        ids += [leg.carrier_load_number for leg in s.legs.all()]
        for raw in ids:
            ident = re.sub(r"\s", "", raw or "").upper()
            if len(ident) >= MIN_ID_LEN and re.fullmatch(r"[A-Z0-9#\-]+", ident):
                out.setdefault(ident, set()).add(s.project_id)
    return out


def open_projects():
    return Project.objects.filter(status__in=[Project.Status.PENDING, Project.Status.ACTIVE, Project.Status.DELIVERED]).select_related("customer")


def project_terms(projects):
    """{project id: (code digits, customer word)} so projects can be found before they have container numbers.

    Both must appear in an e-mail: "4915" alone would match far too much."""
    out = {}
    for p in projects:
        code = re.sub(r"\D", "", p.monday_item_id or "").lstrip("0")
        word = next(iter(re.findall(r"[A-Za-z][A-Za-z\-]{2,}", p.customer.name or "")), "")
        if len(code) >= 4 and word:
            out[p.pk] = (code, word.upper())
    return out


def _queries(idents, since, pterms=None):
    after = f"after:{int(since.timestamp())}"
    terms = [f'"{k}"' for k in sorted(idents)]
    terms += [f'("{code}" "{word}")' for code, word in sorted(set((pterms or {}).values()))]
    for i in range(0, len(terms), IDS_PER_QUERY):
        yield f"{after} -in:chats -in:drafts (" + " OR ".join(terms[i:i + IDS_PER_QUERY]) + ")"


# ---------------------------------------------------------------------------
# Reading a message
# ---------------------------------------------------------------------------
def _b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _body(payload):
    """Plain text of a message: text/plain if present, else stripped HTML."""
    plain, htmls = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            htmls.append(_b64(data))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    if plain:
        return "\n".join(plain)
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", "\n".join(htmls))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


SHEET_EXTS = (".xlsx", ".csv", ".tsv", ".txt")
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_CHARS_PER_ATTACHMENT = 20000


def _attachments(payload):
    """[(filename, attachment id or None, inline data or None, size)] of a message."""
    out = []

    def walk(part):
        name = part.get("filename") or ""
        body = part.get("body", {})
        if name:
            out.append((name, body.get("attachmentId"), body.get("data"), body.get("size", 0)))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    return out


def _attachment_text(token, mid, name, aid, data, size):
    """Text of a spreadsheet attachment, or None if it is not one we read."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    if not name.lower().endswith(SHEET_EXTS) or size > MAX_ATTACHMENT_BYTES:
        return None
    raw = _download(token, mid, aid, data)
    if not raw:
        return None
    try:
        return ai.sheet_to_text(SimpleUploadedFile(name, raw))[:MAX_CHARS_PER_ATTACHMENT]
    except ai.AiError:
        return None


def _download(token, mid, aid, data):
    if data is None and aid:
        data = _get(token, f"/messages/{mid}/attachments/{aid}").get("data")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)) if data else b""


def read_message(token, mid):
    """Text of a message with its spreadsheets inlined. PDFs and images are only
    listed in "docs" here; fetch_docs() downloads them for the messages that matter."""
    m = _get(token, f"/messages/{mid}", {"format": "full"})
    payload = m.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    body = re.sub(r"[ \t]+", " ", _body(payload))
    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()[:MAX_CHARS_PER_MESSAGE]
    extra, unread, docs = [], [], []
    for name, aid, data, size in _attachments(payload):
        mime = ai.doc_mime(name)
        if mime and size <= ai.MAX_DOC_BYTES:
            docs.append({"message_id": mid, "name": name, "mime": mime, "aid": aid, "inline": data, "size": size})
            continue
        text = _attachment_text(token, mid, name, aid, data, size)
        if text:
            extra.append(f"--- 附件：{name} ---\n{text}")
        else:
            unread.append(name)
    if extra:
        body += "\n\n" + "\n\n".join(extra)
    if docs:
        body += "\n\n（附件，AI 直接阅读：" + "、".join(d["name"] for d in docs) + "）"
    if unread:
        body += "\n\n（未读取的附件：" + "、".join(unread[:20]) + "）"
    return {
        "id": mid, "internal": int(m.get("internalDate") or 0), "subject": headers.get("subject", "")[:300], "from": parseaddr(headers.get("from", ""))[1] or headers.get("from", ""),
        "date": headers.get("date", ""), "text": body, "unread_attachments": unread, "docs": docs,
    }


def fetch_docs(token, msgs):
    """Download the PDFs / images of these messages, newest first (the latest POD
    or rate confirmation matters most), within what one AI request can take."""
    out, total, n = [], 0, 0
    for m in sorted(msgs, key=lambda m: m.get("internal", 0), reverse=True):
        for d in m.get("docs", []):
            if n >= ai.MAX_DOCS or total + d["size"] > ai.MAX_DOCS_BYTES:
                out.append({"name": d["name"], "mime": d["mime"], "data": None})  # listed as not read
                continue
            data = _download(token, d["message_id"], d["aid"], d["inline"])
            if data and len(data) <= ai.MAX_DOC_BYTES:
                out.append({"name": d["name"], "mime": d["mime"], "data": data})
                total, n = total + len(data), n + 1
    return out


def _messages_text(msgs):
    return "\n\n".join(
        f"=== 邮件 {i + 1}/{len(msgs)} ===\nFrom: {m['from']}\nDate: {m['date']}\nSubject: {m['subject']}\n\n{m['text']}"
        for i, m in enumerate(msgs)
    )[: ai.MAX_EMAIL_CHARS]


def gmail_query(text):
    """What the person typed -> a Gmail search: a subject, or Gmail syntax if they used operators."""
    text = (text or "").strip()
    if not text:
        raise ScanError("请输入邮件主题")
    if re.search(r"\b(subject|from|to|has|after|before|label|in|filename):", text, re.I):
        return text
    return 'subject:"' + text.replace('"', " ") + '"'


MAX_FIND = 15


def find_and_extract(conn, text, project=None, caller=None):
    """Read the e-mails matching a subject (any date, with attachments) and run one AI extraction."""
    token = _access_token(conn)
    res = _get(token, "/messages", {"q": gmail_query(text) + " -in:chats -in:drafts", "maxResults": MAX_FIND})
    ids = [m["id"] for m in res.get("messages", [])]
    if not ids:
        raise ScanError(f"邮箱里没找到主题包含「{text}」的邮件")
    msgs = [read_message(token, mid) for mid in ids]
    msgs.sort(key=lambda m: m["date"])
    try:
        ext = ai.extract(_messages_text(msgs), conn.user, project=project, caller=caller, source=AiExtraction.Source.GMAIL,
                         attachments=fetch_docs(token, msgs))
    except ai.AiError as e:
        raise ScanError(str(e)) from e
    for m in msgs:
        ScannedMessage.objects.update_or_create(connection=conn, message_id=m["id"],
                                                defaults={"subject": m["subject"], "extraction": ext})
    return ext, len(msgs)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def scan_connection(conn, caller=None, now=None):
    """Scan one mailbox. Returns (result text, complete). Raises ScanError on access problems.

    When not complete, the caller keeps the old last_scan_at so the next run
    searches the same window again; messages already handled are skipped."""
    now = now or timezone.now()
    started = time.monotonic()
    loads = list(open_loads())
    idents = identifiers(loads)
    pterms = project_terms(open_projects())
    if not idents and not pterms:
        return "没有在执行的项目可对应", True
    since = conn.last_scan_at - timedelta(hours=1) if conn.last_scan_at else now - timedelta(days=FIRST_SCAN_DAYS)
    token = _access_token(conn)

    seen = set(conn.scanned.values_list("message_id", flat=True))
    ids = []
    for q in _queries(idents, since, pterms):
        page = None
        while len(ids) < MAX_MESSAGES:
            params = {"q": q, "maxResults": 100}
            if page:
                params["pageToken"] = page
            res = _get(token, "/messages", params)
            for m in res.get("messages", []):
                if m["id"] not in seen and m["id"] not in ids:
                    ids.append(m["id"])
            page = res.get("nextPageToken")
            if not page:
                break

    by_project = {}
    for mid in ids[:MAX_MESSAGES]:
        msg = read_message(token, mid)
        hay = (msg["subject"] + "\n" + msg["text"]).upper()
        projects = set()
        for ident, pids in idents.items():
            if ident in hay:
                projects |= pids
        for pid, (code, word) in pterms.items():
            if re.search(rf"(?<!\d){code}(?!\d)", hay) and word in hay:
                projects.add(pid)
        if not projects:  # Gmail matched on something we don't count (e.g. a longer token); skip it
            ScannedMessage.objects.get_or_create(connection=conn, message_id=mid, defaults={"subject": msg["subject"]})
            continue
        for pid in projects:
            by_project.setdefault(pid, []).append(msg)

    created, errors = 0, []
    projects = {p.pk: p for p in Project.objects.filter(pk__in=by_project)}
    for pid, msgs in by_project.items():
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            errors.append("时间不够，剩余项目明天继续")
            break
        msgs.sort(key=lambda m: m["date"])
        text = _messages_text(msgs)
        try:
            ext = ai.extract(text, conn.user, project=projects[pid], caller=caller, source=AiExtraction.Source.GMAIL,
                             attachments=fetch_docs(token, msgs))
        except ai.AiError as e:
            errors.append(f"{projects[pid].name}：{e}")
            continue  # leave these messages unmarked so the next run retries them
        if not ext.changes.exists() and not ai.proposals(ext):
            ext.status = AiExtraction.State.DONE  # nothing new: keep it out of the to-do list
            ext.save(update_fields=["status"])
        else:
            created += 1
        for m in msgs:
            ScannedMessage.objects.update_or_create(connection=conn, message_id=m["id"],
                                                    defaults={"subject": m["subject"], "extraction": ext})
    relevant = len({m["id"] for msgs in by_project.values() for m in msgs})
    result = f"读了 {relevant} 封相关邮件，{created} 个项目有更新待确认"
    if errors:
        result += "；" + "；".join(errors)
    return result[:300], not errors and len(ids) <= MAX_MESSAGES


def scan_all(caller=None):
    out = []
    for conn in GmailConnection.objects.filter(enabled=True).select_related("user"):
        now = timezone.now()
        out.append(scan_one(conn, caller=caller, now=now))
    return out


def scan_one(conn, caller=None, now=None):
    now = now or timezone.now()
    try:
        res, complete = scan_connection(conn, caller=caller, now=now)
    except (ScanError, requests.RequestException) as e:
        conn.last_error = str(e)[:500]
        conn.save(update_fields=["last_error"])
        return {"mailbox": conn.email, "error": conn.last_error}
    if complete:
        conn.last_scan_at = now
    conn.last_result, conn.last_error = res, ""
    conn.save(update_fields=["last_scan_at", "last_result", "last_error"])
    return {"mailbox": conn.email, "result": res}
