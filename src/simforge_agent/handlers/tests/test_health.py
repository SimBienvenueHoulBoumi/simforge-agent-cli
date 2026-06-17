"""Tests pour le handler health_check."""
import json
import pytest
from unittest.mock import AsyncMock, patch

from simforge_agent.handlers.health import HealthHandler


@pytest.fixture
def handler():
    return HealthHandler(send_fn=AsyncMock())


def helm_status_json(status: str) -> str:
    return json.dumps({"info": {"status": status}})


def pods_json(phases: list[str]) -> str:
    return json.dumps({"items": [{"status": {"phase": p}} for p in phases]})


@pytest.mark.asyncio
async def test_health_sain(handler):
    async def fake_run(cmd):
        if "helm" in cmd[0]:
            return (0, helm_status_json("deployed"), "")
        return (0, pods_json(["Running", "Running"]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_health_check({
            "task_id": "h1", "release_name": "grafana", "namespace": "monitoring",
        })
    assert result["status"] == "healthy"
    assert result["pods_running"] == 2
    assert result["pods_failed"] == 0


@pytest.mark.asyncio
async def test_health_degrade(handler):
    async def fake_run(cmd):
        if "helm" in cmd[0]:
            return (0, helm_status_json("deployed"), "")
        return (0, pods_json(["Running", "Failed"]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_health_check({
            "task_id": "h2", "release_name": "grafana", "namespace": "monitoring",
        })
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_defaillant(handler):
    async def fake_run(cmd):
        if "helm" in cmd[0]:
            return (0, helm_status_json("failed"), "")
        return (0, pods_json(["Failed", "Failed"]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_health_check({
            "task_id": "h3", "release_name": "grafana", "namespace": "monitoring",
        })
    assert result["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_health_agent_deconnecte(handler):
    async def fake_run(cmd):
        return (1, "", "connection refused")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_health_check({
            "task_id": "h4", "release_name": "grafana", "namespace": "monitoring",
        })
    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_health_envoie_task_result(handler):
    async def fake_run(cmd):
        if "helm" in cmd[0]:
            return (0, helm_status_json("deployed"), "")
        return (0, pods_json(["Running"]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        await handler.handle_health_check({
            "task_id": "xyz", "release_name": "g", "namespace": "n",
        })
    sent = handler._send.call_args[0][0]
    assert sent["task_id"] == "xyz"
    assert json.loads(sent["stdout"])["status"] == "healthy"
