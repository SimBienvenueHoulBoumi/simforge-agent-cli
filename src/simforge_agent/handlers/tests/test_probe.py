"""Tests pour le handler cluster_probe."""
import json
import pytest
from unittest.mock import AsyncMock, patch

from simforge_agent.handlers.probe import ProbeHandler


@pytest.fixture
def handler():
    return ProbeHandler(send_fn=AsyncMock())


@pytest.mark.asyncio
async def test_probe_retourne_les_cles_requises(handler):
    async def fake_run(cmd):
        if "helm" in cmd[0]:
            return (0, json.dumps([]), "")  # helm list → liste
        return (0, json.dumps({"items": []}), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_probe({"task_id": "abc"})
    for key in ("nodes_count", "total_cpu_cores", "total_ram_gb", "pods_total", "helm_releases"):
        assert key in result


@pytest.mark.asyncio
async def test_probe_parse_nodes_correctement(handler):
    nodes = {"items": [{
        "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}
    }]}
    async def fake_run(cmd):
        if "nodes" in cmd:
            return (0, json.dumps(nodes), "")
        if "pods" in cmd:
            return (0, json.dumps({"items": []}), "")
        return (0, json.dumps([]), "")  # helm list
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_probe({"task_id": "abc"})
    assert result["nodes_count"] == 1
    assert result["total_cpu_cores"] == 4
    assert result["total_ram_gb"] == 8.0


@pytest.mark.asyncio
async def test_probe_parse_helm_releases(handler):
    helm = [{"name": "grafana", "namespace": "monitoring", "status": "deployed", "chart": "grafana-6.50.7", "updated": ""}]
    async def fake_run(cmd):
        if "list" in cmd:
            return (0, json.dumps(helm), "")
        return (0, json.dumps({"items": []}), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_probe({"task_id": "abc"})
    assert len(result["helm_releases"]) == 1
    assert result["helm_releases"][0]["name"] == "grafana"


@pytest.mark.asyncio
async def test_probe_tolerant_si_kubectl_absent(handler):
    async def fake_run(cmd):
        if "kubectl" in cmd[0]:
            return (1, "", "kubectl: not found")
        return (0, json.dumps([]), "")  # helm OK
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        result = await handler.handle_probe({"task_id": "abc"})
    assert result["nodes_count"] == 0
    assert result["helm_releases"] == []


@pytest.mark.asyncio
async def test_probe_envoie_task_result(handler):
    async def fake_run(cmd):
        if "helm" in cmd[0]:
            return (0, json.dumps([]), "")  # helm list → liste
        return (0, json.dumps({"items": []}), "")
    with patch.object(handler, "_run_cmd", side_effect=fake_run):
        await handler.handle_probe({"task_id": "xyz"})
    handler._send.assert_called_once()
    sent = handler._send.call_args[0][0]
    assert sent["type"] == "task_result"
    assert sent["task_id"] == "xyz"
    assert sent["exit_code"] == 0
