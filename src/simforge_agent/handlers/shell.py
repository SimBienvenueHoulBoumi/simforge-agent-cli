"""Handler shell — exécution shell directe (DÉPRÉCIÉ).

Backward compat pour les tâches legacy qui utilisent encore
``task_type="shell"`` avec une ``command`` brute. Toute nouvelle tâche
doit utiliser un type structuré (helm_install, kubectl_rollout, etc.).

Un avertissement est loggé à chaque exécution.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

SendFn = Callable[[dict], Awaitable[None]]


class ShellHandler:
    """Exécute une commande shell arbitraire. Usage déprécié — type shell legacy."""

    def __init__(self, send_fn: SendFn) -> None:
        self._send = send_fn

    async def handle_shell(self, payload: dict) -> None:
        """Exécute ``command`` en shell. Ne PAS utiliser pour les nouveaux flux.

        Args du payload :
            command (str) — commande shell complète
            task_id (str) — identifiant de la tâche (optionnel)
            timeout (int) — timeout en secondes (défaut: 300)
        """
        command = payload.get("command", "")
        if not command:
            raise ValueError("shell: commande vide")

        task_id = payload.get("task_id")
        timeout = payload.get("timeout", 300)

        logger.warning(
            "⚠️  SHELL DÉPRÉCIÉ — task_id=%s command=%.80s…",
            task_id, command.replace("\n", "\\n"),
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            exit_code = proc.returncode or 0
            stdout_str = stdout.decode(errors="replace")
            stderr_str = stderr.decode(errors="replace")
        except (asyncio.TimeoutError, FileNotFoundError, PermissionError) as exc:
            exit_code = 1
            stdout_str = ""
            stderr_str = str(exc)

        await self._send({
            "type": "task_result",
            "task_id": task_id,
            "exit_code": exit_code,
            "stdout": stdout_str,
            "stderr": stderr_str,
        })
