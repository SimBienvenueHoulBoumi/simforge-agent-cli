"""Tests pour le handler Helm."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeStreamReader:
    """Simule asyncio.StreamReader avec readline async."""

    def __init__(self, lines: list[str]):
        self._lines = [(line + "\n").encode() for line in lines] + [b""]
        self._idx = 0

    async def readline(self) -> bytes:
        line = self._lines[self._idx]
        if self._idx < len(self._lines) - 1:
            self._idx += 1
        return line


@pytest.mark.asyncio
async def test_helm_install_success():
    from simforge_agent.handlers.helm import HelmHandler

    sent_messages: list[dict] = []

    async def fake_send(msg):
        sent_messages.append(msg)

    handler = HelmHandler(send_fn=fake_send)

    with patch("asyncio.create_subprocess_exec") as mock_proc:
        process = MagicMock()
        process.stdout = _FakeStreamReader(["Release deployed"])
        process.stderr = _FakeStreamReader([])
        process.wait = AsyncMock(return_value=None)
        process.returncode = 0
        mock_proc.return_value = process

        await handler.handle_install({
            "service_install_id": "abc-123",
            "chart": "bitnami/postgresql",
            "chart_version": "14.0.0",
            "release_name": "pg-prod",
            "namespace": "default",
            "values": {},
        })

    result_msgs = [m for m in sent_messages if m["type"] == "helm_result"]
    assert len(result_msgs) == 1
    assert result_msgs[0]["success"] is True


# ── Validation des arguments Helm ────────────────────────────────────────────


def test_valid_helm_args_pass():
    from simforge_agent.handlers.helm import _validate_helm_arg

    _validate_helm_arg("bitnami/postgresql", "chart")
    _validate_helm_arg("pg-prod", "release")
    _validate_helm_arg("my-namespace", "namespace")
    _validate_helm_arg("14.0.5", "version")


def test_flag_injection_blocked():
    from simforge_agent.handlers.helm import _validate_helm_arg, HelmArgError

    with pytest.raises(HelmArgError):
        _validate_helm_arg("--post-renderer=/bin/bash", "chart")


def test_path_traversal_blocked():
    from simforge_agent.handlers.helm import _validate_helm_arg, HelmArgError

    with pytest.raises(HelmArgError):
        _validate_helm_arg("../../etc/passwd", "release")


def test_empty_namespace_blocked():
    from simforge_agent.handlers.helm import _validate_helm_arg, HelmArgError

    with pytest.raises(HelmArgError):
        _validate_helm_arg("", "namespace")


@pytest.mark.asyncio
async def test_helm_install_failure():
    from simforge_agent.handlers.helm import HelmHandler

    sent_messages: list[dict] = []

    async def fake_send(msg):
        sent_messages.append(msg)

    handler = HelmHandler(send_fn=fake_send)

    with patch("asyncio.create_subprocess_exec") as mock_proc:
        process = MagicMock()
        process.stdout = _FakeStreamReader([])
        process.stderr = _FakeStreamReader(["Error: release failed"])
        process.wait = AsyncMock(return_value=None)
        process.returncode = 1
        mock_proc.return_value = process

        await handler.handle_install({
            "service_install_id": "def-456",
            "chart": "bitnami/nginx",
            "chart_version": "14.0.0",
            "release_name": "web-prod",
            "namespace": "web",
            "values": {},
        })

    result_msgs = [m for m in sent_messages if m["type"] == "helm_result"]
    assert len(result_msgs) == 1
    assert result_msgs[0]["success"] is False
    assert result_msgs[0]["service_install_id"] == "def-456"


@pytest.mark.asyncio
async def test_helm_uninstall():
    from simforge_agent.handlers.helm import HelmHandler

    sent_messages: list[dict] = []

    async def fake_send(msg):
        sent_messages.append(msg)

    handler = HelmHandler(send_fn=fake_send)

    with patch("asyncio.create_subprocess_exec") as mock_proc:
        process = MagicMock()
        process.stdout = _FakeStreamReader(["release \"pg-prod\" uninstalled"])
        process.stderr = _FakeStreamReader([])
        process.wait = AsyncMock(return_value=None)
        process.returncode = 0
        mock_proc.return_value = process

        await handler.handle_uninstall({
            "service_install_id": "abc-123",
            "release_name": "pg-prod",
            "namespace": "default",
        })

    result_msgs = [m for m in sent_messages if m["type"] == "helm_result"]
    assert len(result_msgs) == 1
    assert result_msgs[0]["success"] is True
    assert result_msgs[0]["service_install_id"] == "abc-123"


@pytest.mark.asyncio
async def test_helm_missing_binary():
    from simforge_agent.handlers.helm import HelmHandler

    sent_messages: list[dict] = []

    async def fake_send(msg):
        sent_messages.append(msg)

    handler = HelmHandler(send_fn=fake_send)

    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="helm non trouvé"):
            await handler.handle_install({
                "service_install_id": "abc-123",
                "chart": "bitnami/nginx",
                "chart_version": "14.0.0",
                "release_name": "web-prod",
                "namespace": "default",
                "values": {},
            })

    result_msgs = [m for m in sent_messages if m["type"] == "helm_result"]
    assert len(result_msgs) == 1
    assert result_msgs[0]["success"] is False


@pytest.mark.asyncio
async def test_helm_install_cree_secret_oidc_si_present():
    """Si oidc_config est dans le payload, le Secret K8s est créé avant helm."""
    from simforge_agent.handlers.helm import HelmHandler

    sent_messages: list[dict] = []

    async def fake_send(msg):
        sent_messages.append(msg)

    handler = HelmHandler(send_fn=fake_send)

    kubectl_calls: list[list[str]] = []

    async def fake_kubectl(cmd: list[str]) -> tuple[int, str, str]:
        kubectl_calls.append(cmd)
        return (0, "", "")

    with patch.object(handler, "_run_kubectl", new=fake_kubectl), \
         patch("shutil.which", return_value="/usr/bin/helm"), \
         patch.object(handler, "_stream", new=AsyncMock()):
        await handler.handle_install({
            "service_install_id": "install-123",
            "chart": "gitlab/gitlab",
            "release_name": "gitlab",
            "namespace": "cicd",
            "values": {},
            "oidc_config": {
                "client_id": "abc",
                "client_secret": "xyz",
                "issuer": "https://authentik.example.com/application/o/gitlab-org-a/",
                "redirect_uri": "https://gitlab.org-a.example.com/users/auth/openid_connect/callback",
            },
        })

    # Vérifier que kubectl apply a été appelé (création du Secret OIDC)
    assert len(kubectl_calls) >= 1
    assert any("apply" in c for c in [" ".join(cmd) for cmd in kubectl_calls])
