"""Tests pour le handler Shell (backward compat — tâches legacy)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from simforge_agent.handlers.shell import ShellHandler


def _mock_process(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    """Crée un mock de processus shell avec communicate async."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(
        return_value=(stdout.encode(), stderr.encode())
    )
    return proc


@pytest.fixture
def handler():
    sent: list[dict] = []

    async def fake_send(msg):
        sent.append(msg)

    h = ShellHandler(send_fn=fake_send)
    h._sent = sent
    return h


@pytest.mark.asyncio
async def test_shell_executes_command(handler):
    """Commande shell exécutée avec succès → exit_code=0."""
    proc = _mock_process(0, stdout="hello from shell")

    with patch("asyncio.create_subprocess_shell", return_value=proc):
        await handler.handle_shell({
            "command": "echo hello",
            "task_id": "task-sh-1",
            "timeout": 30,
        })

    result = handler._sent[-1]
    assert result["type"] == "task_result"
    assert result["task_id"] == "task-sh-1"
    assert result["exit_code"] == 0
    assert "hello from shell" in result["stdout"]


@pytest.mark.asyncio
async def test_shell_command_fails(handler):
    """Commande shell échoue → exit_code=1."""
    proc = _mock_process(1, stderr="command not found")

    with patch("asyncio.create_subprocess_shell", return_value=proc):
        await handler.handle_shell({
            "command": "nonexistent-command",
            "task_id": "task-sh-2",
            "timeout": 30,
        })

    result = handler._sent[-1]
    assert result["exit_code"] == 1
    assert "not found" in result["stderr"]


@pytest.mark.asyncio
async def test_shell_empty_command(handler):
    """Commande vide → ValueError."""
    with pytest.raises(ValueError, match="commande vide"):
        await handler.handle_shell({
            "command": "",
            "task_id": "task-sh-3",
        })


@pytest.mark.asyncio
async def test_shell_default_timeout(handler):
    """Timeout par défaut = 300s si non fourni."""
    proc = _mock_process(0, stdout="ok")

    with patch("asyncio.create_subprocess_shell", return_value=proc):
        await handler.handle_shell({
            "command": "echo ok",
            "task_id": "task-sh-4",
        })

    result = handler._sent[-1]
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_shell_propagates_timeout_error(handler):
    """Le handler gère les erreurs de sous-processus."""
    with patch("asyncio.create_subprocess_shell") as mock_shell:
        mock_shell.side_effect = FileNotFoundError("shell not found")
        await handler.handle_shell({
            "command": "echo test",
            "task_id": "task-sh-5",
        })

    result = handler._sent[-1]
    assert result["exit_code"] == 1
