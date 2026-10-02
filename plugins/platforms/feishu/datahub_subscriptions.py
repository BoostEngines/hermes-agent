"""Hermes' verified Feishu subscription inbox and sequential, durable worker.

Only the Feishu SDK adapter admits events. No model tool can enqueue grants.
Subscription writes reuse the Admin API through a fixed host-owned command.
"""
from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

CHAT_ID = "oc_ab338357777d1cb2be2afd81fc6a3581"
FLYNN_EMAIL = "flynn.cao@boostengines.com"
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SHOP = re.compile(r"\b(?:[A-Z]{2}[A-Z0-9]{6,30}|\d{10,24})\b")
YEARS = re.compile(r"([123一二两三])\s*(?:年|years?\b)", re.I)
MENU = {"订阅激活菜单", "开通订阅菜单", "autoboost订阅菜单", "/ab"}


def read(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def owns_text(text):
    value = unicodedata.normalize("NFKC", text).strip()
    if value.lower() in MENU:
        return True
    # Questions, quoted examples, and completion notices are not commands.
    if re.search(r"[?？]|(?:不要|不需要|无需|不开通|不激活|暂不|取消|是否|能否|可以吗|如何|怎么|已完成|已激活|已开通|未激活|未开通|例如|示例|假设)", value):
        return False
    return any(
        re.match(r"^(?:(?:请|麻烦|帮我|帮忙|辛苦)\s*)*(?:激活|开通|监测|给|为)", line)
        and re.search(r"激活|开通|监测", line)
        and re.search(r"autoboost|(?<![a-z0-9])(?:ab|max)(?![a-z0-9])|订阅", line, re.I)
        for line in value.splitlines()
    )


def parse_targets(text, years=None):
    """Labelled blocks, TSV/CSV rows, or one email + shop per line; no guessing."""
    text = unicodedata.normalize("NFKC", text)
    if len(text) > 16000 or "```" in text or any(x.lstrip().startswith(">") for x in text.splitlines()):
        raise ValueError("请直接发送店铺信息，不要使用引用或代码块。")
    durations = YEARS.findall(text)
    normalized = {int({"一": "1", "二": "2", "两": "2", "三": "3"}.get(x, x)) for x in durations}
    if re.search(r"\d+\s*(?:个月|月|months?)|(?:[4-9]|\d{2,})\s*(?:年|years?)", text, re.I):
        raise ValueError("开通年份支持 1、2、3 年；默认 1 年。")
    if years is not None:
        if str(years) not in {"1", "2", "3"}:
            raise ValueError("开通年份支持 1、2、3 年。")
        normalized.add(int(years))
    if len(normalized) > 1:
        raise ValueError("同一批请使用相同年份，不同年份请分批发送。")
    duration = next(iter(normalized), 1)
    if re.search(r"\b(?:pro|basic|trial)\b|试用", text, re.I):
        raise ValueError("此入口只开通正式 Max 订阅。")
    targets, current = [], {}

    def finish():
        nonlocal current
        if not current:
            return
        if not current.get("email") or not (current.get("shopId") or current.get("shopCode")):
            raise ValueError("每家店铺需要邮箱及 Shop Code 或 Shop ID，不能只提供店名。")
        current["years"] = duration
        targets.append(current)
        current = {}

    for line in text.splitlines():
        line = line.strip(" \t|,-;；")
        if not line or line == "---":
            continue
        if re.match(r"(?:shop\s*name|店铺名称|店铺名)\s*:", line, re.I):
            if current.get("email"):
                finish()
            continue
        emails = EMAIL.findall(line)
        # Remove addresses first: numeric mailbox names must not become shop IDs.
        refs = SHOP.findall(EMAIL.sub("", line))
        if len(emails) > 1 or len(refs) > 1:
            raise ValueError("每行最多一组邮箱和店铺编号；多店请分行或分块。")
        if emails and current.get("email") or refs and (current.get("shopId") or current.get("shopCode")):
            finish()
        if emails:
            current["email"] = emails[0].lower()
        if refs:
            current["shopId" if refs[0].isdigit() else "shopCode"] = refs[0]
    finish()
    if not targets or len(targets) > 50:
        raise ValueError("每批支持 1–50 家店铺；请填写邮箱和 Shop Code 或 Shop ID。")
    unique = {}
    for target in targets:
        key = target.get("shopId") or target["shopCode"]
        if key in unique and unique[key] != target:
            raise ValueError("同一店铺出现不同邮箱，请核对后重新提交。")
        unique[key] = target
    return list(unique.values())


def card(text):
    return {"config": {"wide_screen_mode": True}, "elements": [{"tag": "markdown", "content": text}]}


def entry_card():
    return {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "AutoBoost 订阅激活入口"}}, "body": {"elements": [
        {"tag": "markdown", "content": "点击填写年份、邮箱和店铺编号，支持多店批量开通 Max；默认 1 年。每次开通前都会核验当前订阅。"},
        {"tag": "button", "type": "primary_filled", "text": {"tag": "plain_text", "content": "填写激活表单"}, "behaviors": [{"type": "callback", "value": {"datahub_subscription": "open"}}]},
    ]}}


