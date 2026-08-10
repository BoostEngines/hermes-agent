"""Verified DataHub approval callback boundary for the pinned Feishu adapter.

This module is copied into the reviewed Hermes fork source tree. It is not a
runtime monkey patch. DataHub-namespaced actions are always owned here so a
malformed or disabled approval callback can never fall through to a synthetic
command or model turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


ACTION_SCHEMA = "datahub.ops.approval-card-action.v1"
ACTION_NAMESPACE = "datahub.ops.approval-card-action."
VERIFIED_EVENT_SCHEMA = "datahub.ops.verified-feishu-card-action.v1"
DECISION_SCHEMA = "datahub.ops.feishu-action-decision.v1"
RECEIPT_SCHEMA = "datahub.ops.feishu-action-decision-receipt.v1"
CONVERSATION_SCHEMA = "datahub.ops.feishu-conversation-decision.v1"
CONVERSATION_EVENT_SCHEMA = "datahub.ops.verified-feishu-message.v1"
CONVERSATION_RECEIPT_SCHEMA = (
    "datahub.ops.feishu-conversation-decision-receipt.v1"
)
DECISIONS = frozenset({"approve", "reject", "cancel"})
_CHALLENGE_RE = re.compile(r"^v1\.[A-Za-z0-9_-]{32}\.[A-Za-z0-9_-]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PULL_REQUEST_TARGET_RE = re.compile(
    r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$"
)
_ACTION_FIELDS = frozenset(
    {"schemaVersion", "actionId", "challengeRevision", "decision", "challenge"}
)


class ApprovalBoundaryError(Exception):
    """A fail-closed callback validation or RPC result."""

    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class DecisionTransportError(ApprovalBoundaryError):
    def __init__(self, status: int | None, *, uncertain: bool):
        super().__init__("decision_transport_failed", uncertain=uncertain)
        self.status = status


@dataclass(frozen=True)
class ApprovalCallbackResult:
    state: str
    card: Mapping[str, Any]
    reply_text: str


@dataclass(frozen=True)
class ConversationApprovalResult:
    state: str
    reply_text: str


DecisionTransport = Callable[
    [str, str, Mapping[str, Any], float],
    Mapping[str, Any],
]


def _read(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required_text(value: Any, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512:
        raise ApprovalBoundaryError(f"missing_{name}")
    return normalized


def _pull_request_url(receipt: Mapping[str, Any]) -> str | None:
    target_id = str(receipt.get("targetId") or "").strip()
    match = _PULL_REQUEST_TARGET_RE.fullmatch(target_id)
    if match is None:
        return None
    expected = f"https://github.com/{match.group(1)}/pull/{match.group(2)}"
    supplied = str(receipt.get("targetUrl") or "").strip()
    return expected if not supplied or supplied == expected else None


def _parse_action_value(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and len(value) <= 16_384:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def owns_datahub_action_value(value: Any) -> bool:
    """Own the entire DataHub namespace, including unknown future versions."""

    decoded = _parse_action_value(value)
    schema = decoded.get("schemaVersion") if decoded else None
    return isinstance(schema, str) and schema.startswith(ACTION_NAMESPACE)


def _normalized_command_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


_REPLY_COMMAND_RE = re.compile(
    r"^(?:(?:请|麻烦)?(?:帮我)?(?:同意|批准|确认)?(?:合入|合并)(?:这个|该)?(?:\s*pr)?|同意|批准|确认|可以合入|拒绝|不同意)[。！!？?]?$",
    re.IGNORECASE,
)
_EXPLICIT_COMMAND_RE = re.compile(
    r"^(?:请|麻烦)?(?:帮我)?(?:(?:同意|批准|确认)?(?:合入|合并)(?:(?:所有|全部)(?:待审批的?)?\s*(?:pr|pull requests?)|\s*(?:pr\s*)?#?\d+)|拒绝\s*(?:pr\s*)?#?\d+)(?:\s*(?:吧|一下))?[。！!？?]?$",
    re.IGNORECASE,
)


def looks_like_datahub_approval_command(
    value: Any,
    *,
    has_reply_context: bool,
) -> bool:
    """Claim only explicit approval language; ordinary chat still reaches Hermes."""

    text = _normalized_command_text(value)
    if not text or len(text) > 256:
        return False
    return bool(
        _EXPLICIT_COMMAND_RE.fullmatch(text)
        or (has_reply_context and _REPLY_COMMAND_RE.fullmatch(text))
    )


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_occurred_at(value: Any) -> tuple[float, str]:
    text = _required_text(value, "occurred_at")
    if text.isdigit():
        numeric = float(text)
        if numeric >= 100_000_000_000_000_000:
            timestamp = numeric / 1_000_000_000
        elif numeric >= 100_000_000_000_000:
            timestamp = numeric / 1_000_000
        elif numeric >= 100_000_000_000:
            timestamp = numeric / 1_000
        else:
            timestamp = numeric
        try:
            return timestamp, _iso_utc(timestamp)
        except (OverflowError, OSError, ValueError) as exc:
            raise ApprovalBoundaryError("invalid_occurred_at") from exc
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApprovalBoundaryError("invalid_occurred_at") from exc
    if parsed.tzinfo is None:
        raise ApprovalBoundaryError("invalid_occurred_at")
    timestamp = parsed.timestamp()
    return timestamp, _iso_utc(timestamp)


def _bounded_int(
    value: str | None,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _validate_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    is_loopback_http = (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    )
    if not normalized or (parsed.scheme != "https" and not is_loopback_http):
        raise ApprovalBoundaryError("invalid_base_url")
    if parsed.username or parsed.password or not parsed.netloc:
        raise ApprovalBoundaryError("invalid_base_url")
    return normalized


def _default_transport(
    url: str,
    token: str,
    body: Mapping[str, Any],
    timeout_seconds: float,
) -> Mapping[str, Any]:
    request = Request(
        url,
        data=json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-DataHub-Ops-Contract-Version": DECISION_SCHEMA,
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status < 200 or response.status >= 300:
                raise DecisionTransportError(
                    response.status,
                    uncertain=response.status >= 500,
                )
            encoded = response.read(1_048_577)
            if len(encoded) > 1_048_576:
                raise DecisionTransportError(None, uncertain=True)
            payload = json.loads(encoded)
    except HTTPError as exc:
        raise DecisionTransportError(
            exc.code,
            uncertain=exc.code >= 500,
        ) from exc
    except (TimeoutError, URLError, OSError, ValueError) as exc:
        raise DecisionTransportError(None, uncertain=True) from exc
    if not isinstance(payload, Mapping):
        raise DecisionTransportError(None, uncertain=True)
    return payload


class DataHubVerifiedApprovalHandler:
    """Normalize one SDK-verified P2 callback and invoke DataHub directly."""

    def __init__(
        self,
        *,
        enabled: bool,
        app_id: str,
        tenant_key: str,
        connection_mode: str,
        base_url: str,
        gateway_token: str,
        callback_timeout_ms: int = 2_400,
        event_max_age_ms: int = 300_000,
        transport: DecisionTransport = _default_transport,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ):
        self._enabled = enabled
        self._app_id = app_id.strip()
        self._tenant_key = tenant_key.strip()
        self._connection_mode = connection_mode.strip().lower()
        self._base_url = base_url.strip()
        self._gateway_token = gateway_token.strip()
        self._callback_timeout_seconds = callback_timeout_ms / 1_000
        self._event_max_age_seconds = event_max_age_ms / 1_000
        self._transport = transport
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

    @classmethod
    def from_environment(cls) -> "DataHubVerifiedApprovalHandler":
        return cls(
            enabled=os.getenv("FEISHU_VERIFIED_APPROVAL_ENABLED", "false").lower()
            == "true",
            app_id=os.getenv("FEISHU_APP_ID", ""),
            tenant_key=os.getenv("FEISHU_TENANT_KEY", ""),
            connection_mode=os.getenv("FEISHU_CONNECTION_MODE", ""),
            base_url=os.getenv("DATAHUB_OPS_BASE_URL", ""),
            gateway_token=os.getenv("DATAHUB_OPS_GATEWAY_TOKEN", ""),
            callback_timeout_ms=_bounded_int(
                os.getenv("DATAHUB_OPS_APPROVAL_CALLBACK_TIMEOUT_MS"),
                2_400,
                100,
                2_800,
            ),
            event_max_age_ms=_bounded_int(
                os.getenv("DATAHUB_OPS_APPROVAL_EVENT_MAX_AGE_MS"),
                300_000,
                30_000,
                900_000,
            ),
        )

    def handle(self, data: Any, action_value: Any) -> ApprovalCallbackResult:
        try:
            request = self._build_request(data, action_value)
            receipt = self._decide(request)
            pull_request_url = _pull_request_url(receipt)
            reply_lines = [
                "✅ 审批已记录",
                f"Action: {receipt['actionId']}",
                f"当前状态: {receipt['overallState']}",
            ]
            if pull_request_url:
                reply_lines.append(f"PR: {pull_request_url}")
            reply_lines.append("Hermes 正在继续合入、部署并验证业务恢复。")
            return ApprovalCallbackResult(
                state="committed",
                card=_status_card(
                    title="DataHub 审批已记录",
                    template="green",
                    message="决定已提交到 DataHub；领域动作尚未由此回调执行。",
                    action_id=str(receipt["actionId"]),
                    state=str(receipt["overallState"]),
                ),
                reply_text="\n".join(reply_lines),
            )
        except ApprovalBoundaryError as exc:
            if exc.uncertain:
                return ApprovalCallbackResult(
                    state="uncertain",
                    card=_status_card(
                        title="DataHub 审批确认中",
                        template="orange",
                        message="结果暂不确定，请稍后重试或查询 Action 当前状态。",
                    ),
                    reply_text=(
                        "⏳ 审批结果暂不确定；原审批卡和 Action 状态均未被视觉结果替代。"
                        "Hermes 将以 DataHub 的实际状态为准。"
                    ),
                )
            return ApprovalCallbackResult(
                state="rejected",
                card=_status_card(
                    title="DataHub 审批未记录",
                    template="red",
                    message="回调校验失败或审批不可用；Action 保持原状态。",
                ),
                reply_text="❌ 审批未记录；回调校验失败或当前审批已不可用。",
            )
        except Exception:
            return ApprovalCallbackResult(
                state="uncertain",
                card=_status_card(
                    title="DataHub 审批确认中",
                    template="orange",
                    message="结果暂不确定，请稍后重试或查询 Action 当前状态。",
                ),
                reply_text=(
                    "⏳ 审批结果暂不确定；原审批卡和 Action 状态均未被视觉结果替代。"
                    "Hermes 将以 DataHub 的实际状态为准。"
                ),
            )

    def handle_conversation(
        self,
        data: Any,
        command_text: str,
    ) -> ConversationApprovalResult:
        """Submit one SDK-verified, explicitly parsed Feishu approval command."""

        try:
            request = self._build_conversation_request(data, command_text)
            receipt = self._decide_conversation(request)
            accepted = int(receipt.get("acceptedCount") or 0)
            matched = int(receipt.get("matchedCount") or 0)
            decision = str(receipt.get("decision") or "")
            verb = "批准" if decision == "approve" else "拒绝"
            pull_request_urls = list(
                dict.fromkeys(
                    url
                    for result in receipt.get("results", [])
                    if isinstance(result, Mapping)
                    if (url := _pull_request_url(result)) is not None
                )
            )
            if accepted < 1:
                message = f"未{verb}任何 PR；匹配到 {matched} 个待审批项。"
            else:
                message = (
                    f"✅ 已{verb} {accepted} 个修复 PR"
                    + (f"（匹配 {matched} 个）" if matched != accepted else "")
                    + "。Hermes 正在继续执行并验证业务恢复。"
                )
                if pull_request_urls:
                    message += "\nPR:\n" + "\n".join(pull_request_urls)
            return ConversationApprovalResult(state="committed", reply_text=message)
        except ApprovalBoundaryError as exc:
            if exc.uncertain:
                return ConversationApprovalResult(
                    state="uncertain",
                    reply_text="⏳ 对话审批结果暂不确定，请稍后查询 Action 状态。",
                )
            return ConversationApprovalResult(
                state="rejected",
                reply_text="❌ 这条对话未形成审批：指令、身份、范围或当前状态校验失败。",
            )
        except Exception:
            return ConversationApprovalResult(
                state="uncertain",
                reply_text="⏳ 对话审批结果暂不确定，请稍后查询 Action 状态。",
            )

    def _build_conversation_request(
        self,
        data: Any,
        command_text: str,
    ) -> Mapping[str, Any]:
        if not self._enabled:
            raise ApprovalBoundaryError("approval_disabled")
        if self._connection_mode != "websocket":
            raise ApprovalBoundaryError("invalid_transport")
        if not all(
            [self._app_id, self._tenant_key, self._base_url, self._gateway_token]
        ):
            raise ApprovalBoundaryError("approval_not_configured")
        base_url = _validate_base_url(self._base_url)
        normalized_text = re.sub(r"\s+", " ", command_text.strip())
        if not normalized_text or len(normalized_text) > 256:
            raise ApprovalBoundaryError("invalid_command")

        header = _read(data, "header")
        event = _read(data, "event")
        sender = _read(_read(event, "sender"), "sender_id")
        message = _read(event, "message")
        event_id = _required_text(_read(header, "event_id"), "event_id")
        event_type = _required_text(_read(header, "event_type"), "event_type")
        if event_type != "im.message.receive_v1":
            raise ApprovalBoundaryError("invalid_event_type")
        tenant_key = _required_text(_read(header, "tenant_key"), "tenant_key")
        if tenant_key != self._tenant_key:
            raise ApprovalBoundaryError("tenant_mismatch")
        principal_open_id = _required_text(
            _read(sender, "open_id"), "principal_open_id"
        )
        chat_id = _required_text(_read(message, "chat_id"), "chat_id")
        message_id = _required_text(_read(message, "message_id"), "message_id")
        parent_message_id = str(_read(message, "parent_id") or "").strip() or None
        root_message_id = str(_read(message, "root_id") or "").strip() or None
        occurred_timestamp, occurred_at = _parse_occurred_at(
            _read(header, "create_time")
        )
        received_timestamp = self._wall_clock()
        age = received_timestamp - occurred_timestamp
        if age < -30 or age > self._event_max_age_seconds:
            raise ApprovalBoundaryError("event_outside_freshness_window")
        received_at = _iso_utc(received_timestamp)
        digest_material = {
            "header": {
                "event_id": event_id,
                "event_type": event_type,
                "tenant_key": tenant_key,
                "create_time": occurred_at,
            },
            "event": {
                "principal_open_id": principal_open_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "parent_message_id": parent_message_id,
                "root_message_id": root_message_id,
                "command_text": normalized_text,
            },
        }
        raw_event_digest = hashlib.sha256(
            json.dumps(
                digest_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "baseUrl": base_url,
            "body": {
                "schemaVersion": CONVERSATION_SCHEMA,
                "event": {
                    "schemaVersion": CONVERSATION_EVENT_SCHEMA,
                    "source": "feishu",
                    "transport": "websocket",
                    "verified": True,
                    "appId": self._app_id,
                    "tenantKey": tenant_key,
                    "eventId": event_id,
                    "eventType": event_type,
                    "principalOpenId": principal_open_id,
                    "chatId": chat_id,
                    "messageId": message_id,
                    **(
                        {"parentMessageId": parent_message_id}
                        if parent_message_id
                        else {}
                    ),
                    **(
                        {"rootMessageId": root_message_id}
                        if root_message_id
                        else {}
                    ),
                    "occurredAt": occurred_at,
                    "receivedAt": received_at,
                    "rawEventDigest": raw_event_digest,
                },
                "commandText": normalized_text,
            },
        }

    def _decide_conversation(
        self,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        body = request["body"]
        if not isinstance(body, Mapping):
            raise ApprovalBoundaryError("invalid_request")
        url = f"{request['baseUrl']}/v1/conversation-decisions"
        receipt = self._transport(
            url,
            self._gateway_token,
            body,
            self._callback_timeout_seconds,
        )
        if (
            receipt.get("schemaVersion") != CONVERSATION_RECEIPT_SCHEMA
            or receipt.get("decision") not in {"approve", "reject"}
            or receipt.get("selector")
            not in {"reply", "pull_request", "all_pending_pull_requests"}
            or not isinstance(receipt.get("matchedCount"), int)
            or not isinstance(receipt.get("acceptedCount"), int)
            or not isinstance(receipt.get("results"), list)
        ):
            raise DecisionTransportError(None, uncertain=True)
        return receipt

    def _build_request(
        self,
        data: Any,
        action_value: Any,
    ) -> Mapping[str, Any]:
        if not self._enabled:
            raise ApprovalBoundaryError("approval_disabled")
        if self._connection_mode != "websocket":
            raise ApprovalBoundaryError("invalid_transport")
        if not all(
            [
                self._app_id,
                self._tenant_key,
                self._base_url,
                self._gateway_token,
            ]
        ):
            raise ApprovalBoundaryError("approval_not_configured")
        base_url = _validate_base_url(self._base_url)

        action = _parse_action_value(action_value)
        if action is None or set(action) != _ACTION_FIELDS:
            raise ApprovalBoundaryError("invalid_action")
        if action.get("schemaVersion") != ACTION_SCHEMA:
            raise ApprovalBoundaryError("unsupported_action_schema")
        action_id = _required_text(action.get("actionId"), "action_id")
        try:
            uuid.UUID(action_id)
        except ValueError as exc:
            raise ApprovalBoundaryError("invalid_action_id") from exc
        challenge_revision = action.get("challengeRevision")
        if (
            isinstance(challenge_revision, bool)
            or not isinstance(challenge_revision, int)
            or challenge_revision < 1
        ):
            raise ApprovalBoundaryError("invalid_challenge_revision")
        decision = action.get("decision")
        if decision not in DECISIONS:
            raise ApprovalBoundaryError("invalid_decision")
        challenge = _required_text(action.get("challenge"), "challenge")
        if not _CHALLENGE_RE.fullmatch(challenge):
            raise ApprovalBoundaryError("invalid_challenge")

        header = _read(data, "header")
        event = _read(data, "event")
        operator = _read(event, "operator")
        context = _read(event, "context")
        event_id = _required_text(_read(header, "event_id"), "event_id")
        event_type = _required_text(_read(header, "event_type"), "event_type")
        if event_type != "card.action.trigger":
            raise ApprovalBoundaryError("invalid_event_type")
        tenant_key = _required_text(_read(header, "tenant_key"), "tenant_key")
        if tenant_key != self._tenant_key:
            raise ApprovalBoundaryError("tenant_mismatch")
        principal_open_id = _required_text(
            _read(operator, "open_id"), "principal_open_id"
        )
        chat_id = _required_text(_read(context, "open_chat_id"), "chat_id")
        message_id = _required_text(
            _read(context, "open_message_id"), "message_id"
        )
        occurred_timestamp, occurred_at = _parse_occurred_at(
            _read(header, "create_time")
        )
        received_timestamp = self._wall_clock()
        age = received_timestamp - occurred_timestamp
        if age < -30 or age > self._event_max_age_seconds:
            raise ApprovalBoundaryError("event_outside_freshness_window")
        received_at = _iso_utc(received_timestamp)

        digest_material = {
            "header": {
                "event_id": event_id,
                "event_type": event_type,
                "tenant_key": tenant_key,
                "create_time": occurred_at,
            },
            "event": {
                "operator": {"open_id": principal_open_id},
                "context": {
                    "open_chat_id": chat_id,
                    "open_message_id": message_id,
                },
                "action": action,
            },
        }
        raw_event_digest = hashlib.sha256(
            json.dumps(
                digest_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        request = {
            "schemaVersion": DECISION_SCHEMA,
            "event": {
                "schemaVersion": VERIFIED_EVENT_SCHEMA,
                "source": "feishu",
                "transport": "websocket",
                "verified": True,
                "appId": self._app_id,
                "tenantKey": tenant_key,
                "eventId": event_id,
                "eventType": event_type,
                "principalOpenId": principal_open_id,
                "chatId": chat_id,
                "messageId": message_id,
                "occurredAt": occurred_at,
                "receivedAt": received_at,
                "rawEventDigest": raw_event_digest,
            },
            "challengeRevision": challenge_revision,
            "decision": decision,
            "challenge": challenge,
        }
        return {
            "actionId": action_id,
            "baseUrl": base_url,
            "body": request,
        }

    def _decide(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        action_id = str(request["actionId"])
        body = request["body"]
        if not isinstance(body, Mapping):
            raise ApprovalBoundaryError("invalid_request")
        url = (
            f"{request['baseUrl']}/v1/actions/"
            f"{quote(action_id, safe='')}/decision"
        )
        deadline = self._monotonic_clock() + self._callback_timeout_seconds
        last_error: ApprovalBoundaryError | None = None
        for attempt in range(2):
            remaining = deadline - self._monotonic_clock()
            if remaining <= 0:
                break
            attempts_left = 2 - attempt
            timeout = max(0.05, remaining / attempts_left)
            try:
                receipt = self._transport(
                    url,
                    self._gateway_token,
                    body,
                    timeout,
                )
                self._validate_receipt(receipt, action_id, body)
                return receipt
            except DecisionTransportError as exc:
                last_error = exc
                if not exc.uncertain:
                    raise
        raise last_error or DecisionTransportError(None, uncertain=True)

    @staticmethod
    def _validate_receipt(
        receipt: Mapping[str, Any],
        action_id: str,
        request: Mapping[str, Any],
    ) -> None:
        if not isinstance(receipt, Mapping):
            raise DecisionTransportError(None, uncertain=True)
        event = request.get("event")
        expected_event_key = (
            f"feishu:{event.get('appId')}:{event.get('eventId')}"
            if isinstance(event, Mapping)
            else ""
        )
        if (
            receipt.get("schemaVersion") != RECEIPT_SCHEMA
            or receipt.get("actionId") != action_id
            or receipt.get("decision") != request.get("decision")
            or receipt.get("externalEventKey") != expected_event_key
            or not _DIGEST_RE.fullmatch(str(receipt.get("planHash") or ""))
            or not str(receipt.get("targetVersion") or "").strip()
            or not str(receipt.get("decisionEventId") or "").strip()
        ):
            raise DecisionTransportError(None, uncertain=True)
        target_url = str(receipt.get("targetUrl") or "").strip()
        if target_url and _pull_request_url(receipt) is None:
            raise DecisionTransportError(None, uncertain=True)


def _status_card(
    *,
    title: str,
    template: str,
    message: str,
    action_id: str | None = None,
    state: str | None = None,
) -> Mapping[str, Any]:
    details = [message]
    if action_id:
        details.append(f"Action: {action_id}")
    if state:
        details.append(f"当前状态: {state}")
    return {
        "config": {
            "wide_screen_mode": True,
            "enable_forward": False,
        },
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "\n".join(details),
            }
        ],
    }
