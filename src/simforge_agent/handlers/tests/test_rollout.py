"""Tests pour le handler Rollout (kubectl rollout status)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from simforge_agent.handlers.rollout import RolloutHandler


@pytest.fixture
def handler():
    sent: list[dict] = []

    async def fake_send(msg):
        sent.append(msg)

    h = RolloutHandler(send_fn=fake_send)
    h._sent = sent
    return h


def _mock_process(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Crée un mock de processus asyncio avec communicate async."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(stdout.encode(), stderr.encode())
    )
    return proc


@pytest.mark.asyncio
async def test_rollout_success(handler):
    """kubectl rollout status réussit → exit_code=0."""
    proc = _mock_process(0, stdout="deployment rolled out successfully")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await handler.handle_rollout({
            "deployment": "my-app",
            "namespace": "tenant-acme",
            "timeout": "120s",
            "task_id": "task-1",
        })

    result = handler._sent[-1]
    assert result["type"] == "task_result"
    assert result["task_id"] == "task-1"
    assert result["exit_code"] == 0
    assert "rolled out" in result["stdout"]


@pytest.mark.asyncio
async def test_rollout_failure(handler):
    """kubectl rollout status échoue → exit_code=1."""
    proc = _mock_process(1, stderr="error: deployment not found")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await handler.handle_rollout({
            "deployment": "my-app",
            "namespace": "default",
            "timeout": "30s",
            "task_id": "task-2",
        })

    result = handler._sent[-1]
    assert result["exit_code"] == 1
    assert "not found" in result["stderr"]


@pytest.mark.asyncio
async def test_rollout_invalid_deployment(handler):
    """Nom de déploiement invalide → ValueError."""
    with pytest.raises(ValueError, match="ne peut pas commencer par"):
        await handler.handle_rollout({
            "deployment": "--inject-flag",
            "namespace": "default",
            "task_id": "task-3",
        })


@pytest.mark.asyncio
async def test_rollout_empty_deployment(handler):
    """Nom de déploiement vide → ValueError."""
    with pytest.raises(ValueError, match="valeur vide"):
        await handler.handle_rollout({
            "deployment": "",
            "namespace": "default",
            "task_id": "task-4",
        })


@pytest.mark.asyncio
async def test_rollout_invalid_namespace(handler):
    """Namespace invalide → ValueError."""
    with pytest.raises(ValueError, match="caractères invalides"):
        await handler.handle_rollout({
            "deployment": "valid-app",
            "namespace": "../etc/passwd",
            "task_id": "task-5",
        })


@pytest.mark.asyncio
async def test_rollout_subprocess_timeout(handler):
    """Timeout du sous-processus → exit_code=1."""
    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_exec.side_effect = FileNotFoundError("kubectl not found")
        await handler.handle_rollout({
            "deployment": "my-app",
            "namespace": "default",
            "task_id": "task-6",
        })

    result = handler._sent[-1]
    assert result["exit_code"] == 1
