"""Handler cluster_probe — collecte l'état du cluster (nodes, pods, helm releases)."""
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


class ProbeHandler:
    def __init__(self, send_fn: SendFn) -> None:
        self._send = send_fn

    async def _run_cmd(self, cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            return 1, "", str(exc)

    async def handle_probe(self, payload: dict) -> dict:
        """Exécute helm list + kubectl nodes/pods et retourne l'état structuré."""
        kf = _kubeconfig_flags()
        (nodes_rc, nodes_out, _), (pods_rc, pods_out, _), (helm_rc, helm_out, _) = \
            await asyncio.gather(
                self._run_cmd(["kubectl", "get", "nodes", "-o", "json", *kf]),
                self._run_cmd(["kubectl", "get", "pods", "-A", "-o", "json", *kf]),
                self._run_cmd(["helm", "list", "-A", "-o", "json", *kf]),
            )

        nodes = json.loads(nodes_out).get("items", []) if nodes_rc == 0 else []
        pods = json.loads(pods_out).get("items", []) if pods_rc == 0 else []
        helm_releases_raw = json.loads(helm_out) if helm_rc == 0 and helm_out.strip() else []

        # Calculer les ressources allocatable totales
        total_cpu, total_ram = 0, 0.0
        for node in nodes:
            alloc = node.get("status", {}).get("allocatable", {})
            try:
                total_cpu += int(alloc.get("cpu", "0"))
            except ValueError:
                pass
            mem = alloc.get("memory", "0Ki")
            try:
                if mem.endswith("Ki"):
                    total_ram += int(mem[:-2]) / (1024 ** 2)
                elif mem.endswith("Mi"):
                    total_ram += int(mem[:-2]) / 1024
                elif mem.endswith("Gi"):
                    total_ram += float(mem[:-2])
            except ValueError:
                pass

        result = {
            "nodes_count": len(nodes),
            "total_cpu_cores": total_cpu,
            "total_ram_gb": round(total_ram, 1),
            "pods_total": len(pods),
            "pods_running": sum(1 for p in pods if p.get("status", {}).get("phase") == "Running"),
            "pods_failed": sum(1 for p in pods if p.get("status", {}).get("phase") == "Failed"),
            "helm_releases": [
                {
                    "name": r.get("name", ""),
                    "namespace": r.get("namespace", ""),
                    "chart": r.get("chart", ""),
                    "status": r.get("status", ""),
                    "updated": r.get("updated", ""),
                }
                for r in (helm_releases_raw or [])
            ],
        }

        await self._send({
            "type": "task_result",
            "task_id": payload.get("task_id"),
            "exit_code": 0,
            "stdout": json.dumps(result),
            "stderr": "",
        })
        return result
