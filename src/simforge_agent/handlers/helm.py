"""Handler Helm : install / uninstall via `helm upgrade --install` et `helm uninstall`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# ── Validation des arguments Helm (sécurité — pas d'injection CLI) ────────────

_CHART_RE = re.compile(r"^[a-z0-9][a-z0-9._/@-]*$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_VERSION_RE = re.compile(r"^[0-9][0-9a-zA-Z.\-+]*$")


class HelmArgError(ValueError):
    """Argument Helm invalide ou dangereux."""


def _validate_helm_arg(value: str, field: str) -> None:
    """Rejette toute valeur qui pourrait injecter des flags CLI dans Helm.

    Lève HelmArgError si la valeur est vide, commence par ``-``, ou ne
    correspond pas au pattern attendu pour le champ donné.
    """
    if not value:
        raise HelmArgError(f"{field}: valeur vide")
    if value.startswith("-"):
        raise HelmArgError(f"{field}: ne peut pas commencer par '-'")
    _patterns = {
        "chart": _CHART_RE,
        "release": _NAME_RE,
        "namespace": _NAME_RE,
        "version": _VERSION_RE,
    }
    pattern = _patterns.get(field)
    if pattern and not pattern.match(value):
        raise HelmArgError(f"{field}: caractères invalides dans '{value}'")

def _normalize_helm_timeout(value: object) -> str:
    """Convertit le timeout en format duration Helm (ex: "360s", "5m").

    Le backend envoie ``timeout_seconds`` en int ; subprocess exige des str
    dans argv — un int brut lève TypeError au lancement de helm.
    """
    if isinstance(value, bool) or value is None:
        return "5m"
    if isinstance(value, (int, float)):
        return f"{int(value)}s"
    return str(value) or "5m"


SendFn = Callable[[dict], Awaitable[None]]
"""Envoie un message JSON vers le backend via WebSocket (logs temps réel)."""

LogFn = Callable[[str, bool], Awaitable[None]]
"""Callback de log : (line, is_error) — utilisé par le client pour capturer stdout/stderr."""


def _resolve_kubeconfig() -> list[str]:
    """Retourne les flags kubeconfig si nécessaire. Vide = in-cluster."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return []
    kubeconfig = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube" / "config")
    if Path(kubeconfig).exists():
        return ["--kubeconfig", kubeconfig]
    return []


