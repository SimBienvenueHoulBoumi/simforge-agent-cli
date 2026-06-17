"""Gestion du fichier de configuration persistant de l'agent.

Stocké dans /etc/simforge-agent/config.json (root) ou
~/.config/simforge-agent/config.json (utilisateur non-root).
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR_ROOT = Path("/etc/simforge-agent")
CONFIG_DIR_USER = Path.home() / ".config" / "simforge-agent"
CONFIG_FILENAME = "config.json"
VALID_URL_RE = re.compile(r"^https?://[a-zA-Z0-9._:-]+")


def ca_bundle() -> str | None:
    """Chemin du CA bundle custom (SIMFORGE_AGENT_CA_BUNDLE), si défini et lisible.

    Pourquoi : la plateforme dev sert un certificat auto-signé ; la bonne
    pratique est de vérifier contre son CA plutôt que de désactiver TLS.
    """
    path = os.environ.get("SIMFORGE_AGENT_CA_BUNDLE", "")
    if path and Path(path).is_file():
        return path
    return None


def insecure_tls() -> bool:
    """Vrai si SIMFORGE_AGENT_INSECURE est activé (dev uniquement, dernier recours).

    Préférer SIMFORGE_AGENT_CA_BUNDLE. Cette porte de sortie explicite
    n'existe que pour le dev local ; jamais activée par défaut, et ignorée
    si un CA bundle est fourni.
    """
    if ca_bundle():
        return False
    return os.environ.get("SIMFORGE_AGENT_INSECURE", "").lower() in {"1", "true", "yes"}


@dataclass
class AgentConfig:
    """Configuration persistante de l'agent — mappe 1:1 avec config.json."""

    server_url: str
    agent_id: str
    agent_key: str
    agent_token: str
    cluster_token: str
    fleet_id: str
    organisation_slug: str
    heartbeat_interval: int = 60
    enrolled_at: str = ""
    last_connected_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentConfig:
        return cls(
            server_url=data["server_url"],
            agent_id=data["agent_id"],
            agent_key=data.get("agent_key", ""),
            agent_token=data.get("agent_token", ""),
            cluster_token=data.get("cluster_token", ""),
            fleet_id=data.get("fleet_id", ""),
            organisation_slug=data.get("organisation_slug", ""),
            heartbeat_interval=data.get("heartbeat_interval", 60),
            enrolled_at=data.get("enrolled_at", ""),
            last_connected_at=data.get("last_connected_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Chemins ───────────────────────────────────────────────────────────────────


def _config_dir() -> Path:
    """Retourne le dossier de config : /etc/simforge-agent/ si root, ~/.config/ sinon."""
    if os.geteuid() == 0:
        path = CONFIG_DIR_ROOT
    else:
        path = CONFIG_DIR_USER
    path.mkdir(parents=True, exist_ok=True)
    return path


def _config_path() -> Path:
    """Chemin complet du fichier config.json."""
    return _config_dir() / CONFIG_FILENAME


# ── Opérations ────────────────────────────────────────────────────────────────


def _set_secure_permissions(path: Path) -> None:
    """🔴 P0: Forcer les permissions à 600 (root only) si on est root.

    Le fichier config.json contient des tokens sensibles (agent_key,
    agent_token JWT, cluster_token). En production (root), seul root
    doit pouvoir le lire.
    """
    if os.geteuid() == 0:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            logger.warning("Impossible de fixer les permissions sur %s : %s", path, exc)


def _check_permissions(path: Path) -> None:
    """Log un warning si les permissions sont trop permissives."""
    if os.geteuid() == 0 and path.exists():
        try:
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning(
                    "Permissions permissives sur %s: %o — devrait être 600",
                    path,
                    mode,
                )
        except OSError:
            pass


def load() -> AgentConfig | None:
    """Lit config.json. Retourne None si absent ou invalide."""
    path = _config_path()
    if not path.exists():
        logger.debug("Aucun fichier config.json trouvé à %s", path)
        return None
    _check_permissions(path)
    try:
        raw = path.read_text()
        data: dict[str, Any] = json.loads(raw)
        return AgentConfig.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning("config.json invalide à %s : %s", path, exc)
        return None


def save(cfg: AgentConfig) -> Path:
    """Écrit config.json. Retourne le chemin."""
    path = _config_path()
    if not cfg.enrolled_at:
        cfg.enrolled_at = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))
    tmp.replace(path)
    _set_secure_permissions(path)
    logger.info("Config sauvegardée dans %s", path)
    return path


def delete() -> bool:
    """Supprime config.json. Retourne True si supprimé."""
    path = _config_path()
    if path.exists():
        path.unlink()
        logger.info("Config supprimée : %s", path)
        return True
    return False


# ── Statut (pour la commande `status`) ────────────────────────────────────────


def get_status_dict() -> dict[str, Any]:
    """Retourne un dict décrivant l'état actuel de l'agent."""
    cfg = load()
    if cfg is None:
        return {"configured": False, "message": "Agent non configuré. Lancez 'simforge-agent enroll'."}

    # Détection si le processus tourne
    running = False
    pid: str | None = None
    try:
        result = subprocess.run(
            ["pgrep", "-f", "simforge-agent start"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
            if pids:
                running = True
                pid = pids[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    config_path = _config_path()
    return {
        "configured": True,
        "server_url": cfg.server_url,
        "agent_id": cfg.agent_id,
        "fleet_id": cfg.fleet_id,
        "organisation": cfg.organisation_slug,
        "connected": running,
        "pid": pid,
        "enrolled_at": cfg.enrolled_at,
        "last_connected_at": cfg.last_connected_at,
        "config_path": str(config_path),
    }


def validate_enrollment_token(token: str) -> str | None:
    """Valide un token d'enrollment. Retourne None si valide, un message d'erreur sinon."""
    if not token:
        return "Token d'enrollment requis."
    if len(token) < 8:
        return "Token d'enrollment trop court (< 8 caractères)."
    return None


def validate_server_url(url: str) -> str | None:
    """Valide une URL de serveur. Retourne None si valide, un message d'erreur sinon."""
    if not url:
        return "URL du serveur requise."
    if not VALID_URL_RE.match(url):
        return "URL invalide. Format attendu : http://... ou https://..."
    return None
