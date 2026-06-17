"""Client WebSocket de l'agent distant — se connecte au backend et dispatche les tâches."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import random
import socket
from pathlib import Path

import websockets

from simforge_agent.config import ca_bundle, insecure_tls
from simforge_agent.handlers.gitlab_runner import GitLabRunnerHandler
from simforge_agent.handlers.health import HealthHandler
from simforge_agent.handlers.helm import HelmHandler
from simforge_agent.handlers.preflight import PreflightHandler
from simforge_agent.handlers.probe import ProbeHandler
from simforge_agent.handlers.rollout import RolloutHandler
from simforge_agent.handlers.shell import ShellHandler

logger = logging.getLogger(__name__)

# Reconnexion exponentielle : 5s → 10s → 20s → 40s → max 60s
BASE_RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60

# 🟡 P1: Limite de tentatives consécutives avant abandon
MAX_CONSECUTIVE_FAILURES = 100

# Heartbeat applicatif (ping/pong) — envoyé toutes les 30s
HEARTBEAT_INTERVAL = 30


# ── Fonctions utilitaires pour le handshake enrichi ─────────────────────────


def _collect_hardware_info() -> dict:
    """Collecte CPU/RAM/OS pour le handshake. Fail-safe si psutil absent."""
    info: dict = {
        "hostname": socket.gethostname(),
        "arch": platform.machine(),
        "os_name": platform.system(),
        "os_version": platform.release(),
        "cpu_cores": 0,
        "ram_total_gb": 0.0,
        "disk_free_gb": 0.0,
        "cpu_brand": "",
    }
    try:
        import psutil  # noqa: PLC0415
        info["cpu_cores"] = psutil.cpu_count(logical=True) or 0
        info["ram_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        info["disk_free_gb"] = round(psutil.disk_usage("/").free / (1024 ** 3), 1)
    except ImportError:
        pass
    return info


def _detect_cluster_info() -> dict:
    """Détecte le type de cluster K8s et le chemin kubeconfig. Fail-safe."""
    kubeconfig = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
    if not Path(kubeconfig).exists():
        return {"type": "none", "kubeconfig_path": ""}
    try:
        content = Path(kubeconfig).read_text(errors="replace")
        if "k3s" in content or Path("/etc/rancher/k3s").exists():
            cluster_type = "k3s"
        elif "microk8s" in content or Path("/var/snap/microk8s").exists():
            cluster_type = "microk8s"
        elif "k0s" in content or Path("/var/lib/k0s").exists():
            cluster_type = "k0s"
        else:
            cluster_type = "k8s"
    except Exception:
        cluster_type = "unknown"
    return {"type": cluster_type, "kubeconfig_path": kubeconfig}


class AgentClient:
    """Client WebSocket qui écoute les tâches du backend et les exécute.

    Gère la reconnexion automatique avec backoff exponentiel et
    heartbeat applicatif pour maintenir la connexion active.
    """

    def __init__(
        self,
        server_url: str,
        agent_key: str,
        cluster_token: str,
        capabilities: list[str] | None = None,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
    ) -> None:
        ws_url = server_url.replace("https://", "wss://").replace("http://", "ws://")
        # Le backend attend une URL exacte : /ws/agents/
        self.ws_url = f"{ws_url}/ws/agents/"
        self.agent_key = agent_key
        self.cluster_token = cluster_token
        self.capabilities = capabilities or ["helm"]
        self.heartbeat_interval = heartbeat_interval

    async def _handle_kubeconfig_upload(
        self, args: dict, task_id: str | None = None
    ) -> None:
        """Upload le kubeconfig du cluster détecté vers le backend.

        Reçoit les arguments structurés du backend (pas de f-string).
        """
        import re as _re

        kubeconfig_path = args.get("kubeconfig_path", "")
        upload_url = args.get("upload_url", "")
        cluster_type = args.get("cluster_type", "k3s")

        # Validation stricte
        if not _re.match(r"^/[A-Za-z0-9._/-]+$", kubeconfig_path):
            raise ValueError(f"kubeconfig_path invalide: {kubeconfig_path!r}")
        if cluster_type not in {"k3s", "k8s", "microk8s", "k0s"}:
            raise ValueError(f"cluster_type invalide: {cluster_type!r}")
        import httpx

        try:
            with open(kubeconfig_path) as f:
                kube = f.read()
        except FileNotFoundError:
            raise ValueError(f"Fichier kubeconfig introuvable: {kubeconfig_path}")

        resp = httpx.post(
            upload_url,
            json={"kubeconfig": kube, "cluster_type": cluster_type},
            headers={
                # self.agent_key vient de config.json (enroll) — l'env var
                # n'existe qu'en mode rétrocompatible et était souvent vide,
                # produisant un header invalide ("Illegal header value").
                "Authorization": f"Agent-Key {self.agent_key or os.environ.get('SIMFORGE_AGENT_KEY', '')}",
            },
            timeout=15,
            verify=ca_bundle() or not insecure_tls(),
        )
        resp.raise_for_status()

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Envoie un heartbeat applicatif toutes les self.heartbeat_interval secondes."""
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                try:
                    await ws.send(
                        json.dumps({"type": "heartbeat", "ts": asyncio.get_event_loop().time()})
                    )
                except websockets.ConnectionClosed:
                    logger.warning("Heartbeat: connexion fermée")
                    return
        except asyncio.CancelledError:
            pass

    async def _connect_once(self) -> None:
        """Connexion unique : handshake + boucle de messages.

        Retourne normalement si la connexion est fermée par le serveur
        ou si une erreur fatale survient.
        """
        logger.info("Connexion à %s", self.ws_url)
        # ping_timeout=None : on désactive les PING websocket (certains proxies
        # ne les forwardent pas correctement). On utilise plutôt le heartbeat
        # applicatif (message 'heartbeat').
        ssl_ctx = None
        if self.ws_url.startswith("wss://"):
            if ca_bundle():
                import ssl

                ssl_ctx = ssl.create_default_context(cafile=ca_bundle())
            elif insecure_tls():
                import ssl

                ssl_ctx = ssl.create_default_context()
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                logger.warning(
                    "SIMFORGE_AGENT_INSECURE=1 — vérification TLS désactivée (dev)"
                )

        async with websockets.connect(
            self.ws_url,
            ping_interval=None,  # heartbeat applicatif plutôt que WebSocket PING
            close_timeout=5,
            ssl=ssl_ctx,
        ) as ws:
            helm_ctx = HelmHandler(send_fn=lambda msg: ws.send(json.dumps(msg)))
            probe_ctx = ProbeHandler(send_fn=lambda msg: ws.send(json.dumps(msg)))
            preflight_ctx = PreflightHandler(send_fn=lambda msg: ws.send(json.dumps(msg)))
            health_ctx = HealthHandler(send_fn=lambda msg: ws.send(json.dumps(msg)))
            gitlab_runner_ctx = GitLabRunnerHandler(send_fn=lambda msg: ws.send(json.dumps(msg)))
            rollout_ctx = RolloutHandler(send_fn=lambda msg: ws.send(json.dumps(msg)))
            shell_ctx = ShellHandler(send_fn=lambda msg: ws.send(json.dumps(msg)))

            # Handshake enrichi avec hardware + cluster_info (Plan A)
            await ws.send(
                json.dumps({
                    "type": "handshake",
                    "agent_key": self.agent_key,
                    "cluster_token": self.cluster_token,
                    "capabilities": self.capabilities,
                    "hardware": _collect_hardware_info(),
                    "cluster_info": _detect_cluster_info(),
                })
            )

            # Attendre la confirmation
            raw = await ws.recv()
            ack = json.loads(raw)
            if ack.get("type") != "handshake_ack":
                logger.error(
                    "Handshake refusé: %s", ack.get("error", "réponse inattendue")
                )
                return

            logger.info(
                "Handshake réussi — org=%s fleet=%s",
                ack.get("organisation"), ack.get("fleet"),
            )

            # Lancer le heartbeat applicatif en arrière-plan
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

            try:
                # Boucle d'écoute des tâches entrantes
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        await ws.send(
                            json.dumps({"type": "pong", "ts": msg.get("ts")})
                        )
                        continue
                    if msg.get("type") not in ("task",):
                        # Ignorer les autres types (heartbeat_ack, etc.)
                        continue

                    task_type = msg.get("task_type", "")
                    args = msg.get("args", {})
                    task_id = msg.get("task_id")
                    command = msg.get("command", "")
                    task_timeout = msg.get("timeout", 300)

                    # Ajouter command + timeout aux args pour les handlers qui en ont besoin
                    if command:
                        args = dict(args, command=command)
                    if task_timeout:
                        args = dict(args, timeout=task_timeout)

                    # Mapping task_type → handler callable
                    _task_handlers: dict[str, object] = {
                        "helm_install": helm_ctx,
                        "helm_uninstall": helm_ctx,
                        "kubeconfig-upload": self,
                        "cluster_probe": probe_ctx,
                        "probe": probe_ctx,  # alias
                        "preflight_check": preflight_ctx,
                        "health_check": health_ctx,
                        "configure_runner": gitlab_runner_ctx,
                        "kubectl_rollout": rollout_ctx,
                        "shell": shell_ctx,
                    }

                    async def _dispatch_task(ttp: str, a_args: dict, a_task_id: str | None) -> None:
                        handler_obj = _task_handlers.get(ttp)
                        if handler_obj is None:
                            raise ValueError(f"task_type inconnu: {ttp}")

                        # Chaque handler a une méthode handle_<type> spécifique
                        method_map: dict[str, str] = {
                            "helm_install": "handle_install",
                            "helm_uninstall": "handle_uninstall",
                            "kubeconfig-upload": "_handle_kubeconfig_upload",
                            "cluster_probe": "handle_probe",
                            "probe": "handle_probe",
                            "preflight_check": "handle_preflight",
                            "health_check": "handle_health_check",
                            "configure_runner": "handle_configure",
                            "kubectl_rollout": "handle_rollout",
                            "shell": "handle_shell",
                        }
                        method_name = method_map.get(ttp)
                        if method_name is None:
                            raise ValueError(f"task_type inconnu: {ttp}")

                        method = getattr(handler_obj, method_name)
                        # Certains handlers prennent args + task_id, d'autres un seul payload
                        if ttp in ("helm_install", "helm_uninstall"):
                            stdout_lines: list[str] = []
                            stderr_lines: list[str] = []
                            async def _log(line: str, is_err: bool = False) -> None:
                                if is_err:
                                    stderr_lines.append(line)
                                else:
                                    stdout_lines.append(line)
                            await method(a_args, log_fn=_log)
                            await ws.send(json.dumps({
                                "type": "task_result",
                                "task_id": a_task_id,
                                "exit_code": 0,
                                "stdout": "\n".join(stdout_lines),
                                "stderr": "\n".join(stderr_lines),
                            }))
                        elif ttp == "kubeconfig-upload":
                            await method(a_args, a_task_id)
                            await ws.send(json.dumps({
                                "type": "task_result",
                                "task_id": a_task_id,
                                "exit_code": 0,
                                "stdout": "ok",
                                "stderr": "",
                            }))
                        elif ttp == "configure_runner":
                            # configure_runner envoie son propre résultat
                            await method(a_args)
                            await ws.send(json.dumps({
                                "type": "task_result",
                                "task_id": a_task_id,
                                "exit_code": 0,
                                "stdout": "",
                                "stderr": "",
                            }))
                        else:
                            # cluster_probe / preflight_check / health_check
                            # Ces handlers envoient eux-mêmes leur task_result
                            payload = dict(a_args, task_id=a_task_id)
                            await method(payload)

                    try:
                        await _dispatch_task(task_type, args, task_id)
                    except Exception as exc:
                        logger.exception("Erreur task %s", task_id)
                        await ws.send(
                            json.dumps({
                                "type": "task_result",
                                "task_id": task_id,
                                "exit_code": 1,
                                "stdout": "",
                                "stderr": str(exc),
                            })
                        )
            except websockets.ConnectionClosed as exc:
                logger.warning(
                    "Connexion fermée: code=%s reason=%s",
                    exc.code, getattr(exc, 'reason', ''),
                )
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        logger.info("Déconnecté")

    async def run(self) -> None:
        """Boucle principale avec reconnexion automatique.

        🟡 P1: Jitter aléatoire pour éviter le thundering herd.
        🟡 P1: Abandon après MAX_CONSECUTIVE_FAILURES tentatives consécutives.
        """
        delay = BASE_RECONNECT_DELAY
        consecutive_failures = 0
        while True:
            try:
                await self._connect_once()
                # Réinitialiser le compteur après une connexion réussie
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                logger.error(
                    "Erreur de connexion (%d/%d): %s",
                    consecutive_failures,
                    MAX_CONSECUTIVE_FAILURES,
                    exc,
                    exc_info=True,
                )

            # 🟡 P1: Abandon après trop d'échecs consécutifs
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.critical(
                    "Échec de connexion après %d tentatives consécutives. Arrêt.",
                    MAX_CONSECUTIVE_FAILURES,
                )
                return  # Le processus s'arrête, systemd redémarrera

            # 🟡 P1: Jitter aléatoire pour éviter le thundering herd
            jitter = random.uniform(0, 2)  # 0-2s aléatoire
            total_delay = delay + jitter

            logger.info(
                "Reconnexion dans %.1fs... (tentative %d, appuyez sur Ctrl+C pour quitter)",
                total_delay,
                consecutive_failures,
            )
            await asyncio.sleep(total_delay)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)


# ── Point d'entrée déplacé dans cli.py ────────────────────────────────────────
# La fonction main() a été déplacée vers simforge_agent.cli.main.
# Le point d'entrée pyproject.toml pointe désormais vers simforge_agent.cli:main.
