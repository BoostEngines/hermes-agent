"""HTTP receipts reflect actual admission, through the real background pipeline."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def adapter_with_handler(handler):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "routes": {"alerts": {"secret": _INSECURE_NO_AUTH, "prompt": "{message}"}},
    }))
    adapter.gateway_runner = SimpleNamespace(supports_webhook_admission=True)
    adapter.set_message_handler(handler)
    return adapter


def request(delivery_id):
    req = MagicMock()
    req.headers = {"X-Request-ID": delivery_id}
    req.match_info = {"route_name": "alerts"}
    req.method = "POST"
    body = b'{"message":"recover"}'
    req.content_length = len(body)

    async def read():
        return body

    req.read = read
    return req


@pytest.mark.asyncio
async def test_rejected_turn_returns_429_and_same_delivery_can_retry():
    admitted = False

    async def handler(event):
        if admitted:
            event.admission_callback(True)
        return "done" if admitted else "active session limit"

    adapter = adapter_with_handler(handler)
    response = await adapter._handle_webhook(request("capacity"))
    assert response.status == 429
    assert json.loads(response.text)["error"] == "agent_not_admitted"
    admitted = True
    response = await adapter._handle_webhook(request("capacity"))
    assert response.status == 202
    assert json.loads(response.text)["status"] == "accepted"


@pytest.mark.asyncio
async def test_admission_receipt_does_not_wait_for_model_completion():
    release = asyncio.Event()
    finished = asyncio.Event()

    async def handler(event):
        event.admission_callback(True)
        await release.wait()
        finished.set()
        return "done"

    adapter = adapter_with_handler(handler)
    try:
        response = await asyncio.wait_for(adapter._handle_webhook(request("slow-model")), 5)
        assert response.status == 202
        assert not finished.is_set()
        duplicate = await adapter._handle_webhook(request("slow-model"))
        assert json.loads(duplicate.text)["status"] == "duplicate"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 5)


@pytest.mark.asyncio
async def test_unknown_admission_retains_delivery_without_cancelling_run():
    release = asyncio.Event()
    finished = asyncio.Event()

    async def handler(event):
        await release.wait()
        event.admission_callback(True)
        finished.set()
        return "done"

    adapter = adapter_with_handler(handler)
    try:
        response = await asyncio.wait_for(adapter._handle_webhook(request("uncertain")), 5)
        assert response.status == 202
        assert json.loads(response.text)["status"] == "admission_pending"
        assert "uncertain" in adapter._seen_deliveries
        duplicate = await adapter._handle_webhook(request("uncertain"))
        assert json.loads(duplicate.text)["status"] == "admission_pending"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 5)
