"""Tests pour le handler GitLab Runner (configure_runner)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_configure_runner_cree_token_et_upgrade_helm():
    """configure_runner crée un token via API GitLab local puis met à jour la release."""
    sent = []

    async def fake_send(msg):
        sent.append(msg)

    from simforge_agent.handlers.gitlab_runner import GitLabRunnerHandler
    handler = GitLabRunnerHandler(send_fn=fake_send)

    payload = {
        "gitlab_internal_url": "http://gitlab.cicd.svc.cluster.local",
        "gitlab_admin_token": "root-token",
        "runner_namespace": "cicd",
        "runner_release_name": "gitlab-runner",
    }

    runner_resp = MagicMock()
    runner_resp.status_code = 201
    runner_resp.json.return_value = {"token": "runner-tok-abc123", "id": 1}
    runner_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls, \
         patch.object(handler, "_run_cmd", new=AsyncMock(return_value=(0, "", ""))) as mock_cmd:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=runner_resp)
        mock_cls.return_value = mock_client

        await handler.handle_configure(payload)

    # helm upgrade doit être appelé avec le token
    helm_call = next(
        c for c in mock_cmd.call_args_list
        if "helm" in c.args[0]
    )
    cmd_str = " ".join(helm_call.args[0])
    assert "runner-tok-abc123" in cmd_str
    assert "gitlab-runner" in cmd_str


@pytest.mark.asyncio
async def test_configure_runner_echec_si_gitlab_inaccessible():
    sent = []

    async def fake_send(msg):
        sent.append(msg)

    from simforge_agent.handlers.gitlab_runner import GitLabRunnerHandler
    handler = GitLabRunnerHandler(send_fn=fake_send)

    import httpx
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_cls.return_value = mock_client

        await handler.handle_configure({
            "gitlab_internal_url": "http://gitlab.cicd.svc.cluster.local",
            "gitlab_admin_token": "token",
            "runner_namespace": "cicd",
            "runner_release_name": "gitlab-runner",
        })

    assert sent[-1]["success"] is False
    assert "refused" in sent[-1]["error"].lower()
