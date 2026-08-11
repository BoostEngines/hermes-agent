"""Verified Feishu conversation boundary for DataHub login recovery.

This is deliberately a narrow control-plane adapter.  It parses only a
small, canonical vocabulary from SDK-verified Feishu messages and forwards
that intent to DataHub's server-owned recovery conversation endpoint.  It
never contains credentials, browser cookies, or a direct browser operation;
DataHub may publish its audited relogin_device Action for an explicit
operator login request.

The server remains authoritative for principal permission, explicit target
resolution, fresh recovery state, idempotency, and the decision to enqueue
any recovery action.  An Incident topic is one valid source of context, but
an operator may also name an AdsPower profile from a status topic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


LOGIN_RECOVERY_CONVERSATION_SCHEMA = "datahub.ops.feishu-relogin-conversation.v1"
LOGIN_RECOVERY_CONVERSATION_RECEIPT_SCHEMA = (
    "datahub.ops.feishu-relogin-conversation-receipt.v1"
)
LOGIN_RECOVERY_CONVERSATION_API_PATH = "/v1/relogin-conversations/receipts"

_INTENT_NAMES = frozenset(
    {
        "explain_current_failure",
        "explain_automation",
        "request_retry",
        "request_relogin",
        "confirm_local_login",
        "confirm_captcha_complete",
    }
)
_WRITE_INTENT_NAMES = frozenset(
    {
        "request_retry",
        "request_relogin",
        "confirm_local_login",
        "confirm_captcha_complete",
    }
)
_RECEIPT_STATES = frozenset(
    {
        "explained",
        "action_queued",
        "reassessment_queued",
        "requires_reassessment",
        "not_implemented",
    }
)
_ROUTING_IDENTIFIER_RE = re.compile(r"^(?:oc|om|omt)_[A-Za-z0-9_-]{1,180}$")
_OPEN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_ENDPOINT_PATH_RE = re.compile(r"^/v1/[A-Za-z0-9._/-]{1,240}$")
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
_DEVICE_ORDINAL_PREFIX_RE = re.compile(r"^\d{1,4}号设备")


class LoginRecoveryConversationError(Exception):
    """A fail-closed validation or controlled-conversation RPC error."""

    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


class LoginRecoveryConversationTransportError(LoginRecoveryConversationError):
    def __init__(self, status: int | None, *, uncertain: bool):
        super().__init__("conversation_transport_failed", uncertain=uncertain)
        self.status = status


@dataclass(frozen=True)
class CanonicalIntent:
    """A bounded intent that is safe to send instead of raw conversation text."""

    name: str
    target: "TargetReference | None" = None

    def __post_init__(self) -> None:
        if self.name not in _INTENT_NAMES:
            raise ValueError("unknown login recovery intent")

    @property
    def is_write(self) -> bool:
        return self.name in _WRITE_INTENT_NAMES


@dataclass(frozen=True)
class TargetReference:
    type: str
    id: str

    def __post_init__(self) -> None:
        if self.type not in {"profile", "device"}:
            raise ValueError("unknown login recovery target type")
        if not _TARGET_ID_RE.fullmatch(self.id):
            raise ValueError("invalid login recovery target id")


@dataclass(frozen=True)
class LoginRecoveryConversationResult:
    state: str
    reply_text: str


LoginRecoveryTransport = Callable[
    [str, str, Mapping[str, Any], float, str],
    Mapping[str, Any],
]


def _read(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required_text(value: Any, name: str, *, maximum: int = 512) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise LoginRecoveryConversationError(f"missing_{name}")
    return normalized


def _normalized_command_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _strip_terminal_punctuation(value: str) -> str:
    return value.rstrip("。！!？?，,吧呀啊哦")


_CANONICAL_COMMANDS = {
    "现在什么问题": "explain_current_failure",
    "什么问题": "explain_current_failure",
    "失败原因": "explain_current_failure",
    "现在失败原因": "explain_current_failure",
    "为什么没有自动处理": "explain_automation",
    "为什么没自动处理": "explain_automation",
    "为什么没有自动恢复": "explain_automation",
    "再试一次": "request_retry",
    "重试一次": "request_retry",
    "再登录一次": "request_relogin",
    "重新登录一次": "request_relogin",
    "直接重新登录": "request_relogin",
    "请直接重新登录": "request_relogin",
    "重新登录": "request_relogin",
    "请重新登录": "request_relogin",
    "直接登录": "request_relogin",
    "请直接登录": "request_relogin",
    "登录": "request_relogin",
    "我本地登录好了": "confirm_local_login",
    "本地登录好了": "confirm_local_login",
    "我已经本地登录好了": "confirm_local_login",
    "captcha已完成": "confirm_captcha_complete",
    "人机验证已完成": "confirm_captcha_complete",
    "验证码挑战已完成": "confirm_captcha_complete",
}


def _targeted_intent(text: str) -> CanonicalIntent | None:
    """Parse ``[<ordinal>号设备] <profile> <fixed command>`` safely."""

    separators = ":：,，"
    for command, name in sorted(
        _CANONICAL_COMMANDS.items(), key=lambda item: -len(item[0])
    ):
        candidates: list[str] = []
        if text.endswith(command):
            candidates.append(text[: -len(command)])
        if text.startswith(command):
            candidates.append(text[len(command) :])
        for candidate in candidates:
            if candidate == text or not candidate:
                continue
            candidate = candidate.strip(separators)
            candidate = _DEVICE_ORDINAL_PREFIX_RE.sub("", candidate, count=1)
            if _TARGET_ID_RE.fullmatch(candidate):
                return CanonicalIntent(
                    name,
                    TargetReference(type="profile", id=candidate),
                )
    return None


def canonical_login_recovery_intent(value: Any) -> CanonicalIntent | None:
    """Return an explicit, lossily parsed login-recovery intent, if any.

    Free-form text is intentionally not forwarded.  This prevents a message
    mentioning a credential, a profile id, or a production command from
    becoming a side effect through this pathway.
    """

    text = _normalized_command_text(value)
    if not text or len(text) > 128:
        return None
    normalized = _strip_terminal_punctuation(text)
    name = _CANONICAL_COMMANDS.get(normalized)
    return CanonicalIntent(name) if name else _targeted_intent(normalized)


def looks_like_datahub_login_recovery_command(
    value: Any,
    *,
    has_reply_context: bool,
) -> bool:
    """Claim canonical login-recovery language before it reaches a model.

    Rootless unqualified commands are claimed as well, so they receive the
    bounded "reply in the recovery topic" response rather than a model/tool
    attempt.  A rootless command that names a profile is forwarded through
    the explicit-target path.
    """

    del has_reply_context
    return canonical_login_recovery_intent(value) is not None


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
            raise LoginRecoveryConversationError("invalid_occurred_at") from exc
    try:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LoginRecoveryConversationError("invalid_occurred_at") from exc
    if parsed.tzinfo is None:
        raise LoginRecoveryConversationError("invalid_occurred_at")
    return parsed.timestamp(), _iso_utc(parsed.timestamp())


def _bounded_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
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
        raise LoginRecoveryConversationError("invalid_base_url")
    if parsed.username or parsed.password or not parsed.netloc:
        raise LoginRecoveryConversationError("invalid_base_url")
    return normalized


def _validate_endpoint_path(value: str) -> str:
    normalized = value.strip()
    if (
        not _ENDPOINT_PATH_RE.fullmatch(normalized)
        or "//" in normalized
        or "/../" in normalized
        or normalized.endswith("/..")
    ):
        raise LoginRecoveryConversationError("invalid_endpoint_path")
    return normalized


def _default_transport(
    url: str,
    token: str,
    body: Mapping[str, Any],
    timeout_seconds: float,
    contract_version: str,
) -> Mapping[str, Any]:
    request = Request(
        url,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-DataHub-Ops-Contract-Version": contract_version,
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            if response.status < 200 or response.status >= 300:
                raise LoginRecoveryConversationTransportError(
                    response.status,
                    uncertain=response.status >= 500,
                )
            encoded = response.read(1_048_577)
            if len(encoded) > 1_048_576:
                raise LoginRecoveryConversationTransportError(None, uncertain=True)
            payload = json.loads(encoded)
    except HTTPError as exc:
        raise LoginRecoveryConversationTransportError(
            exc.code,
            uncertain=exc.code >= 500,
        ) from exc
    except (TimeoutError, URLError, OSError, ValueError) as exc:
        raise LoginRecoveryConversationTransportError(None, uncertain=True) from exc
    if not isinstance(payload, Mapping):
        raise LoginRecoveryConversationTransportError(None, uncertain=True)
    return payload


class LoginRecoveryConversationGatewayAdapter:
    """One centralized, schema-versioned adapter to DataHub's conversation API."""

    def __init__(
        self,
        *,
        base_url: str,
        gateway_token: str,
        timeout_seconds: float,
        transport: LoginRecoveryTransport = _default_transport,
    ):
        self._base_url = _validate_base_url(base_url)
        self._endpoint_path = _validate_endpoint_path(
            LOGIN_RECOVERY_CONVERSATION_API_PATH
        )
        self._gateway_token = _required_text(gateway_token, "gateway_token")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    @property
    def endpoint_url(self) -> str:
        return f"{self._base_url}{self._endpoint_path}"

    def submit(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._transport(
            self.endpoint_url,
            self._gateway_token,
            body,
            self._timeout_seconds,
            LOGIN_RECOVERY_CONVERSATION_SCHEMA,
        )


class DataHubLoginRecoveryConversationHandler:
    """Parse and relay a verified topic reply without performing recovery locally."""

    def __init__(
        self,
        *,
        enabled: bool,
        app_id: str,
        tenant_key: str,
        connection_mode: str,
        adapter: LoginRecoveryConversationGatewayAdapter | None,
        event_max_age_ms: int = 300_000,
        wall_clock: Callable[[], float] = time.time,
    ):
        self._enabled = enabled
        self._app_id = app_id.strip()
        self._tenant_key = tenant_key.strip()
        self._connection_mode = connection_mode.strip().lower()
        self._adapter = adapter
        self._event_max_age_seconds = event_max_age_ms / 1_000
        self._wall_clock = wall_clock

    @classmethod
    def from_environment(cls) -> "DataHubLoginRecoveryConversationHandler":
        enabled = (
            os.getenv("FEISHU_LOGIN_RECOVERY_CONVERSATION_ENABLED", "false").lower()
            == "true"
        )
        adapter = None
        base_url = os.getenv("DATAHUB_OPS_BASE_URL", "")
        gateway_token = os.getenv("DATAHUB_OPS_GATEWAY_TOKEN", "")
        if base_url and gateway_token:
            try:
                adapter = LoginRecoveryConversationGatewayAdapter(
                    base_url=base_url,
                    gateway_token=gateway_token,
                    timeout_seconds=(
                        _bounded_int(
                            os.getenv("DATAHUB_OPS_LOGIN_RECOVERY_CONVERSATION_TIMEOUT_MS"),
                            2_400,
                            100,
                            2_800,
                        )
                        / 1_000
                    ),
                )
            except LoginRecoveryConversationError:
                # A malformed endpoint fails closed in handle_conversation.
                adapter = None
        return cls(
            enabled=enabled,
            app_id=os.getenv("FEISHU_APP_ID", ""),
            tenant_key=os.getenv("FEISHU_TENANT_KEY", ""),
            connection_mode=os.getenv("FEISHU_CONNECTION_MODE", ""),
            adapter=adapter,
            event_max_age_ms=_bounded_int(
                os.getenv("DATAHUB_OPS_LOGIN_RECOVERY_EVENT_MAX_AGE_MS"),
                300_000,
                30_000,
                900_000,
            ),
        )

    def handle_conversation(
        self,
        data: Any,
        command_text: str,
    ) -> LoginRecoveryConversationResult:
        intent = canonical_login_recovery_intent(command_text)
        if intent is None:
            return LoginRecoveryConversationResult(
                state="rejected",
                reply_text="🤖 Hermes：我只识别登录恢复话题中的固定操作，请使用卡片提示的操作。",
            )
        try:
            request = self._build_request(data, intent)
            receipt = self._submit(request)
            return self._to_view_model(
                receipt,
                intent,
                expected_event_id=str(request["event"]["eventId"]),
            )
        except LoginRecoveryConversationError as exc:
            if exc.code == "missing_topic_message":
                return LoginRecoveryConversationResult(
                    state="rejected",
                    reply_text="🤖 Hermes：请在对应设备的登录恢复话题内回复，我才能安全处理。",
                )
            if exc.uncertain:
                return LoginRecoveryConversationResult(
                    state="uncertain",
                    reply_text="🤖 Hermes：请求结果暂不确定。我没有重复触发登录；请稍后查看该话题状态。",
                )
            return LoginRecoveryConversationResult(
                state="rejected",
                reply_text="🤖 Hermes：这条请求未执行。身份、话题绑定或最新状态校验未通过。",
            )
        except Exception:
            return LoginRecoveryConversationResult(
                state="uncertain",
                reply_text="🤖 Hermes：请求结果暂不确定。我没有重复触发登录；请稍后查看该话题状态。",
            )

    def _build_request(
        self,
        data: Any,
        intent: CanonicalIntent,
    ) -> Mapping[str, Any]:
        if not self._enabled:
            raise LoginRecoveryConversationError("conversation_disabled")
        if self._connection_mode != "websocket":
            raise LoginRecoveryConversationError("invalid_transport")
        if not self._app_id or not self._tenant_key:
            raise LoginRecoveryConversationError("conversation_not_configured")
        if self._adapter is None:
            raise LoginRecoveryConversationError("conversation_not_configured")

        header = _read(data, "header")
        event = _read(data, "event")
        sender = _read(_read(event, "sender"), "sender_id")
        message = _read(event, "message")
        event_id = _required_text(_read(header, "event_id"), "event_id")
        event_type = _required_text(_read(header, "event_type"), "event_type")
        if event_type != "im.message.receive_v1":
            raise LoginRecoveryConversationError("invalid_event_type")
        tenant_key = _required_text(_read(header, "tenant_key"), "tenant_key")
        if tenant_key != self._tenant_key:
            raise LoginRecoveryConversationError("tenant_mismatch")
        principal_open_id = _required_text(
            _read(sender, "open_id"),
            "principal_open_id",
            maximum=256,
        )
        if not _OPEN_ID_RE.fullmatch(principal_open_id):
            raise LoginRecoveryConversationError("invalid_principal")
        chat_id = _required_text(_read(message, "chat_id"), "chat_id", maximum=192)
        message_id = _required_text(
            _read(message, "message_id"), "message_id", maximum=192
        )
        parent_message_id = str(_read(message, "parent_id") or "").strip() or None
        root_message_id = str(_read(message, "root_id") or "").strip() or None
        # A target-qualified command may be sent from any verified status topic
        # (or as a root message).  The server resolves the explicit
        # profile/device in that mode.  Unqualified commands still require an
        # Incident root.
        if root_message_id is None and intent.target is None:
            raise LoginRecoveryConversationError("missing_topic_message")
        thread_id = root_message_id or message_id
        for routing_identifier, name in (
            (chat_id, "chat_id"),
            (message_id, "message_id"),
            (thread_id, "thread_id"),
        ):
            if not _ROUTING_IDENTIFIER_RE.fullmatch(routing_identifier):
                raise LoginRecoveryConversationError(f"invalid_{name}")
        if parent_message_id and not _ROUTING_IDENTIFIER_RE.fullmatch(parent_message_id):
            raise LoginRecoveryConversationError("invalid_parent_message_id")
        if root_message_id and not _ROUTING_IDENTIFIER_RE.fullmatch(root_message_id):
            raise LoginRecoveryConversationError("invalid_root_message_id")
        occurred_timestamp, occurred_at = _parse_occurred_at(
            _read(header, "create_time")
        )
        received_timestamp = self._wall_clock()
        age = received_timestamp - occurred_timestamp
        if age < -30 or age > self._event_max_age_seconds:
            raise LoginRecoveryConversationError("event_outside_freshness_window")
        received_at = _iso_utc(received_timestamp)

        digest_material = {
            "eventId": event_id,
            "eventType": event_type,
            "tenantKey": tenant_key,
            "principalOpenId": principal_open_id,
            "chatId": chat_id,
            "messageId": message_id,
            "parentMessageId": parent_message_id,
            # The target-qualified path does not use this field for routing;
            # use the inbound message as a stable placeholder for the
            # versioned event contract when no Feishu root exists.
            "rootMessageId": root_message_id or message_id,
            "occurredAt": occurred_at,
            "intent": intent.name,
            "target": (
                {"type": intent.target.type, "id": intent.target.id}
                if intent.target is not None
                else None
            ),
        }
        raw_event_digest = hashlib.sha256(
            json.dumps(
                digest_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        event_context = {
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
            "threadId": thread_id,
            # For a target-qualified rootless command this is the inbound
            # message id placeholder; DataHub routes by target in that mode.
            "rootMessageId": root_message_id or message_id,
            "occurredAt": occurred_at,
            "receivedAt": received_at,
            "rawEventDigest": raw_event_digest,
        }
        if parent_message_id:
            event_context["parentMessageId"] = parent_message_id
        return {
            "schemaVersion": LOGIN_RECOVERY_CONVERSATION_SCHEMA,
            # The verified Feishu event id is the server-owned event key.
            # Do not synthesize a second idempotency namespace at the gateway.
            "idempotencyKey": event_id,
            "intent": intent.name,
            "event": event_context,
            **(
                {
                    "target": {
                        "type": intent.target.type,
                        "id": intent.target.id,
                    }
                }
                if intent.target is not None
                else {}
            ),
            "controlRequirements": {
                "principalBinding": "exact",
                "conversationBinding": (
                    "target_exact" if intent.target is not None else "topic_exact"
                ),
                "incidentState": "current_cookie_invalid",
                "eventFreshness": "verified_event_age",
                "idempotency": "gateway_event_key",
            },
        }

    def _submit(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._adapter is None:
            raise LoginRecoveryConversationError("conversation_not_configured")
        receipt = self._adapter.submit(request)
        if not isinstance(receipt, Mapping):
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        return receipt

    def _to_view_model(
        self,
        receipt: Mapping[str, Any],
        intent: CanonicalIntent,
        *,
        expected_event_id: str,
    ) -> LoginRecoveryConversationResult:
        if receipt.get("schemaVersion") != LOGIN_RECOVERY_CONVERSATION_RECEIPT_SCHEMA:
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        state = str(receipt.get("state") or "")
        if state not in _RECEIPT_STATES:
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        event_id = receipt.get("eventId")
        if (
            not isinstance(event_id, str)
            or event_id != expected_event_id
            or receipt.get("idempotencyKey") != event_id
        ):
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        view = receipt.get("view")
        if not isinstance(view, Mapping):
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        intent_receipt = receipt.get("intentReceipt")
        if not isinstance(intent_receipt, Mapping):
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        principal = receipt.get("principal")
        if not isinstance(principal, Mapping):
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        for field in ("principalId", "environment", "mappingRevision"):
            _required_text(principal.get(field), field, maximum=256)
        if (
            intent_receipt.get("intent") != intent.name
            or not isinstance(intent_receipt.get("accepted"), bool)
            or intent_receipt.get("execution")
            not in {"not_started", "reassessment_queued", "action_queued"}
            or not isinstance(intent_receipt.get("code"), str)
        ):
            raise LoginRecoveryConversationTransportError(None, uncertain=True)
        title = _safe_view_text(view.get("title"), "当前恢复状态")
        detail_value = view.get("detail")
        if intent.name == "explain_current_failure":
            detail_value = view.get("failureReason") or detail_value
        elif intent.name == "explain_automation":
            detail_value = view.get("automationExplanation") or detail_value
        detail = _safe_view_text(
            detail_value,
            "DataHub 未返回可展示的诊断。",
        )
        next_step = _safe_view_text(view.get("nextStep"), "")
        lines = [f"🤖 Hermes：{title}", detail]
        if intent.is_write:
            intent_message = _safe_view_text(intent_receipt.get("message"), "")
            execution = intent_receipt.get("execution")
            if execution == "action_queued":
                lines.append(
                    "执行状态：已发布 relogin_device，设备将按现有执行器开始登录。"
                )
            elif execution == "reassessment_queued":
                lines.append(
                    "执行状态：已安排重新评估。DataHub 会先读取新鲜状态，"
                    "只有现有恢复规划器允许时才会创建恢复动作。"
                )
            else:
                lines.append(
                    "执行状态：未启动（not_started）。DataHub 仅记录了这条受控请求，"
                    "尚未由 Gateway 直接触发登录。"
                )
            if intent_message:
                lines.append(intent_message)
        if next_step:
            lines.append(f"下一步：{next_step}")
        return LoginRecoveryConversationResult(state=state, reply_text="\n".join(lines))


def _safe_view_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if len(text) > 1_000 or "\x00" in text:
        raise LoginRecoveryConversationTransportError(None, uncertain=True)
    return text
