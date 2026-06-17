"""Handler configure_runner — configure GitLab Runner pour pointer vers le GitLab local."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


def _kubeconfig_flags() -> list[str]:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return []
    kube = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
    return ["--kubeconfig", kube] if Path(kube).exists() else []


class GitLabRunnerHandler:
    """Configure GitLab Runner pour pointer vers le GitLab local du cluster."""

    def __init__(self, send_fn: Callable[[dict], Awaitable[None]]) -> None:
        self._send = send_fn

    async def handle_configure(self, payload: dict) -> None:
        gitlab_url = payload["gitlab_internal_url"]
        token = payload["gitlab_admin_token"]
        runner_namespace = payload["runner_namespace"]
        release = payload.get("runner_release_name", "gitlab-runner")

        try:
            runner_token = await self._create_runner_token(gitlab_url, token)
            await self._upgrade_runner_helm(
                release, runner_namespace, gitlab_url, runner_token
            )
            await self._send({"type": "configure_runner_result", "success": True})
        except Exception as exc:
            logger.exception("configure_runner: erreur")
            await self._send({
                "type": "configure_runner_result",
                "success": False,
                "error": str(exc),
            })

    async def _create_runner_token(self, gitlab_url: str, admin_token: str) -> str:
        """Crée un runner instance-level via l'API GitLab locale."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{gitlab_url}/api/v4/user/runners",
                data={
                    "runner_type": "instance_type",
                    "description": "SimDevForge Fleet Runner",
                    "locked": False,
                    "run_untagged": True,
                },
                headers={"PRIVATE-TOKEN": admin_token},
            )
            resp.raise_for_status()
            return resp.json()["token"]

    async def _upgrade_runner_helm(
        self,
        release: str,
        namespace: str,
        gitlab_url: str,
        runner_token: str,
    ) -> None:
        """Met à jour la release Helm gitlab-runner avec le token et l'URL locale."""
        kf = _kubeconfig_flags()
        cmd = [
            "helm", "upgrade", release, "gitlab/gitlab-runner",
            "--namespace", namespace,
            "--reuse-values",
            "--set", f"gitlabUrl={gitlab_url}",
            "--set", f"runnerRegistrationToken={runner_token}",
            "--wait", "--timeout", "5m",
            *kf,
        ]
        rc, _, stderr = await self._run_cmd(cmd)
        if rc != 0:
            raise RuntimeError(f"helm upgrade gitlab-runner échoué: {stderr}")

    async def _run_cmd(self, cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=360)
            return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            return 1, "", str(exc)
