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
    / "datahub_verified_approval.py"
)
SPEC = importlib.util.spec_from_file_location(
    "datahub_verified_approval",
    MODULE_PATH,
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ACTION_ID = "1f55f530-7115-4fe8-8be9-1f8099be697d"
CHALLENGE = f"v1.{'a' * 32}.{'b' * 32}"
NOW = 1_775_000_000.0


def action(**overrides):
    value = {
        "schemaVersion": MODULE.ACTION_SCHEMA,
        "actionId": ACTION_ID,
        "challengeRevision": 3,
        "decision": "approve",
        "challenge": CHALLENGE,
    }
    value.update(overrides)
    return value


def event(**overrides):
    header = types.SimpleNamespace(
        event_id="event-7",
        event_type="card.action.trigger",
        tenant_key="tenant-7",
        # Feishu P2 callback headers use microseconds, for example
        # create_time="1603977298000000" in the official card callback docs.
        create_time=str(int(NOW * 1_000_000)),
    )
    operator = types.SimpleNamespace(open_id="operator-7")
    context = types.SimpleNamespace(
        open_chat_id="chat-7",
        open_message_id="message-7",
    )
    payload = types.SimpleNamespace(
        header=header,
        event=types.SimpleNamespace(
            operator=operator,
            context=context,
            token="delayed-update-token-must-not-leave-gateway",
        ),
    )
    for name, value in overrides.items():
        setattr(payload, name, value)
    return payload


def receipt(**overrides):
    value = {
        "schemaVersion": MODULE.RECEIPT_SCHEMA,
        "actionId": ACTION_ID,
        "decision": "approve",
        "approvalState": "approved",
        "executionState": "pending",
        "overallState": "ready",
        "decisionEventId": "decision-event-7",
        "externalEventKey": "feishu:cli_app_7:event-7",
        "planHash": "c" * 64,
        "targetVersion": "target-version-7",
    }
    value.update(overrides)
    return value


def handler(transport, **overrides):
    options = {
        "enabled": True,
        "app_id": "cli_app_7",
        "tenant_key": "tenant-7",
        "connection_mode": "websocket",
        "base_url": "https://datahub.example.invalid/api/ops",
        "gateway_token": "gateway-token-7",
        "callback_timeout_ms": 2400,
        "event_max_age_ms": 300000,
        "transport": transport,
        "wall_clock": lambda: NOW,
        "monotonic_clock": lambda: 100.0,
    }
    options.update(overrides)
    return MODULE.DataHubVerifiedApprovalHandler(**options)


class NamespaceOwnershipTest(unittest.TestCase):
    def test_owns_current_unknown_and_json_encoded_datahub_versions(self):
        self.assertTrue(MODULE.owns_datahub_action_value(action()))
        self.assertTrue(
            MODULE.owns_datahub_action_value(
                action(schemaVersion="datahub.ops.approval-card-action.v99")
            )
        )
        self.assertTrue(MODULE.owns_datahub_action_value(json.dumps(action())))
        self.assertFalse(
            MODULE.owns_datahub_action_value({"hermes_action": "approve"})
        )

    def test_disabled_and_unknown_versions_fail_without_falling_through(self):
        calls = []
        disabled = handler(
            lambda *args: calls.append(args),
            enabled=False,
        )
        self.assertEqual(disabled.handle(event(), action()).state, "rejected")
        self.assertEqual(
            disabled.handle(
                event(),
                action(schemaVersion="datahub.ops.approval-card-action.v99"),
            ).state,
            "rejected",
        )
        self.assertEqual(calls, [])


class VerifiedNormalizationTest(unittest.TestCase):
    def test_normalizes_exact_p2_context_and_excludes_update_token(self):
        calls = []

        def transport(url, token, body, timeout):
            calls.append((url, token, body, timeout))
            return receipt()

        result = handler(transport).handle(event(), action())

        self.assertEqual(result.state, "committed")
        self.assertEqual(
            calls[0][0],
            "https://datahub.example.invalid/api/ops/v1/actions/"
            f"{ACTION_ID}/decision",
        )
        self.assertEqual(calls[0][1], "gateway-token-7")
        self.assertLessEqual(calls[0][3], 1.201)
        body = calls[0][2]
        self.assertNotIn("actionId", body)
        self.assertEqual(body["schemaVersion"], MODULE.DECISION_SCHEMA)
        self.assertEqual(
            body["event"],
            {
                "schemaVersion": MODULE.VERIFIED_EVENT_SCHEMA,
                "source": "feishu",
                "transport": "websocket",
                "verified": True,
                "appId": "cli_app_7",
                "tenantKey": "tenant-7",
                "eventId": "event-7",
                "eventType": "card.action.trigger",
                "principalOpenId": "operator-7",
                "chatId": "chat-7",
                "messageId": "message-7",
                "occurredAt": MODULE._iso_utc(NOW),
                "receivedAt": MODULE._iso_utc(NOW),
                "rawEventDigest": body["event"]["rawEventDigest"],
            },
        )
        self.assertNotIn(
            "delayed-update-token",
            json.dumps(body, sort_keys=True),
        )

    def test_normalizes_numeric_timestamp_precisions_and_rejects_overflow(self):
        expected = MODULE._iso_utc(NOW)
        for multiplier in (1, 1_000, 1_000_000, 1_000_000_000):
            timestamp, normalized = MODULE._parse_occurred_at(
                str(int(NOW * multiplier))
            )
            self.assertEqual(timestamp, NOW)
            self.assertEqual(normalized, expected)
        with self.assertRaises(MODULE.ApprovalBoundaryError):
            MODULE._parse_occurred_at("9" * 40)

    def test_missing_operator_wrong_tenant_and_expired_event_fail_closed(self):
        calls = []
        transport = lambda *args: calls.append(args)
        missing_operator = event()
        missing_operator.event.operator = None
        self.assertEqual(
            handler(transport).handle(missing_operator, action()).state,
            "rejected",
        )

        wrong_tenant = event()
        wrong_tenant.header.tenant_key = "other-tenant"
        self.assertEqual(
            handler(transport).handle(wrong_tenant, action()).state,
            "rejected",
        )

        expired = event()
        expired.header.create_time = str(int((NOW - 301) * 1_000))
        self.assertEqual(
            handler(transport).handle(expired, action()).state,
            "rejected",
        )
        self.assertEqual(calls, [])

    def test_requires_websocket_and_exact_action_shape(self):
        calls = []
        transport = lambda *args: calls.append(args)
        self.assertEqual(
            handler(transport, connection_mode="webhook").handle(
                event(), action()
            ).state,
            "rejected",
        )
        self.assertEqual(
            handler(transport).handle(
                event(),
                action(untrustedPrincipal="attacker"),
            ).state,
            "rejected",
        )
        self.assertEqual(calls, [])


class DecisionTransportTest(unittest.TestCase):
    def test_retries_uncertain_transport_once_with_identical_request(self):
        calls = []

        def transport(url, token, body, timeout):
            calls.append((url, token, body, timeout))
            if len(calls) == 1:
                raise MODULE.DecisionTransportError(None, uncertain=True)
            return receipt()

        result = handler(transport).handle(event(), action())

        self.assertEqual(result.state, "committed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0:3], calls[1][0:3])

    def test_definitive_deny_is_not_retried(self):
        calls = []

        def transport(*args):
            calls.append(args)
            raise MODULE.DecisionTransportError(403, uncertain=False)

        self.assertEqual(
            handler(transport).handle(event(), action()).state,
            "rejected",
        )
        self.assertEqual(len(calls), 1)

    def test_uncertain_result_never_claims_success(self):
        calls = []

        def transport(*args):
            calls.append(args)
            raise MODULE.DecisionTransportError(None, uncertain=True)

        result = handler(transport).handle(event(), action())

        self.assertEqual(result.state, "uncertain")
        self.assertEqual(len(calls), 2)
        self.assertIn("确认中", result.card["header"]["title"]["content"])
        self.assertNotIn("已记录", json.dumps(result.card, ensure_ascii=False))


class ForkPatchContractTest(unittest.TestCase):
    def test_patch_intercepts_before_hermes_and_generic_model_paths(self):
        patch = (
            ROOT
            / "patches"
            / "hermes-agent-v2026.7.20-datahub-verified-approval.patch"
        ).read_text()
        self.assertIn(
            "Base-Commit: 3ef6bbd201263d354fd83ec55b3c306ded2eb72a",
            patch,
        )
        intercept = patch.index("+        if owns_datahub_action_value(action_value):")
        original_extraction = patch.index(
            "-        event = getattr(data, \"event\", None)"
        )
        self.assertLess(intercept, original_extraction)
        self.assertIn(
            "return self._handle_datahub_verified_approval(data, action_value)",
            patch,
        )


if __name__ == "__main__":
    unittest.main()