def form_card(nonce):
    return {"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "AutoBoost Max 订阅激活"}}, "body": {"elements": [
        {"tag": "markdown", "content": "仅在本群生效。默认 1 年，支持 1–3 年；已有效的订阅不会重复开通或延长。可填写单店，也可粘贴多行邮箱和 Shop Code / Shop ID。"},
        {"tag": "form", "name": "subscription", "elements": [
            {"tag": "input", "name": "years", "label": {"tag": "plain_text", "content": "开通年份"}, "default_value": "1", "required": True},
            {"tag": "input", "name": "email", "label": {"tag": "plain_text", "content": "单店邮箱"}},
            {"tag": "input", "name": "shop", "label": {"tag": "plain_text", "content": "单店 Shop Code / Shop ID"}},
            {"tag": "input", "name": "accounts", "input_type": "multiline_text", "label": {"tag": "plain_text", "content": "批量店铺（与单店字段二选一）"}, "placeholder": {"tag": "plain_text", "content": "USLC32EMHS maria.garcia7104@zohomail.com\n另一店铺编号 另一邮箱"}},
            {"tag": "button", "name": "datahub_subscription_" + nonce, "type": "primary_filled", "width": "fill", "text": {"tag": "plain_text", "content": "开通 Max 订阅"}, "form_action_type": "submit"},
        ]},
    ]}}


