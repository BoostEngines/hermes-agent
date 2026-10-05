import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from plugins.platforms.feishu import datahub_subscriptions as s

TEXT = "请开通 AutoBoost Max 1 年订阅\nShop Name：STEPLAB\nShop Code: USLC32EMHS\n邮箱：maria.garcia7104@zohomail.com"
TARGET = {"shopCode": "USLC32EMHS", "email": "maria.garcia7104@zohomail.com", "years": 1}


def event(now):
    return {"header": {"event_id": "event1", "event_type": "im.message.receive_v1", "tenant_key": "tenant", "app_id": "app", "create_time": str(int(now * 1e6))}, "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_developer"}}, "message": {"chat_id": s.CHAT_ID, "chat_type": "group", "message_type": "text", "message_id": "om_request", "create_time": str(int(now * 1000)), "content": json.dumps({"text": TEXT})}}}


def active(seat="seat1"):
    return {"subscriptionReady": True, "identity": {"shopId": "7496016493727025414", "shopCode": "USLC32EMHS", "shopName": "STEPLAB", "email": TARGET["email"], "uid": "owner"}, "subscription": {"id": seat, "currentPeriodEnd": "2027-10-02T09:00:00Z"}}


def inactive():
    return {"subscriptionReady": False, "identity": active()["identity"]}


class SubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1_800_000_000.0
        self.store = s.Store(Path(self.directory.name) / "tasks.db", now=lambda: self.now)
        self.now += 1
        self.gateway = s.Gateway(self.store, app_id="app", tenant="tenant", enabled=True, now=lambda: self.now)
        self.sent = []

    def send(self, payload, key):
        self.sent.append((key, payload))
        return "om_" + s.digest(key)

    def test_flexible_batches_and_reject_ambiguous_identity(self):
        self.assertEqual(s.parse_targets(TEXT), [TARGET])
        self.assertEqual(s.parse_targets(TEXT + "\n---\nShop Name: OTHER\n邮箱: second@example.com\nShop Code: USABCDEFGH")[1]["email"], "second@example.com")
        rows = s.parse_targets("开通 MAX 两年\nUSLC32EMHS,maria.garcia7104@zohomail.com\n7496016493727025415\tsecond@example.com")
        self.assertEqual([x["years"] for x in rows], [2, 2])
        self.assertEqual(rows[1]["shopId"], "7496016493727025415")
        for text in ["开通 MAX 4 年\nUSLC32EMHS a@example.com", "开通 MAX\nUSLC32EMHS a@example.com b@example.com", "开通 MAX\nUSLC32EMHS a@example.com\nUSLC32EMHS b@example.com", "开通 MAX\n> USLC32EMHS a@example.com"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                s.parse_targets(text)

    def test_linked_email_inline_labels_and_code_plus_id_are_one_shop(self):
        linked = TEXT.replace(TARGET["email"], f'[{TARGET["email"]}](mailto:{TARGET["email"]})')
        self.assertEqual(s.parse_targets(linked), [TARGET])
        inline = "激活下 ab 订阅： Shop Name: FIRMOFFICE Shop Code: USLC32EMHS 邮箱: maria.garcia7104@zohomail.com"
        self.assertEqual(s.parse_targets(inline), [TARGET])
        both = inline.replace(" 邮箱:", " Shop ID: 7496016493727025414 邮箱:")
        self.assertEqual(s.parse_targets(both), [{**TARGET, "shopId": "7496016493727025414"}])
        batch = inline + " Shop Name: ANOTHER Shop Code: USABCDEFGH 邮箱: second@example.com"
        self.assertEqual(len(s.parse_targets(batch)), 2)
        # Different addresses in a link remain ambiguous; never silently choose one.
        with self.assertRaises(ValueError):
            s.parse_targets(linked.replace("mailto:" + TARGET["email"], "mailto:other@example.com"))

    def test_local_parse_cli_requires_no_server_environment_or_state(self):
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, str(Path(s.__file__).resolve()), "--parse"], input=TEXT,
                                    cwd=cwd, env={}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"action": "activate", "targets": [TARGET]})
            self.assertEqual(list(Path(cwd).iterdir()), [])
            rejected = subprocess.run([sys.executable, str(Path(s.__file__).resolve()), "--parse"], input="不要开通 MAX",
                                      cwd=cwd, env={}, text=True, capture_output=True)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("error", json.loads(rejected.stdout))
            self.assertEqual(list(Path(cwd).iterdir()), [])

    def test_email_lists_and_indented_fields(self):
        message = "  请开通以下店铺的ab订阅：  \n qqq@123.com \n ee44@345.com\n aa33@313.com  "
        self.assertEqual(s.parse_message(message), {"action": "activate", "targets": [
            {"email": email, "years": 1} for email in ("qqq@123.com", "ee44@345.com", "aa33@313.com")]})
        for prefix in ("激活下 ab 订阅：", "请激活 ab订阅："):
            self.assertEqual(s.parse_message(f" {prefix} \n 邮箱：kevinhans xxx @gmail.com  ")["targets"], [{"email": "kevinhansxxx@gmail.com", "years": 1}])
        self.assertEqual(s.parse_targets("\n  邮箱： a@example.com \n Shop Code： USLC32EMHS \n Shop Name： NAME\n"), [{**TARGET, "email": "a@example.com"}])

    def test_preview_delivery_grace_restart_and_pinned_identity(self):
        self.store.enqueue("email-list", "ou_developer", [{"email": TARGET["email"], "years": 1}])
        calls = []
        def call(request):
            calls.append(copy.deepcopy(request))
            return active() if request["mode"] == "activate" else inactive()
        def unavailable(*_):
            raise TimeoutError()
        worker = s.Worker(self.store, call, unavailable)
        worker.tick()
        self.now += 600
        worker.tick()
        self.assertEqual([x["mode"] for x in calls], ["inspect"])
        worker.deliver = self.send
        worker.flush()
        self.assertIn("STEPLAB", json.dumps(self.sent))
        reopened = s.Store(self.store.path, now=lambda: self.now)
        worker = s.Worker(reopened, call, self.send)
        self.now += 59
        worker.tick()
        self.assertEqual(len(calls), 1)
        self.now += 1
        worker.tick()
        self.assertEqual([x["mode"] for x in calls], ["inspect", "inspect", "activate"])
        self.assertEqual(calls[-1]["target"]["shopId"], active()["identity"]["shopId"])
        self.assertEqual(calls[-1]["target"]["uid"], "owner")
        self.assertEqual(len([key for key, _ in self.sent if key.startswith("complete:")]), 1)

    def test_ambiguous_email_requests_code_without_retry_or_grant(self):
        self.store.enqueue("ambiguous", "ou_developer", [{"email": TARGET["email"], "years": 1}])
        calls = []
        def call(request):
            calls.append(request)
            raise RuntimeError("shop_code_required_multiple_matches")
        worker = s.Worker(self.store, call, self.send)
        worker.tick()
        self.now += 300
        worker.tick()
        self.assertEqual(len(calls), 1)
        self.assertIn("关联多家店铺", json.dumps(self.sent, ensure_ascii=False))
        self.assertNotIn(s.FLYNN_EMAIL, json.dumps(self.sent))

    def test_cancel_or_reply_prevents_delayed_writes_and_replay_is_harmless(self):
        self.gateway.message(event(self.now), TEXT)
        worker = s.Worker(self.store, lambda _: inactive(), self.send)
        worker.tick()
        reply = event(self.now)
        reply["event"]["message"].update(message_id="om_reply", parent_id="om_" + s.digest("preview:om_request"))
        self.gateway.message(reply, "这个店铺不对")
        self.now += 60
        worker.call = lambda _: self.fail("cancelled requests must not write or monitor")
        worker.tick()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "cancelled")
        cancel = event(self.now)
        cancel["event"]["message"]["message_id"] = "om_cancel"
        self.gateway.message(cancel, "取消激活")
        self.store.enqueue("later", "ou_developer", [TARGET])
        self.gateway.message(cancel, "取消激活")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE batch='later'").fetchone()[0], "resolving")

    def test_cancellation_during_inspect_is_not_overwritten(self):
        self.store.enqueue("race", "ou_developer", [TARGET])
        def call(request):
            self.assertEqual(request["mode"], "inspect")
            self.store.cancel("cancel-race", "ou_developer")
            return inactive()
        worker = s.Worker(self.store, call, self.send)
        worker.tick()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "cancelled")
        self.assertFalse(any(key.startswith("preview:") for key, _ in self.sent))

    def test_cancellation_during_final_inspect_prevents_claiming_grant(self):
        self.store.enqueue("race", "ou_developer", [TARGET])
        worker = s.Worker(self.store, lambda _: inactive(), self.send)
        worker.tick()
        self.now += 60
        def call(request):
            self.assertEqual(request["mode"], "inspect")
            self.store.cancel("cancel-race", "ou_developer")
            return inactive()
        worker.call = call
        worker.tick()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "cancelled")
            self.assertEqual(db.execute("SELECT count(*) FROM grants").fetchone()[0], 0)

    def test_changed_identity_after_preview_never_grants(self):
        self.store.enqueue("identity", "ou_developer", [{"email": TARGET["email"], "years": 1}])
        worker = s.Worker(self.store, lambda _: inactive(), self.send)
        worker.tick()
        def changed(request):
            self.assertEqual(request["mode"], "inspect")
            self.assertEqual(request["target"]["shopId"], active()["identity"]["shopId"])
            return {"subscriptionReady": False, "identity": {**inactive()["identity"], "shopId": "different"}}
        worker.call = changed
        self.now += 60
        worker.tick()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM grants").fetchone()[0], 0)

    def test_previewed_active_subscription_is_not_replaced_if_it_expires(self):
        self.store.enqueue("active", "ou_developer", [TARGET])
        worker = s.Worker(self.store, lambda _: active(), self.send)
        worker.tick()
        def expired(request):
            self.assertEqual(request["mode"], "inspect")
            return inactive()
        worker.call = expired
        self.now += 60
        worker.tick()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs").fetchone()[0], "needs_input")
            self.assertEqual(db.execute("SELECT count(*) FROM grants").fetchone()[0], 0)

    def test_bare_cancel_only_own_batches_and_reply_cancels_exact_batch(self):
        self.store.enqueue("other", "ou_other", [TARGET])
        self.store.enqueue("own", "ou_developer", [TARGET])
        self.store.cancel("first", "ou_developer")
        with self.store.connect() as db:
            self.assertEqual(dict(db.execute("SELECT batch,status FROM jobs").fetchall()), {"other": "resolving", "own": "cancelled"})
        reply = event(self.now)
        reply["event"]["message"].update(message_id="om_reply", parent_id="other")
        self.gateway.message(reply, "先等等")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE batch='other'").fetchone()[0], "cancelled")

    def test_only_explicit_commands(self):
        self.assertTrue(s.owns_text(TEXT))
        self.assertTrue(s.owns_text("请帮我给STEPLAB开通一年max"))
        self.assertTrue(s.owns_text("给以下店铺完成1年max激活"))
        self.assertTrue(s.owns_text("请给这些店铺完成一年 max 激活"))
        for text in ["账户订阅激活已完成", "不要开通 MAX", "如何开通订阅？", "示例：开通 MAX", "Shop Code: USLC32EMHS\n邮箱: a@example.com"]:
            self.assertFalse(s.owns_text(text), text)

    def test_event_boundary_and_durable_deduplication(self):
        original = event(self.now)
        paths = [("event.message.chat_id", "oc_wrong"), ("event.message.chat_type", "p2p"), ("event.sender.sender_type", "app"), ("header.tenant_key", "wrong"), ("header.app_id", "wrong"), ("header.event_type", "synthetic"), ("header.create_time", "1000"), ("event.message.create_time", "1000")]
        for path, value in paths:
            bad = copy.deepcopy(original)
            node = bad
            *parts, leaf = path.split(".")
            for part in parts:
                node = node[part]
            node[leaf] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.gateway.message(bad, TEXT)
        self.gateway.message(original, TEXT)
        self.gateway.message(original, TEXT)
        reopened = s.Store(self.store.path, now=lambda: self.now)
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_form_bound_to_issued_card_and_submit_once(self):
        self.gateway.message(event(self.now), "订阅激活菜单")
        worker = s.Worker(self.store, deliver=self.send)
        worker.flush()
        nonce = s.digest("om_request")
        data = event(self.now)
        data["header"]["event_type"] = "card.action.trigger"
        data["event"] = {"operator": {"open_id": "ou_other_developer"}, "context": {"open_chat_id": s.CHAT_ID, "open_message_id": "om_" + s.digest("form:" + nonce)}, "action": {"name": "datahub_subscription_" + nonce, "tag": "button", "form_value": {"years": "2", "email": TARGET["email"], "shop": TARGET["shopCode"]}}}
        self.gateway.submit_form(data)
        self.gateway.submit_form(data)
        with self.store.connect() as db:
            rows = db.execute("SELECT target FROM jobs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])["years"], 2)
        data["event"]["context"]["open_message_id"] = "om_copied_card"
        with self.assertRaises(ValueError):
            self.gateway.submit_form(data)

    def test_permanent_entry_opens_fresh_forms_without_granting(self):
        self.store.notify("menu-entry", s.entry_card())
        s.Worker(self.store, deliver=self.send).flush()
        data = event(self.now)
        data["header"]["event_type"] = "card.action.trigger"
        data["event"] = {"operator": {"open_id": "ou_developer"}, "context": {"open_chat_id": s.CHAT_ID, "open_message_id": "om_" + s.digest("menu-entry")}, "action": {"tag": "button", "value": {"datahub_subscription": "open"}}}
        self.gateway.submit_form(data)
        self.gateway.submit_form(data)
        data["header"]["event_id"] = "second-click"
        self.gateway.submit_form(data)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM forms").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
        data["event"]["context"]["open_message_id"] = "om_copy"
        with self.assertRaises(ValueError):
            self.gateway.submit_form(data)

    def test_already_active_does_not_grant(self):
        self.gateway.message(event(self.now), TEXT)
        calls = []
        def call(value):
            calls.append(value)
            return active()
        worker = s.Worker(self.store, call, self.send)
        worker.tick()
        self.now += 60
        worker.tick()
        self.assertEqual([x["mode"] for x in calls], ["inspect", "inspect"])
        self.assertEqual(len([x for x in self.sent if x[0].startswith("complete:")]), 1)

    def test_timeout_after_grant_reconciles_without_second_grant(self):
        self.gateway.message(event(self.now), TEXT)
        granted, calls = [], []
        def call(value):
            calls.append(value)
            if value["mode"] == "inspect":
                return active() if granted else inactive()
            granted.append(value)
            raise TimeoutError("response lost after successful grant")
        worker = s.Worker(self.store, call, self.send)
        worker.tick()
        self.now += 60
        worker.tick()
        self.now += 11
        worker.tick()
        self.assertEqual(len(granted), 1)
        self.assertTrue(granted[0]["allowCreate"])
        self.assertEqual(len([x for x in self.sent if x[0].startswith("complete:")]), 1)

    def test_retry_budget_mentions_flynn_and_external_activation_notifies_once(self):
        self.gateway.message(event(self.now), TEXT)
        def fail(value):
            raise TimeoutError()
        worker = s.Worker(self.store, fail, self.send)
        for seconds in [0, 11, 31]:
            self.now += seconds
            worker.tick()
        completed = [payload for key, payload in self.sent if key.startswith("complete:")]
        self.assertEqual(len(completed), 1)
        self.assertIn(s.FLYNN_EMAIL, json.dumps(completed[0]))
        worker.call = lambda _: active()
        worker.tick()
        self.now += 31
        worker.tick()
        self.assertEqual(len([key for key, _ in self.sent if key.startswith("observed:")]), 1)

    def test_watch_does_not_write_or_notify_historical_subscription(self):
        self.store.enqueue("watch1", "ou_developer", [TARGET], "watch")
        calls = []
        state = [active()]
        def call(value):
            calls.append(value)
            return state[0]
        worker = s.Worker(self.store, call, self.send)
        worker.tick()
        self.assertEqual(self.sent, [])
        state[0] = {"subscriptionReady": False}
        self.now += 31
        worker.tick()
        state[0] = active("seat2")
        self.now += 31
        worker.tick()
        self.assertTrue(all(x["mode"] == "inspect" for x in calls))
        self.assertEqual(len(self.sent), 1)

    def test_uncertain_grant_is_shared_across_different_messages_for_same_shop(self):
        self.store.enqueue("request1", "ou_developer", [TARGET])
        self.store.enqueue("request2", "ou_developer", [TARGET])
        grants = []
        def call(value):
            if value["mode"] == "inspect":
                return inactive()
            grants.append(value)
            raise TimeoutError()
        worker = s.Worker(self.store, call, self.send)
        worker.tick()
        worker.tick()
        self.now += 60
        worker.tick()
        worker.tick()
        self.assertEqual([x["allowCreate"] for x in grants], [True, False])
        self.assertEqual(grants[0]["operationId"], grants[1]["operationId"])

    def test_receipt_recovers_after_crash_between_verification_and_notification(self):
        self.store.enqueue("request1", "ou_developer", [TARGET])
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='success',result=?,fingerprint=?,checked_at=?", (json.dumps(active()), s.fingerprint(active()), self.now))
        worker = s.Worker(self.store, lambda _: self.fail("must not grant again"), self.send)
        worker.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0][0].startswith("complete:"))

    def test_batch_continues_after_one_shop_fails_and_only_summarizes_when_done(self):
        self.store.enqueue("batch", "ou_developer", [TARGET, {"shopCode": "USABCDEFGH", "email": "second@example.com", "years": 1}])
        def call(value):
            if value["target"]["shopCode"] == "USLC32EMHS":
                raise RuntimeError("identity_mismatch")
            return {**active(), "identity": {"shopId": "another", "shopCode": "USABCDEFGH", "shopName": "OTHER", "email": "second@example.com", "uid": "another-owner"}}
        worker = s.Worker(self.store, call, self.send)
        worker.tick()
        worker.tick()
        self.assertEqual(self.sent, [])
        self.now += 11
        worker.tick()
        self.now += 31
        worker.tick()
        self.now += 60
        worker.tick()
        completed = [payload for key, payload in self.sent if key.startswith("complete:")]
        self.assertEqual(len(completed), 1)
        rendered = json.dumps(completed[0], ensure_ascii=False)
        self.assertIn("OTHER", rendered)
        self.assertIn("identity_mismatch", rendered)
        self.assertIn(s.FLYNN_EMAIL, rendered)

    def test_unknown_notification_does_not_block_new_notices(self):
        self.store.notify("old", s.card("old"))
        with self.store.connect() as db:
            db.execute("UPDATE outbox SET attempted_at=?", (self.now - 3601,))
        self.store.notify("new", s.card("new"))
        s.Worker(self.store, deliver=self.send).flush()
        self.assertEqual([key for key, _ in self.sent], ["new"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM outbox WHERE id='old'").fetchone()[0], "unknown")


if __name__ == "__main__":
    unittest.main()
