"""Exercise the real SDK objects and adapter bypass with isolated durable state."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
from plugins.platforms.feishu import datahub_subscriptions as s
from tests.gateway.feishu_helpers import make_adapter_skeleton


def test_real_adapter_admits_text_and_card_without_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_SUBSCRIPTIONS_ENABLED", "true")
    now = time.time()
    store = s.Store(tmp_path / "tasks.db", now=lambda: now - 1)
    monkeypatch.setattr(s, "_gateway", s.Gateway(store, app_id="app", tenant="tenant", enabled=True))
    header = {"event_id": "sdk-message", "event_type": "im.message.receive_v1", "tenant_key": "tenant", "app_id": "app", "create_time": str(int(now * 1e6))}
    text = "开通 Max 1 年订阅\nUSLC32EMHS maria.garcia7104@zohomail.com"
    data = P2ImMessageReceiveV1({"header": header, "event": {"sender": {"sender_type": "user", "sender_id": {"open_id": "ou_developer"}}, "message": {"chat_id": s.CHAT_ID, "chat_type": "group", "message_type": "text", "message_id": "om_sdk", "create_time": str(int(now * 1000)), "content": json.dumps({"text": text})}}})
    adapter = make_adapter_skeleton()
    async def run_blocking(fn, *args):
        return fn(*args)
    adapter._run_blocking = run_blocking
    adapter._dispatch_inbound_event = AsyncMock(side_effect=AssertionError("must bypass model"))
    asyncio.run(adapter._process_inbound_message(data=data, message=data.event.message, sender_id=data.event.sender.sender_id, chat_type="group", message_id="om_sdk"))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    # A real SDK callback on the persisted entry sends a form, never a grant.
    store.notify("menu-entry", s.entry_card())
    s.Worker(store, deliver=lambda *_: "om_entry").flush()
    callback = P2CardActionTrigger({"header": {**header, "event_type": "card.action.trigger", "event_id": "sdk-card"}, "event": {"operator": {"open_id": "ou_developer"}, "context": {"open_chat_id": s.CHAT_ID, "open_message_id": "om_entry"}, "action": {"tag": "button", "value": {"datahub_subscription": "open"}}}})
    async def trigger():
        adapter._loop = asyncio.get_running_loop()
        return adapter._on_card_action_trigger(callback)
    response = asyncio.run(trigger())
    assert response.toast.content == "已接收，将在本群反馈处理结果。"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forms").fetchone()[0] == 1
    s.Worker(store, deliver=lambda *_: "om_form").flush()
    nonce = s.digest("sdk-card")
    form = P2CardActionTrigger({"header": {**header, "event_type": "card.action.trigger", "event_id": "sdk-form"}, "event": {"operator": {"open_id": "ou_developer"}, "context": {"open_chat_id": s.CHAT_ID, "open_message_id": "om_form"}, "action": {"tag": "button", "name": "datahub_subscription_" + nonce, "form_value": {"years": "1", "accounts": "USLC32EMHS maria.garcia7104@zohomail.com"}}}})
    async def submit():
        adapter._loop = asyncio.get_running_loop()
        return adapter._on_card_action_trigger(form)
    assert asyncio.run(submit()).toast.content == "已接收，将在本群反馈处理结果。"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    adapter._dispatch_inbound_event.assert_not_called()
