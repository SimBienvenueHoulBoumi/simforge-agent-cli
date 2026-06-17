"""Handler health_check — état d'un service via helm status + kubectl pods."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)
SendFn = Callable[[dict], Awaitable[None]]


def _kubeconfig_flags() -> list[str]:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return []
    kube = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
    return ["--kubeconfig", kube] if Path(kube).exists() else []


class HealthHandler:
    def __init__(self, send_fn: SendFn) -> None:
        self._send = send_fn

    async def _run_cmd(self, cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            return 1, "", str(exc)

    async def handle_health_check(self, payload: dict) -> dict:
        release_name = payload.get("release_name", "")
        namespace = payload.get("namespace", "default")
        task_id = payload.get("task_id")
        kf = _kubeconfig_flags()

        (helm_rc, helm_out, _), (pods_rc, pods_out, _) = await asyncio.gather(
            self._run_cmd(["helm", "status", release_name, "-n", namespace, "-o", "json", *kf]),
            self._run_cmd(["kubectl", "get", "pods", "-n", namespace, "-o", "json", *kf]),
        )

        helm_status = "unknown"
        if helm_rc == 0:
            try:
                helm_status = json.loads(helm_out).get("info", {}).get("status", "unknown")
            except json.JSONDecodeError:
                pass

        pods_running = pods_failed = 0
        if pods_rc == 0:
            try:
                for pod in json.loads(pods_out).get("items", []):
                    phase = pod.get("status", {}).get("phase", "")
                    if phase == "Running":
                        pods_running += 1
                    elif phase in ("Failed", "CrashLoopBackOff", "OOMKilled"):
                        pods_failed += 1
            except json.JSONDecodeError:
                pass

        if helm_status == "deployed" and pods_failed == 0 and pods_running > 0:
            status = "healthy"
        elif helm_status == "failed" or (pods_failed > 0 and pods_running == 0):
            status = "unhealthy"
        elif pods_failed > 0:
            status = "degraded"
        else:
            status = "unknown"

        result = {
            "status": status,
            "helm_status": helm_status,
            "pods_running": pods_running,
            "pods_failed": pods_failed,
        }
        await self._send({
            "type": "task_result", "task_id": task_id,
            "exit_code": 0, "stdout": json.dumps(result), "stderr": "",
        })
        return result
