"""Handler preflight_check — vérifie RAM/CPU/conflits avant helm_install."""
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


class PreflightHandler:
    def __init__(self, send_fn: SendFn) -> None:
        self._send = send_fn

    async def _run_cmd(self, cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            return 1, "", str(exc)

    async def handle_preflight(self, payload: dict) -> dict:
        release_name = payload.get("release_name", "")
        namespace = payload.get("namespace", "default")
        required_ram_gb = float(payload.get("required_ram_gb", 0))
        required_cpu = int(payload.get("required_cpu", 0))
        required_storage_gb = float(payload.get("required_storage_gb", 0))
        task_id = payload.get("task_id")
        kf = _kubeconfig_flags()

        (nodes_rc, nodes_out, _), (helm_rc, helm_out, _) = await asyncio.gather(
            self._run_cmd(["kubectl", "get", "nodes", "-o", "json", *kf]),
            self._run_cmd(["helm", "list", "-n", namespace, "-o", "json", *kf]),
        )

        # Cluster inaccessible
        if nodes_rc != 0:
            result = {
                "ok": False,
                "reason": "Impossible de vérifier l'état du cluster (timeout ou kubectl indisponible)",
                "available_ram_gb": 0.0,
                "available_cpu": 0,
                "available_storage_gb": 0.0,
            }
            await self._send({
                "type": "task_result", "task_id": task_id,
                "exit_code": 0, "stdout": json.dumps(result), "stderr": "",
            })
            return result

        # Calculer ressources allocatable
        available_ram, available_cpu, available_storage = 0.0, 0, 0.0
        try:
            for node in json.loads(nodes_out).get("items", []):
                alloc = node.get("status", {}).get("allocatable", {})
                try:
                    available_cpu += int(alloc.get("cpu", "0"))
                except ValueError:
                    pass
                mem = alloc.get("memory", "0Ki")
                try:
                    if mem.endswith("Ki"):
                        available_ram += int(mem[:-2]) / (1024 ** 2)
                    elif mem.endswith("Mi"):
                        available_ram += int(mem[:-2]) / 1024
                    elif mem.endswith("Gi"):
                        available_ram += float(mem[:-2])
                except ValueError:
                    pass
                stor = alloc.get("ephemeral-storage", "0Ki")
                try:
                    if stor.endswith("Ki"):
                        available_storage += int(stor[:-2]) / (1024 ** 2)
                    elif stor.endswith("Mi"):
                        available_storage += int(stor[:-2]) / 1024
                    elif stor.endswith("Gi"):
                        available_storage += float(stor[:-2])
                except ValueError:
                    pass
        except json.JSONDecodeError:
            pass

        # Vérifier releases existantes
        existing: list[str] = []
        if helm_rc == 0:
            try:
                existing = [r.get("name", "") for r in (json.loads(helm_out) or [])]
            except json.JSONDecodeError:
                pass

        result: dict = {
            "ok": True, "reason": "",
            "available_ram_gb": round(available_ram, 1),
            "available_cpu": available_cpu,
            "available_storage_gb": round(available_storage, 1),
        }

        if release_name and release_name in existing:
            result.update({
                "ok": False,
                "reason": f"Conflit : release '{release_name}' déjà présente dans namespace '{namespace}'",
            })
        elif required_ram_gb > 0 and available_ram < required_ram_gb:
            result.update({
                "ok": False,
                "reason": f"RAM insuffisante : {available_ram:.1f} GB disponible, {required_ram_gb} GB requis",
            })
        elif required_cpu > 0 and available_cpu < required_cpu:
            result.update({
                "ok": False,
                "reason": f"CPU insuffisant : {available_cpu} cores disponibles, {required_cpu} requis",
            })
        elif required_storage_gb > 0 and available_storage < required_storage_gb:
            result.update({
                "ok": False,
                "reason": (
                    f"Storage insuffisant : {available_storage:.1f} GB disponible, "
                    f"{required_storage_gb} GB requis"
                ),
            })

        await self._send({
            "type": "task_result", "task_id": task_id,
            "exit_code": 0, "stdout": json.dumps(result), "stderr": "",
        })
        return result
