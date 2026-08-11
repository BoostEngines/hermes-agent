import importlib.util
import json
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "plugins"
    / "platforms"
    / "feishu"
    / "datahub_login_recovery_conversation.py"
)
SPEC = importlib.util.spec_from_file_location(
    "datahub_login_recovery_conversation",
    MODULE_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ADAPTER_PATCH = (
    ROOT
    / "patches"
    / "hermes-agent-v2026.7.20-datahub-verified-approval.patch"
)

NOW = 1_775_000_000.0


def message_event(*, text="再试一次", parent_id="om_parent_7", root_id="om_root_7"):
    return types.SimpleNamespace(
        header=types.SimpleNamespace(
            event_id="message-event-7",
            event_type="im.message.receive_v1",
            tenant_key="tenant-7",
            create_time=str(int(NOW * 1_000_000)),
        ),
        event=types.SimpleNamespace(
            sender=types.SimpleNamespace(
                sender_id=types.SimpleNamespace(open_id="operator_7")
            ),
            message=types.SimpleNamespace(
                chat_id="oc_chat_7",
                message_id="om_message_7",
                parent_id=parent_id,
                root_id=root_id,
                content=json.dumps({"text": text}, ensure_ascii=False),
            ),
        ),
    )


class RecordingAdapter:
    def __init__(self, receipt_factory=None):
        self.calls = []
        self.receipt_factory = receipt_factory or self._receipt

    def submit(self, body):
        self.calls.append(body)
        return self.receipt_factory(body)

    @staticmethod
    def _receipt(body):
        event = body["event"]
        return {
            "schemaVersion": MODULE.LOGIN_RECOVERY_CONVERSATION_RECEIPT_SCHEMA,
            "idempotencyKey": body["idempotencyKey"],
            "state": (
                "explained"
                if body["intent"].startswith("explain_")
                else "not_implemented"
            ),
            "eventId": event["eventId"],
            "principal": {
                "principalId": "operator-7",
                "environment": "production",
                "mappingRevision": "r7",
            },
            "intentReceipt": {
                "intent": body["intent"],
                "accepted": body["intent"].startswith("explain_"),
                "execution": "not_started",
                "code": "read_only"
                if body["intent"].startswith("explain_")
                else "not_implemented",
                "message": "DataHub 已记录这条受控请求。",
            },
            "view": {
                "title": "🟠 需要协助完成登录",
                "detail": "DataHub 会先重新读取该设备的最新状态。",
                "failureReason": "本轮登录出现验证码挑战，尚未完成验证。",
                "automationExplanation": "自动流程没有绕过当前人工验证步骤。",
                "nextStep": "请在本话题查看后续进度。",
                "owner": "Hermes",
                "findingVersion": 7,
                "firstSeenAt": "2026-08-10T00:00:00.000Z",
                "retryAfter": None,
            },
        }


def handler(adapter, **overrides):
    options = {
        "enabled": True,
        "app_id": "cli_app_7",
        "tenant_key": "tenant-7",
        "connection_mode": "websocket",
        "adapter": adapter,
        "event_max_age_ms": 300000,
        "wall_clock": lambda: NOW,
    }
    options.update(overrides)
    return MODULE.DataHubLoginRecoveryConversationHandler(**options)


class CanonicalIntentTest(unittest.TestCase):
    def test_parses_only_bounded_canonical_phrases(self):
        expected = {
            "现在什么问题？": "explain_current_failure",
            "失败原因": "explain_current_failure",
            "好了么": "explain_current_failure",
            "登录成功了吗": "explain_current_failure",
            "为什么没有自动处理": "explain_automation",
            "再试一次": "request_retry",
            "请重新登录": "request_relogin",
            "我本地登录好了": "confirm_local_login",
            "CAPTCHA 已完成": "confirm_captcha_complete",
        }
        for text, intent in expected.items():
            self.assertEqual(MODULE.canonical_login_recovery_intent(text).name, intent)
        self.assertIsNone(MODULE.canonical_login_recovery_intent("登录 95 号设备，密码是 x"))
        self.assertIsNone(MODULE.canonical_login_recovery_intent("帮我看看这个问题"))

    def test_claims_canonical_rootless_writes_before_model_routing(self):
        self.assertTrue(
            MODULE.looks_like_datahub_login_recovery_command(
                "再试一次", has_reply_context=False
            )
        )
        self.assertFalse(
            MODULE.looks_like_datahub_login_recovery_command(
                "帮我登录", has_reply_context=True
            )
        )

    def test_parses_device_ordinal_and_profile_without_forwarding_free_text(self):
        intent = MODULE.canonical_login_recovery_intent(
            "95 号设备 k1c89u03 请重新登录"
        )

        self.assertIsNotNone(intent)
        self.assertEqual(intent.name, "request_relogin")
        self.assertEqual(intent.target.type, "profile")
        self.assertEqual(intent.target.id, "k1c89u03")
        self.assertTrue(
            MODULE.looks_like_datahub_login_recovery_command(
                "95 号设备 k1c89u03 请重新登录",
                has_reply_context=False,
            )
        )
        self.assertIsNone(
            MODULE.canonical_login_recovery_intent(
                "95 号设备 k1c89u03 请重新登录，密码是 x"
            )
        )


class LoginRecoveryConversationHandlerTest(unittest.TestCase):
    def test_accepts_the_actual_server_receipt_shape_without_top_level_intent(self):
        adapter = RecordingAdapter()
        result = handler(adapter).handle_conversation(message_event(), "再试一次")

        receipt = RecordingAdapter._receipt(adapter.calls[0])
        self.assertNotIn("intent", receipt)
        self.assertEqual(receipt["intentReceipt"]["intent"], "request_retry")
        self.assertEqual(receipt["view"]["owner"], "Hermes")
        self.assertEqual(result.state, "not_implemented")

    def test_retry_uses_controlled_schema_without_credentials_or_direct_login(self):
        adapter = RecordingAdapter()
        result = handler(adapter).handle_conversation(message_event(), "再试一次")

        self.assertEqual(result.state, "not_implemented")
        self.assertIn("需要协助完成登录", result.reply_text)
        self.assertEqual(len(adapter.calls), 1)
        body = adapter.calls[0]
        self.assertEqual(body["schemaVersion"], MODULE.LOGIN_RECOVERY_CONVERSATION_SCHEMA)
        self.assertEqual(body["intent"], "request_retry")
        self.assertEqual(body["idempotencyKey"], "message-event-7")
        self.assertNotIn("incidentId", body)
        self.assertEqual(
            body["controlRequirements"],
            {
                "principalBinding": "exact",
                "conversationBinding": "topic_exact",
                "incidentState": "current_cookie_invalid",
                "eventFreshness": "verified_event_age",
                "idempotency": "gateway_event_key",
            },
        )
        self.assertEqual(body["event"]["threadId"], "om_root_7")
        self.assertEqual(body["event"]["rootMessageId"], "om_root_7")
        self.assertNotIn("schemaVersion", body["event"])
        serialized = json.dumps(body).lower()
        self.assertNotIn("password", serialized)
        self.assertNotIn('"cookies"', serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("loginjob", serialized)
        self.assertNotIn("relogin_device", serialized)
        self.assertNotIn("再试一次", json.dumps(body, ensure_ascii=False))
        self.assertIn("not_started", result.reply_text)

    def test_profile_qualified_retry_can_start_from_a_status_topic(self):
        adapter = RecordingAdapter()
        data = message_event(parent_id=None, root_id=None)
        result = handler(adapter).handle_conversation(
            data, "k1bxfpa8 请重新登录"
        )

        self.assertEqual(result.state, "not_implemented")
        self.assertEqual(len(adapter.calls), 1)
        body = adapter.calls[0]
        self.assertEqual(body["intent"], "request_relogin")
        self.assertEqual(body["target"], {"type": "profile", "id": "k1bxfpa8"})
        self.assertEqual(body["controlRequirements"]["conversationBinding"], "target_exact")
        self.assertEqual(body["event"]["rootMessageId"], "om_message_7")

    def test_queued_reassessment_is_explained_without_claiming_a_login_job(self):
        def queued_receipt(body):
            receipt = RecordingAdapter._receipt(body)
            receipt["state"] = "reassessment_queued"
            receipt["intentReceipt"] = {
                "intent": body["intent"],
                "accepted": True,
                "execution": "reassessment_queued",
                "code": "reassessment_queued",
                "message": "已安排重新读取当前设备和 Cookie 状态。",
            }
            return receipt

        result = handler(RecordingAdapter(queued_receipt)).handle_conversation(
            message_event(), "再试一次"
        )

        self.assertEqual(result.state, "reassessment_queued")
        self.assertIn("已安排重新评估", result.reply_text)
        self.assertNotIn("Login Job 已启动", result.reply_text)

    def test_action_queued_is_reported_as_direct_device_login(self):
        def action_receipt(body):
            receipt = RecordingAdapter._receipt(body)
            receipt["state"] = "action_queued"
            receipt["intentReceipt"] = {
                "intent": body["intent"],
                "accepted": True,
                "execution": "action_queued",
                "code": "action_queued",
                "message": "已发布 relogin_device（Action action-7）。",
            }
            return receipt

        result = handler(RecordingAdapter(action_receipt)).handle_conversation(
            message_event(), "请重新登录"
        )

        self.assertEqual(result.state, "action_queued")
        self.assertIn("已发布 relogin_device", result.reply_text)
        self.assertNotIn("需要人工步骤", result.reply_text)

    def test_local_login_and_captcha_are_evidence_not_direct_login(self):
        for command, expected in (
            ("我本地登录好了", "confirm_local_login"),
            ("CAPTCHA 已完成", "confirm_captcha_complete"),
        ):
            adapter = RecordingAdapter()
            result = handler(adapter).handle_conversation(message_event(text=command), command)
            self.assertEqual(result.state, "not_implemented")
            self.assertEqual(adapter.calls[0]["intent"], expected)

    def test_read_intents_do_not_create_a_mutation_request(self):
        adapter = RecordingAdapter()
        result = handler(adapter).handle_conversation(
            message_event(text="现在什么问题"), "现在什么问题"
        )
        self.assertEqual(result.state, "explained")
        self.assertNotIn("requestedMutation", adapter.calls[0])
        self.assertIn("验证码挑战", result.reply_text)

        automation = handler(RecordingAdapter()).handle_conversation(
            message_event(text="为什么没有自动处理"), "为什么没有自动处理"
        )
        self.assertEqual(automation.state, "explained")
        self.assertIn("没有绕过当前人工验证", automation.reply_text)

    def test_rootless_message_is_rejected_before_gateway_call(self):
        adapter = RecordingAdapter()
        result = handler(adapter).handle_conversation(
            message_event(parent_id=None, root_id=None), "再试一次"
        )
        self.assertEqual(result.state, "rejected")
        self.assertIn("登录恢复话题", result.reply_text)
        self.assertEqual(adapter.calls, [])

    def test_targeted_rootless_message_is_allowed(self):
        adapter = RecordingAdapter()
        result = handler(adapter).handle_conversation(
            message_event(parent_id=None, root_id=None),
            "k1bxfpa8 请重新登录",
        )
        self.assertEqual(result.state, "not_implemented")
        self.assertEqual(adapter.calls[0]["target"]["id"], "k1bxfpa8")

    def test_parent_only_message_is_rejected_before_gateway_call(self):
        adapter = RecordingAdapter()
        result = handler(adapter).handle_conversation(
            message_event(parent_id="om_parent_7", root_id=None), "再试一次"
        )
        self.assertEqual(result.state, "rejected")
        self.assertEqual(adapter.calls, [])

    def test_stale_event_is_rejected_before_gateway_call(self):
        adapter = RecordingAdapter()
        stale = message_event()
        stale.header.create_time = str(int((NOW - 301) * 1_000_000))
        result = handler(adapter).handle_conversation(stale, "再试一次")
        self.assertEqual(result.state, "rejected")
        self.assertEqual(adapter.calls, [])

    def test_unbound_receipt_is_uncertain_and_never_claims_recovery(self):
        def mismatched_receipt(body):
            receipt = RecordingAdapter._receipt(body)
            receipt["idempotencyKey"] = "other-event"
            return receipt

        result = handler(RecordingAdapter(mismatched_receipt)).handle_conversation(
            message_event(), "再试一次"
        )
        self.assertEqual(result.state, "uncertain")
        self.assertIn("没有重复触发登录", result.reply_text)

    def test_receipt_event_must_match_the_verified_inbound_event(self):
        def replayed_receipt(body):
            receipt = RecordingAdapter._receipt(body)
            receipt["eventId"] = "other-event"
            receipt["idempotencyKey"] = "other-event"
            return receipt

        result = handler(RecordingAdapter(replayed_receipt)).handle_conversation(
            message_event(), "再试一次"
        )
        self.assertEqual(result.state, "uncertain")

    def test_missing_gateway_configuration_fails_closed(self):
        result = handler(None).handle_conversation(message_event(), "再试一次")
        self.assertEqual(result.state, "rejected")
        self.assertIn("未执行", result.reply_text)


class GatewayAdapterTest(unittest.TestCase):
    def test_uses_one_configured_path_and_versioned_header(self):
        calls = []

        def transport(url, token, body, timeout, contract):
            calls.append((url, token, body, timeout, contract))
            return {"ok": True}

        adapter = MODULE.LoginRecoveryConversationGatewayAdapter(
            base_url="https://datahub.example.invalid/api/ops",
            gateway_token="gateway-token-7",
            timeout_seconds=2.4,
            transport=transport,
        )
        adapter.submit({"schemaVersion": MODULE.LOGIN_RECOVERY_CONVERSATION_SCHEMA})

        self.assertEqual(
            calls[0][0],
            "https://datahub.example.invalid/api/ops/v1/relogin-conversations/receipts",
        )
        self.assertEqual(calls[0][4], MODULE.LOGIN_RECOVERY_CONVERSATION_SCHEMA)

    def test_rejects_ambiguous_endpoint_path(self):
        with self.assertRaises(MODULE.LoginRecoveryConversationError):
            MODULE._validate_endpoint_path("https://attacker.invalid/anything")


class PinnedAdapterWiringTest(unittest.TestCase):
    def test_patch_installs_and_intercepts_only_canonical_login_recovery_intents(self):
        patch = ADAPTER_PATCH.read_text(encoding="utf-8")
        self.assertIn("DataHubLoginRecoveryConversationHandler", patch)
        self.assertIn("looks_like_datahub_login_recovery_command", patch)
        self.assertIn("_handle_datahub_login_recovery_conversation", patch)
        self.assertIn("await self._handle_datahub_login_recovery_conversation", patch)


if __name__ == "__main__":
    unittest.main()