class Store:
    def __init__(self, path, now=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.now = str(path), now
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, actor TEXT NOT NULL, created REAL NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, batch TEXT NOT NULL, target TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0, intent INTEGER NOT NULL DEFAULT 0,
                    result TEXT, fingerprint TEXT, checked_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, payload TEXT NOT NULL, message_id TEXT, attempted_at REAL, state TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS forms (nonce TEXT PRIMARY KEY, created REAL NOT NULL, message_id TEXT);
                CREATE TABLE IF NOT EXISTS grants (shop TEXT PRIMARY KEY, operation TEXT NOT NULL, years INTEGER NOT NULL, actor TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('enabled_at', ?)", (str(now()),))

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, batch, actor, targets, source="activate"):
        with self.connect() as db:
            if db.execute("INSERT OR IGNORE INTO batches VALUES (?, ?, ?, ?)", (batch, actor, self.now(), source)).rowcount == 0:
                return False
            for i, target in enumerate(targets):
                db.execute("INSERT INTO jobs (id, batch, target, status) VALUES (?, ?, ?, ?)", (digest(f"{batch}:{i}"), batch, json.dumps(target, sort_keys=True), "watch" if source == "watch" else "pending"))
        return True

    def notify(self, key, payload):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO outbox (id,payload) VALUES (?, ?)", (key, json.dumps(payload, ensure_ascii=False)))


class Gateway:
    def __init__(self, store, *, app_id, tenant, enabled=False, connection="websocket", now=time.time):
        self.store, self.app_id, self.tenant = store, app_id, tenant
        self.enabled, self.connection, self.now = enabled, connection, now

    @classmethod
    def from_environment(cls):
        return cls(Store(os.environ["HERMES_SUBSCRIPTIONS_DB"]), app_id=os.getenv("FEISHU_APP_ID", ""), tenant=os.getenv("FEISHU_TENANT_KEY", ""), enabled=os.getenv("HERMES_SUBSCRIPTIONS_ENABLED") == "true", connection=os.getenv("FEISHU_CONNECTION_MODE", ""))

    def validate(self, data, event_type, chat, actor):
        header = read(data, "header")
        if not self.enabled or self.connection != "websocket" or not self.app_id or not self.tenant:
            raise ValueError("订阅入口尚未启用。")
        if chat != CHAT_ID or read(header, "tenant_key") != self.tenant or read(header, "app_id", self.app_id) != self.app_id:
            raise ValueError("订阅激活只接受指定 staging 群的新请求。")
        if read(header, "event_type") != event_type or not re.fullmatch(r"ou_[\w-]+", actor or ""):
            raise ValueError("消息来源校验失败。")
        timestamp = float(read(header, "create_time") or 0)
        while timestamp > 100_000_000_000:
            timestamp /= 1000
        with self.store.connect() as db:
            enabled_at = float(db.execute("SELECT value FROM meta WHERE key='enabled_at'").fetchone()[0])
        if not enabled_at <= timestamp <= self.now() + 30 or self.now() - timestamp > 300:
            raise ValueError("只接受入口启用后的新请求，请重新发送。")
        event_id = read(header, "event_id")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            raise ValueError("缺少事件标识。")
        return event_id

    def message(self, data, text):
        event = read(data, "event")
        message, sender = read(event, "message"), read(event, "sender")
        actor = read(read(sender, "sender_id"), "open_id")
        self.validate(data, "im.message.receive_v1", read(message, "chat_id"), actor)
        if read(sender, "sender_type") != "user" or read(message, "chat_type") != "group" or read(message, "message_type") != "text":
            raise ValueError("只接受群成员直接发送的文本请求。")
        batch = read(message, "message_id")
        if not re.fullmatch(r"om_[\w-]+", batch or ""):
            raise ValueError("缺少消息标识。")
        # Check the original creation time too: newly delivered old history is not new input.
        created = float(read(message, "create_time") or 0) / 1000
        with self.store.connect() as db:
            enabled_at = float(db.execute("SELECT value FROM meta WHERE key='enabled_at'").fetchone()[0])
        if created < enabled_at or self.now() - created > 300 or created > self.now() + 30:
            raise ValueError("历史消息不能触发激活，请重新发送。")
        if text.lower().strip() in MENU:
            self.open_form(batch)
            return
        if not owns_text(text):
            raise ValueError("请明确说明“开通 AutoBoost Max 订阅”或“监测订阅”。")
        targets = parse_targets(text)
        watch = "监测" in text
        self.store.enqueue(batch, actor, targets, "watch" if watch else "activate")

    def open_form(self, request_id):
        nonce = digest(request_id)
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO forms (nonce,created) VALUES (?,?)", (nonce, self.now()))
        self.store.notify(f"form:{nonce}", form_card(nonce))

    def submit_form(self, data):
        event = read(data, "event")
        action, context = read(event, "action"), read(event, "context")
        actor = read(read(event, "operator"), "open_id")
        event_id = self.validate(data, "card.action.trigger", read(context, "open_chat_id"), actor)
        if (read(action, "value") or {}).get("datahub_subscription") == "open":
            with self.store.connect() as db:
                entry = db.execute("SELECT message_id FROM outbox WHERE id='menu-entry'").fetchone()
            if not entry or not entry["message_id"] or entry["message_id"] != read(context, "open_message_id"):
                raise ValueError("请使用本群原始订阅入口。")
            self.open_form(event_id)
            return
        name = str(read(action, "name") or "")
        if not re.fullmatch(r"datahub_subscription_[a-f0-9]{64}", name) or read(action, "tag") != "button":
            raise ValueError("表单操作无效。")
        nonce = name.removeprefix("datahub_subscription_")
        with self.store.connect() as db:
            form = db.execute("SELECT * FROM forms WHERE nonce=?", (nonce,)).fetchone()
        if not form or form["message_id"] != read(context, "open_message_id") or self.now() - form["created"] > 86400:
            raise ValueError("表单已失效，请在本群发送“订阅激活菜单”。")
        values = read(action, "form_value") or {}
        text = str(values.get("accounts", "")).strip()
        single = str(values.get("email", "")).strip() + " " + str(values.get("shop", "")).strip()
        if text and single.strip():
            raise ValueError("单店与批量字段请选择一种填写。")
        targets = parse_targets(text or single, values.get("years", "1"))
        # One card can admit only one batch, even with two people clicking at once.
        self.store.enqueue(f"form:{nonce}", actor, targets)


def backend(payload):
    result = subprocess.run(["sudo", "-n", "/usr/local/sbin/hermes-subscription-admin"], input=json.dumps(payload), text=True, capture_output=True, timeout=35 if payload["mode"] == "inspect" else 150)
    if result.returncode:
        try:
            code = json.loads(result.stderr).get("error", "subscription_backend_unavailable")
        except ValueError:
            code = "subscription_backend_unavailable"
        raise RuntimeError(code if re.fullmatch(r"[a-z0-9_]{1,100}", str(code)) else "subscription_backend_unavailable")
    return json.loads(result.stdout)


def send(payload, key):
    args = [os.getenv("LARK_CLI_BIN", "lark-cli"), "--profile", os.getenv("LARK_CLI_PROFILE", "datahub-hermes-ops"), "im", "+messages-send", "--as", "bot", "--chat-id", CHAT_ID, "--msg-type", "interactive", "--content", json.dumps(payload, ensure_ascii=False), "--idempotency-key", digest(key)[:40], "--format", "json"]
    result = subprocess.run(args, text=True, capture_output=True, timeout=30, check=True)
    response = json.loads(result.stdout)
    message_id = (response.get("data") or {}).get("message_id")
    if not response.get("ok") or not message_id:
        raise RuntimeError("notification_failed")
    return message_id


def fingerprint(result):
    seat = result.get("subscription")
    return f"{seat['id']}:{seat['currentPeriodEnd']}" if result.get("subscriptionReady") and seat else "inactive"


def shop_line(target, result):
    identity = result.get("identity", target)
    name = identity.get("shopName") or identity.get("shopCode") or identity.get("shopId") or "店铺"
    email = identity.get("email", target["email"])
    expiry = result.get("subscription", {}).get("currentPeriodEnd", "")
    if expiry:
        expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    return f"- {html.escape(name)}｜{html.escape(email)}" + (f"｜有效期至 {expiry}" if expiry else "")


class Worker:
    def __init__(self, store, call=backend, deliver=send):
        self.store, self.call, self.deliver = store, call, deliver
        self.delivery_lock = threading.Lock()

    def flush(self):
        with self.delivery_lock:
            self._flush()

    def _flush(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM outbox WHERE message_id IS NULL AND state='pending' ORDER BY rowid LIMIT 20").fetchall()
        for row in rows:
            now = self.store.now()
            # Feishu deduplicates for one hour. An older uncertain send needs reconciliation,
            # not a blind resend that can double-notify people.
            if row["attempted_at"] and now - row["attempted_at"] > 3500:
                # Keep other shops' notifications moving while this receipt is reconciled.
                print("subscription notification needs reconciliation: " + row["id"], flush=True)
                with self.store.connect() as db:
                    db.execute("UPDATE outbox SET state='unknown' WHERE id=?", (row["id"],))
                continue
            with self.store.connect() as db:
                db.execute("UPDATE outbox SET attempted_at=COALESCE(attempted_at, ?) WHERE id=?", (now, row["id"]))
            try:
                message_id = self.deliver(json.loads(row["payload"]), row["id"])
            except Exception:
                continue
            with self.store.connect() as db:
                db.execute("UPDATE outbox SET message_id=? WHERE id=?", (message_id, row["id"]))
                if row["id"].startswith("form:"):
                    db.execute("UPDATE forms SET message_id=? WHERE nonce=?", (message_id, row["id"][5:]))

    def tick(self, *, include_watch=True):
        self.flush()
        now = self.store.now()
        with self.store.connect() as db:
            jobs = db.execute("SELECT j.*, b.actor FROM jobs j JOIN batches b ON b.id=j.batch WHERE j.status='pending' AND j.next_at<=? ORDER BY b.created,j.rowid LIMIT 1", (now,)).fetchall()
        for job in jobs:
            target = json.loads(job["target"])
            request = {"target": target, "operationId": "hermes-sub:" + job["id"], "actor": job["actor"]}
            try:
                result = self.call({**request, "mode": "inspect"})
                if not result.get("subscriptionReady"):
                    # Commit the intent BEFORE the API call. Recovered attempts reconcile the
                    # same operation and never issue another non-idempotent manual grant.
                    with self.store.connect() as db:
                        identity = result["identity"]
                        key = identity["shopId"] + ":" + identity["uid"]
                        created = db.execute("INSERT OR IGNORE INTO grants VALUES (?, ?, ?, ?)", (key, request["operationId"], target["years"], job["actor"])).rowcount == 1
                        grant = db.execute("SELECT * FROM grants WHERE shop=?", (key,)).fetchone()
                        if grant["years"] != target["years"]:
                            raise ValueError("existing_grant_duration_conflict")
                        request["operationId"], request["actor"] = grant["operation"], grant["actor"]
                        db.execute("UPDATE jobs SET intent=1 WHERE id=?", (job["id"],))
                    result = self.call({**request, "mode": "activate", "allowCreate": created})
                if not result.get("subscriptionReady"):
                    raise RuntimeError("subscription_not_verified")
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET status='success',result=?,fingerprint=?,checked_at=? WHERE id=?", (json.dumps(result), fingerprint(result), now, job["id"]))
                    # A later watch of another request for this shop must not echo our own success.
                    db.execute("INSERT OR IGNORE INTO outbox (id,payload,message_id) VALUES (?, '{}', 'covered-by-batch')", ("observed:" + result["identity"]["shopId"] + ":" + fingerprint(result),))
            except Exception as exc:
                attempts = job["attempts"] + 1
                code = str(exc) if re.fullmatch(r"[a-z0-9_]{1,100}", str(exc)) else "subscription_backend_unavailable"
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET attempts=?,next_at=?,status=?,result=? WHERE id=?", (attempts, now + (10 if attempts == 1 else 30), "failed" if attempts >= 3 else "pending", json.dumps({"error": code}), job["id"]))
        # Recover a crash after saving terminal jobs but before composing their receipt.
        with self.store.connect() as db:
            batches = db.execute("SELECT id FROM batches WHERE source='activate' AND NOT EXISTS (SELECT 1 FROM jobs WHERE batch=batches.id AND status='pending') AND NOT EXISTS (SELECT 1 FROM outbox WHERE id='complete:' || batches.id)").fetchall()
        for batch in batches:
            self.summarize(batch["id"])
        if include_watch:
            self.watch()
        self.flush()

    def summarize(self, batch):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE batch=? ORDER BY rowid", (batch,)).fetchall()
        if any(row["status"] == "pending" for row in rows):
            return
        successes = [row for row in rows if row["status"] == "success"]
        failures = [row for row in rows if row["status"] == "failed"]
        lines = []
        if successes:
            lines.append("✅ 以下店铺的 Max 订阅已确认生效：")
            for row in successes:
                target, result = json.loads(row["target"]), json.loads(row["result"])
                label = f"｜本次开通 {target['years']} 年" if result.get("outcome") in {"activated", "binding_reconciled", "granted", "grant_reconciled"} else "｜原订阅已有效，未延长"
                lines.append(shop_line(target, result) + label)
        if failures:
            lines.append("⚠️ 以下店铺激活未完成，自动重试已结束；后续仍会监测订阅状态：")
            lines.extend(shop_line(json.loads(row["target"]), {}) + "｜" + json.loads(row["result"] or '{}').get("error", "需要核对") for row in failures)
            lines.append(f'<at email="{FLYNN_EMAIL}"></at> 请协助核对。')
        self.store.notify(f"complete:{batch}", card("\n".join(lines)))

    def watch(self):
        now = self.store.now()
        with self.store.connect() as db:
            rows = db.execute("SELECT j.*,b.actor FROM jobs j JOIN batches b ON b.id=j.batch WHERE j.rowid IN (SELECT max(rowid) FROM jobs WHERE status!='pending' GROUP BY target) AND j.checked_at<=? ORDER BY j.checked_at LIMIT 50", (now - 30,)).fetchall()
        def inspect(row):
            target = json.loads(row["target"])
            try:
                return self.call({"mode": "inspect", "target": target, "operationId": "hermes-sub:" + row["id"], "actor": row["actor"]})
            except Exception:
                return None
        # Only reads run concurrently. A slow grant must not delay external-activation notices.
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(inspect, row): row for row in rows}
            for future in as_completed(futures):
                row, result = futures[future], future.result()
                target = json.loads(row["target"])
                if result is None:
                    with self.store.connect() as db:
                        db.execute("UPDATE jobs SET checked_at=? WHERE id=?", (now, row["id"]))
                    continue
                current = fingerprint(result)
                if current != "inactive" and current != row["fingerprint"] and (row["fingerprint"] is not None or row["status"] == "failed"):
                    # Across requests for the same shop, one observed activation gets one notice.
                    self.store.notify("observed:" + result["identity"]["shopId"] + ":" + current, card("✅ 监测到店铺订阅激活已完成\n" + shop_line(target, result)))
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET fingerprint=?,checked_at=? WHERE id=?", (current, now, row["id"]))
                self.flush()


_gateway = None


def gateway():
    global _gateway
    if _gateway is None:
        _gateway = Gateway.from_environment()
    return _gateway


async def handle_message(adapter, data, text):
    """Called only by the authenticated SDK inbound path, before model routing."""
    if not owns_text(text):
        return False
    if os.getenv("HERMES_SUBSCRIPTIONS_ENABLED") != "true":
        return False
    event = read(data, "event")
    message = read(event, "message")
    if read(message, "chat_id") != CHAT_ID:
        return True
    try:
        await adapter._run_blocking(gateway().message, data, text)
    except ValueError as exc:
        gateway().store.notify("rejected:" + str(read(message, "message_id")), card(str(exc)))
    return True


def handle_card(data):
    if os.getenv("HERMES_SUBSCRIPTIONS_ENABLED") != "true":
        return "订阅入口尚未启用。"
    try:
        gateway().submit_form(data)
        return "已接收，将在本群反馈处理结果。"
    except (ValueError, TypeError) as exc:
        return str(exc)
    except Exception:
        return "请求暂未确认，请稍后重试此表单。"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--publish-entry", action="store_true")
    args = parser.parse_args()
    store = Store(os.environ["HERMES_SUBSCRIPTIONS_DB"])
    if args.publish_entry:
        store.notify("menu-entry", entry_card())
        raise SystemExit(0)
    # A single process owns work and notification delivery across restarts.
    import fcntl
    with open(store.path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(store)
        def monitor():
            while True:
                try:
                    worker.watch()
                    worker.flush()
                except Exception as exc:
                    print(f"subscription monitor: {type(exc).__name__}", flush=True)
                time.sleep(2)
        if not args.once:
            threading.Thread(target=monitor, daemon=True).start()
        while True:
            try:
                worker.tick(include_watch=args.once)
            except Exception as exc:
                print(f"subscription worker: {type(exc).__name__}", flush=True)
            if args.once:
                break
            time.sleep(2)
