"""Tests pour le handler preflight_check."""
import json
import pytest
from unittest.mock import AsyncMock, patch

from simforge_agent.handlers.preflight import PreflightHandler


@pytest.fixture
def handler():
    return PreflightHandler(send_fn=AsyncMock())


def nodes_json(cpu: str, mem: str, storage: str = "100Gi") -> str:
    return json.dumps({"items": [{"status": {"allocatable": {"cpu": cpu, "memory": mem, "ephemeral-storage": storage}}}]})


def helm_json(releases: list) -> str:
    return json.dumps(releases)


@pytest.mark.asyncio
async def test_preflight_ok_ressources_suffisantes(handler):
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, nodes_json("8", "16Gi"), "")
        return (0, helm_json([]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_preflight({
            "task_id": "t1", "release_name": "grafana",
            "namespace": "monitoring", "required_ram_gb": 0.5, "required_cpu": 1,
        })
    assert result["ok"] is True
    assert result["available_ram_gb"] == 16.0


@pytest.mark.asyncio
async def test_preflight_echec_ram_insuffisante(handler):
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, nodes_json("4", "1Gi"), "")
        return (0, helm_json([]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_preflight({
            "task_id": "t2", "release_name": "sonarqube",
            "namespace": "tools", "required_ram_gb": 4.0, "required_cpu": 2,
        })
    assert result["ok"] is False
    assert "RAM" in result["reason"]


@pytest.mark.asyncio
async def test_preflight_echec_conflit_release(handler):
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, nodes_json("8", "16Gi"), "")
        return (0, helm_json([{"name": "grafana", "namespace": "monitoring"}]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_preflight({
            "task_id": "t3", "release_name": "grafana",
            "namespace": "monitoring", "required_ram_gb": 0.5, "required_cpu": 1,
        })
    assert result["ok"] is False
    assert "grafana" in result["reason"]
    assert "monitoring" in result["reason"]


@pytest.mark.asyncio
async def test_preflight_echec_cpu_insuffisant(handler):
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, nodes_json("1", "32Gi"), "")
        return (0, helm_json([]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_preflight({
            "task_id": "t4", "release_name": "jenkins",
            "namespace": "ci", "required_ram_gb": 2.0, "required_cpu": 2,
        })
    assert result["ok"] is False
    assert "CPU" in result["reason"]


@pytest.mark.asyncio
async def test_preflight_echec_storage_insuffisant(handler):
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, nodes_json("8", "16Gi", "20Gi"), "")
        return (0, helm_json([]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_preflight({
            "release_name": "gitlab",
            "namespace": "cicd",
            "required_ram_gb": 8,
            "required_cpu": 4,
            "required_storage_gb": 50,
        })
    assert result["ok"] is False
    assert "storage" in result["reason"].lower()
    assert result["available_storage_gb"] == pytest.approx(20.0, abs=0.5)


@pytest.mark.asyncio
async def test_preflight_ok_storage_suffisant(handler):
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, nodes_json("8", "16Gi", "100Gi"), "")
        return (0, helm_json([]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_preflight({
            "release_name": "gitlab",
            "namespace": "cicd",
            "required_ram_gb": 8,
            "required_cpu": 4,
            "required_storage_gb": 50,
        })
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_preflight_timeout_cluster(handler):
    async def fake_run(cmd):
        return (1, "", "timeout")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_preflight({
            "task_id": "t5", "release_name": "grafana",
            "namespace": "monitoring", "required_ram_gb": 0.5, "required_cpu": 1,
        })
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_preflight_envoie_task_result(handler):
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, nodes_json("8", "16Gi"), "")
        return (0, helm_json([]), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        await handler.handle_preflight({
            "task_id": "xyz", "release_name": "grafana",
            "namespace": "monitoring", "required_ram_gb": 0.5, "required_cpu": 1,
        })
    sent = handler._send.call_args[0][0]
    assert sent["task_id"] == "xyz"
    assert sent["exit_code"] == 0
    parsed = json.loads(sent["stdout"])
    assert "ok" in parsed