class HelmHandler:
    """Gère les commandes Helm (install, uninstall) et streame les logs via WebSocket."""

    def __init__(self, send_fn: SendFn) -> None:
        self._send = send_fn

    async def _run_kubectl(self, cmd: list[str]) -> tuple[int, str, str]:
        """Exécute kubectl et retourne (returncode, stdout, stderr)."""
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

    async def _create_oidc_secret(self, namespace: str, oidc_config: dict) -> None:
        """Crée le Secret K8s gitlab-oidc-provider avant helm install."""
        import json as _json

        provider_json = _json.dumps({
            "name": "openid_connect",
            "label": "SimDevForge SSO",
            "args": {
                "name": "openid_connect",
                "scope": ["openid", "profile", "email"],
                "response_type": "code",
                "issuer": oidc_config["issuer"],
                "discovery": True,
                "client_auth_method": "query",
                "uid_field": "sub",
                "client_options": {
                    "identifier": oidc_config["client_id"],
                    "secret": oidc_config["client_secret"],
                    "redirect_uri": oidc_config["redirect_uri"],
                },
            },
        })

        secret_manifest = _json.dumps({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "gitlab-oidc-provider",
                "namespace": namespace,
            },
            "stringData": {
                "provider": provider_json,
            },
        })

        kf = _resolve_kubeconfig()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            tmp.write(secret_manifest)
            tmp_path = tmp.name
        try:
            rc, _, stderr = await self._run_kubectl([
                "kubectl", "apply", "-f", tmp_path, *kf,
            ])
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if rc != 0:
            raise RuntimeError(f"Impossible de créer le Secret OIDC: {stderr}")
        logger.info("Secret gitlab-oidc-provider créé dans namespace %s", namespace)

    async def _add_helm_repo(self, repo_url: str) -> None:
        """Ajoute ou met à jour un repo Helm."""
        # Extraire le nom du repo depuis l'URL ou un nom par défaut
        repo_name = "simforge"
        if "://" in repo_url:
            repo_name = repo_url.rstrip("/").split("/")[-1]
            repo_name = re.sub(r"[^a-zA-Z0-9_-]", "", repo_name) or "simforge"

        proc = await asyncio.create_subprocess_exec(
            "helm", "repo", "add", repo_name, repo_url,
            "--force-update",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode and proc.returncode != 0:
            logger.warning("helm repo add %s: %s", repo_url, stderr.decode(errors="replace").strip())
        return repo_name

    async def _exec_command_fallback(
        self, command: str, log_fn: LogFn | None = None
    ) -> None:
        """Fallback : exécute la commande shell brute (tâches legacy)."""
        logger.warning("HelmHandler utilisant command fallback (legacy)")
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async def _forward(stream: asyncio.StreamReader, is_err: bool) -> None:
            while True:
                raw_line = await stream.readline()
                if not raw_line:
                    break
                line = raw_line.decode(errors="replace").rstrip()
                if log_fn:
                    await log_fn(line, is_err=is_err)
        await asyncio.gather(
            _forward(process.stdout, False),
            _forward(process.stderr, True),
        )
        await process.wait()

    async def handle_install(
        self, payload: dict, log_fn: LogFn | None = None
    ) -> None:
        """Lance ``helm upgrade --install`` et streame la sortie.

        Deux modes de fonctionnement :
        1. **Args structurés** (recommandé) :
            service_install_id (str) — identifiant de l'installation
            chart (str) — ref du chart Helm (ex: ``jenkins/jenkins``)
            chart_version (str, optionnel) — version du chart
            release_name (str) — nom de la release Helm
            namespace (str, optionnel) — namespace cible (défaut: "default")
            values (dict, optionnel) — valeurs Helm au format JSON
            repo_url (str, optionnel) — URL du repo Helm (sera ajouté automatiquement)
            oidc_config (dict, optionnel) — config OIDC pour créer le Secret K8s avant install
            timeout (str, optionnel) — timeout helm (défaut: "5m")

        2. **Fallback command** (legacy) :
            command (str) — commande shell complète ``helm repo add ...; helm upgrade --install ...``
        """
        # ── Fallback : si pas de chart structuré, utiliser command ─────
        if not payload.get("chart") and payload.get("command"):
            await self._exec_command_fallback(payload["command"], log_fn=log_fn)
            return

        service_install_id = payload.get("service_install_id", "")
        chart = payload["chart"]
        version = payload.get("chart_version", "")
        release = payload.get("release_name", "")
        namespace = payload.get("namespace", "default")
        values = payload.get("values", {})
        repo_url = payload.get("repo_url", "")
        helm_timeout = _normalize_helm_timeout(payload.get("timeout", "5m"))

        # Normalisation : certains backends envoient `app_name` → utiliser comme release
        if not release:
            release = payload.get("app_name", "")

        # Créer le Secret OIDC si présent dans le payload (GitLab uniquement)
        oidc_config = payload.get("oidc_config")
        if oidc_config:
            await self._create_oidc_secret(namespace, oidc_config)

        # Validation sécurité
        try:
            _validate_helm_arg(chart, "chart")
            _validate_helm_arg(release, "release")
            _validate_helm_arg(namespace, "namespace")
            if version:
                _validate_helm_arg(version, "version")
        except HelmArgError as exc:
            await self._send({
                "type": "helm_result",
                "service_install_id": service_install_id,
                "success": False,
                "error": f"Argument invalide: {exc}",
            })
            return

        if not shutil.which("helm"):
            msg = "helm non trouvé sur cette machine."
            await self._send({
                "type": "helm_result",
                "service_install_id": service_install_id,
                "success": False,
                "error": msg,
            })
            if log_fn:
                await log_fn(msg, is_err=True)
            raise RuntimeError(msg)

        # Ajouter le repo Helm si fourni
        if repo_url:
            repo_name = await self._add_helm_repo(repo_url)
            # Si le chart est simple (ex: "jenkins/jenkins"), utilise le repo ajouté
            if "/" in chart and not chart.startswith("oci://"):
                pass  # chart reference already includes repo name
            elif "/" not in chart and repo_name:
                chart = f"{repo_name}/{chart}"

        cmd = [
            "helm", "upgrade", "--install",
            release, chart,
            "--namespace", namespace,
            "--create-namespace",
            "--wait", "--timeout", helm_timeout,
            *_resolve_kubeconfig(),
        ]
        if version:
            cmd += ["--version", version]
        if values:
            cmd += ["--set-json", json.dumps(values)]

        await self._stream(service_install_id, cmd, log_fn=log_fn)

    async def handle_uninstall(
        self, payload: dict, log_fn: LogFn | None = None
    ) -> None:
        """Lance ``helm uninstall`` et streame la sortie.

        Deux modes de fonctionnement :
        1. **Args structurés** :
            service_install_id (str) — identifiant de l'installation
            release_name (str) — nom de la release Helm
            namespace (str, optionnel) — namespace cible

        2. **Fallback command** (legacy) :
            command (str) — commande shell complète
        """
        # ── Fallback : si pas de release structurée, utiliser command ──
        if not payload.get("release_name") and payload.get("command"):
            await self._exec_command_fallback(payload["command"], log_fn=log_fn)
            return

        service_install_id = payload.get("service_install_id", "")
        release = payload.get("release_name", payload.get("app_name", ""))
        namespace = payload.get("namespace", "default")

        try:
            _validate_helm_arg(release, "release")
            _validate_helm_arg(namespace, "namespace")
        except HelmArgError as exc:
            await self._send({
                "type": "helm_result",
                "service_install_id": service_install_id,
                "success": False,
                "error": f"Argument invalide: {exc}",
            })
            return

        cmd = [
            "helm", "uninstall", release,
            "--namespace", namespace,
            *_resolve_kubeconfig(),
        ]
        await self._stream(service_install_id, cmd, log_fn=log_fn)

    async def _stream(
        self,
        service_install_id: str,
        cmd: list[str],
        log_fn: LogFn | None = None,
    ) -> None:
        """Exécute une commande et streame stdout ligne par ligne."""
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _forward(
            stream: asyncio.StreamReader, is_err: bool
        ) -> None:
            while True:
                raw_line = await stream.readline()
                if not raw_line:
                    break
                line = raw_line.decode(errors="replace").rstrip()
                await self._send({
                    "type": "helm_log",
                    "service_install_id": service_install_id,
                    "line": line,
                    "stream": "stderr" if is_err else "stdout",
                })
                if log_fn:
                    await log_fn(line, is_err=is_err)

        await asyncio.gather(
            _forward(process.stdout, is_err=False),
            _forward(process.stderr, is_err=True),
        )

        await process.wait()
        success = process.returncode == 0
        await self._send({
            "type": "helm_result",
            "service_install_id": service_install_id,
            "success": success,
            "error": "" if success else f"helm exit code {process.returncode}",
        })
