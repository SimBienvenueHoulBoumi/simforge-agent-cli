"""Handler kubectl_rollout — rollout status, wait, et vérification déploiement.

Exécute ``kubectl rollout status deployment/<name> -n <ns> --timeout=<t>``
avec des arguments structurés (pas d'injection shell).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SendFn = Callable[[dict], Awaitable[None]]


def _validate_k8s_name(value: str, field: str) -> None:
    """Rejette les noms K8s dangereux (injection kubectl)."""
    if not value:
        raise ValueError(f"{field}: valeur vide")
    if value.startswith("-"):
        raise ValueError(f"{field}: ne peut pas commencer par '-'")
    # Un nom K8s valide = alphanum, '-', '_', '.'
    if not all(c.isalnum() or c in "-_." for c in value):
        raise ValueError(f"{field}: caractères invalides dans '{value}'")


class RolloutHandler:
    """Vérifie que le rollout d'un déploiement K8s est terminé."""

    def __init__(self, send_fn: SendFn) -> None:
        self._send = send_fn

    async def handle_rollout(self, payload: dict) -> dict:
        """Exécute ``kubectl rollout status`` et retourne le résultat.

        Args du payload (envoyé par le backend) :
            deployment (str) — nom du déploiement
            namespace (str) — namespace cible
            timeout (str) — timeout kubectl (ex: "120s")
            task_id (str) — identifiant de la tâche (optionnel)
        """
        deployment = payload.get("deployment", "")
        namespace = payload.get("namespace", "default")
        from simforge_agent.handlers.helm import _normalize_helm_timeout

        timeout = _normalize_helm_timeout(payload.get("timeout", "120s"))
        task_id = payload.get("task_id")

        _validate_k8s_name(deployment, "deployment")
        _validate_k8s_name(namespace, "namespace")

        cmd = [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{deployment}",
            "-n", namespace,
            f"--timeout={timeout}",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=130)
            exit_code = proc.returncode or 0
            stdout_str = stdout.decode(errors="replace")
            stderr_str = stderr.decode(errors="replace")
        except (asyncio.TimeoutError, FileNotFoundError, PermissionError) as exc:
            exit_code = 1
            stdout_str = ""
            stderr_str = str(exc)

        result = {
            "deployment": deployment,
            "namespace": namespace,
            "rollout_status": "success" if exit_code == 0 else "failed",
            "output": stdout_str.strip(),
        }

        await self._send({
            "type": "task_result",
            "task_id": task_id,
            "exit_code": exit_code,
            "stdout": stdout_str,
            "stderr": stderr_str,
        })
        return result
