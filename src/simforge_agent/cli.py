"""CLI de l'agent SimDevForge — point d'entrée unique.

Usage :
  simforge-agent enroll <server_url> <enrollment_token>   ← configuration (1 fois)
  simforge-agent start                                     ← connexion WebSocket
  simforge-agent status                                    ← statut
  simforge-agent stop                                      ← arrêt

Rétrocompatibilité :
  simforge-agent <server_url> <agent_key> <cluster_token>  ← démarrage direct
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from typing import NoReturn

import httpx

from simforge_agent.client import AgentClient
from simforge_agent.config import (
    AgentConfig,
    get_status_dict,
    load as load_config,
    save as save_config,
    validate_enrollment_token,
    validate_server_url,
)

logger = logging.getLogger(__name__)

# ── Logging ───────────────────────────────────────────────────────────────────


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ── Commandes ─────────────────────────────────────────────────────────────────


def cmd_enroll(args: argparse.Namespace) -> None:
    """Enrôle l'agent auprès de la plateforme : POST /api/agents/register/ → config.json."""
    server_url = args.server_url.rstrip("/")
    enrollment_token = args.enrollment_token

    # Validation
    err = validate_server_url(server_url)
    if err:
        print(f"✖ {err}", file=sys.stderr)
        sys.exit(1)

    # 🟡 P1: Forcer HTTPS en production (root)
    if os.geteuid() == 0 and server_url.startswith("http://"):
        print(
            "✖ HTTP non autorisé en production. Utilisez HTTPS (wss://).",
            file=sys.stderr,
        )
        sys.exit(1)
    err = validate_enrollment_token(enrollment_token)
    if err:
        print(f"✖ {err}", file=sys.stderr)
        sys.exit(1)

    # Collecte infos hardware
    hardware = _collect_hardware()

    # Appel API
    register_url = f"{server_url}/api/agents/register/"
    print(f"→ Enrôlement auprès de {server_url}...")

    from simforge_agent.config import ca_bundle, insecure_tls

    verify: bool | str = ca_bundle() or not insecure_tls()
    if verify is False:
        print("⚠ SIMFORGE_AGENT_INSECURE=1 — vérification TLS désactivée (dev)")

    try:
        resp = httpx.post(
            register_url,
            json={
                "enrollment_token": enrollment_token,
                "name": hardware.get("hostname", ""),
                "hardware": hardware,
            },
            timeout=30,
            verify=verify,
        )
    except httpx.RequestError as exc:
        print(f"✖ Impossible de joindre le serveur : {exc}", file=sys.stderr)
        sys.exit(2)

    if resp.status_code == 409:
        print(
            "✖ Token déjà utilisé. Générez-en un nouveau depuis l'interface "
            "Fleets → Régénérer le token.",
            file=sys.stderr,
        )
        sys.exit(1)
    if resp.status_code == 410:
        print("✖ Token expiré. Générez-en un nouveau.", file=sys.stderr)
        sys.exit(1)
    if resp.status_code == 404:
        print("✖ Token invalide. Vérifiez votre token d'enrollment.", file=sys.stderr)
        sys.exit(1)
    if not resp.is_success:
        print(
            f"✖ Erreur serveur ({resp.status_code}) : {resp.text}",
            file=sys.stderr,
        )
        sys.exit(2)

    data = resp.json()

    # Construction de la config
    config = AgentConfig(
        server_url=server_url,
        agent_id=data["agent_id"],
        agent_key=data["agent_key"],
        agent_token=data["agent_jwt"],
        cluster_token=data["cluster_token"],
        fleet_id=data["fleet_id"],
        organisation_slug=data["organisation_slug"],
        # 30s < timeout idle proxy Kong (60s) → évite le flap déconnexion/reco.
        heartbeat_interval=30,
    )

    path = save_config(config)

    print("✓ Enrôlement réussi !")
    print(f"  Agent ID      : {config.agent_id}")
    print(f"  Organisation  : {config.organisation_slug}")
    print(f"  Fleet ID      : {config.fleet_id}")
    print(f"  Config        : {path}")
    print("")
    print("  Prochaine étape : simforge-agent start")


def cmd_start(args: argparse.Namespace) -> None:
    """Démarre la connexion WebSocket avec la plateforme.

    Ordre de résolution (du plus prioritaire au moins) :
      1. Args CLI positionnels (rétrocompat)
      2. Args CLI optionnels (--hub-url, --agent-key, --cluster-token)
      3. config.json
    """
    # Priorité 1: args positionnels (rétrocompat — 3 args)
    server_url = getattr(args, "server_url", None) or ""
    agent_key = getattr(args, "agent_key", None) or ""
    cluster_token = getattr(args, "cluster_token", None) or ""

    if server_url and agent_key and cluster_token:
        logger.info("Mode rétrocompatible : connexion directe")
        _run_client(server_url, agent_key, cluster_token)
        return

    # Priorité 2: config.json
    config = load_config()
    if config is None:
        print(
            "✖ Aucune configuration trouvée. Lancez d'abord :\n"
            "    simforge-agent enroll <server_url> <enrollment_token>",
            file=sys.stderr,
        )
        sys.exit(1)

    # Priorité 3: args optionnels (--hub-url, --agent-key, --cluster-token)
    # priment sur config.json s'ils sont fournis
    hub_url = getattr(args, "hub_url", None) or config.server_url
    agent_key_opt = getattr(args, "agent_key", None) or config.agent_key
    cluster_token_opt = getattr(args, "cluster_token", None) or config.cluster_token

    logger.info(
        "Connexion à %s (agent %s, organisation %s)",
        hub_url,
        config.agent_id,
        config.organisation_slug,
    )

    _run_client(
        server_url=hub_url,
        agent_key=agent_key_opt,
        cluster_token=cluster_token_opt,
        heartbeat_interval=config.heartbeat_interval,
    )


def cmd_status(args: argparse.Namespace) -> None:
    """Affiche le statut de l'agent."""
    status = get_status_dict()

    print("═" * 50)
    print("  SimDevForge Agent — Statut")
    print("═" * 50)

    if not status["configured"]:
        print(f"\n  {status['message']}")
        print()
        return

    connected_emoji = "🟢" if status["connected"] else "🔴"
    print(f"\n  {connected_emoji} Statut    : {'Connecté' if status['connected'] else 'Déconnecté'}")
    if status["pid"]:
        print(f"  PID        : {status['pid']}")
    print(f"  Serveur    : {status['server_url']}")
    print(f"  Agent ID   : {status['agent_id']}")
    print(f"  Fleet ID   : {status['fleet_id']}")
    print(f"  Org        : {status['organisation']}")
    print(f"  Config     : {status['config_path']}")
    print(f"  Enrôlé le  : {status['enrolled_at']}")
    if status["last_connected_at"]:
        print(f"  Dernière   : {status['last_connected_at']}")
    print()


def cmd_stop(args: argparse.Namespace) -> None:
    """Arrête le processus agent en cours."""
    try:
        result = subprocess.run(
            ["pkill", "-f", "simforge-agent start"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print("✓ Agent arrêté.")
        else:
            print("ℹ Aucun processus agent trouvé.")
    except FileNotFoundError:
        print("✖ pkill non disponible. Tue manuellement le processus.", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("✖ Délai dépassé pour l'arrêt.", file=sys.stderr)
        sys.exit(1)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _collect_hardware() -> dict:
    """Collecte les infos hardware du serveur pour l'enrôlement."""
    import platform as _platform

    info: dict = {
        "hostname": _platform.node(),
        "os_name": _platform.system(),
        "os_version": _platform.release(),
        "arch": _platform.machine(),
        "cpu_brand": "",
        "cpu_cores": 0,
        "ram_total_gb": 0.0,
    }

    # CPU cores (multi-OS)
    try:
        import os as _os
        info["cpu_cores"] = _os.cpu_count() or 0
    except Exception:
        pass

    # RAM (Linux uniquement)
    if sys.platform == "linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        info["ram_total_gb"] = round(kb / (1024 * 1024), 1)
                        break
        except Exception:
            pass
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        info["cpu_brand"] = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass

    return info


def _run_client(
    server_url: str,
    agent_key: str,
    cluster_token: str,
    heartbeat_interval: int = 30,
) -> NoReturn:
    """Lance la boucle AgentClient (ne retourne jamais)."""
    try:
        asyncio.run(
            AgentClient(
                server_url=server_url,
                agent_key=agent_key,
                cluster_token=cluster_token,
                heartbeat_interval=heartbeat_interval,
            ).run()
        )
    except KeyboardInterrupt:
        logger.info("Arrêt demandé par l'utilisateur")
        sys.exit(0)
    except Exception as exc:
        logger.exception("Erreur fatale : %s", exc)
        sys.exit(3)


# ── Parser argparse ───────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simforge-agent",
        description="Agent SimDevForge — connexion sécurisée à la plateforme.",
        epilog=(
            "Documentation : https://github.com/your-org/simdevforge\n"
            "Commandes principales :\n"
            "  simforge-agent enroll <url> <token>   Enrôlement (1 fois)\n"
            "  simforge-agent start                   Connexion WebSocket\n"
            "  simforge-agent status                   Statut\n"
            "  simforge-agent stop                     Arrêt"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Logs détaillés (debug)",
    )

    sub = parser.add_subparsers(dest="command", metavar="<commande>")
    sub.required = False

    # ── enroll ────────────────────────────────────────────────────────────
    p_enroll = sub.add_parser(
        "enroll",
        help="Enrôler l'agent auprès de la plateforme (1 fois)",
        description="Envoie le token d'enrollment, reçoit les credentials, et écrit config.json.",
    )
    p_enroll.add_argument("server_url", help="URL du backend SimDevForge (ex: https://hub.example.com)")
    p_enroll.add_argument("enrollment_token", help="Token d'enrollment (récupéré depuis l'UI Fleets)")

    # ── start ─────────────────────────────────────────────────────────────
    p_start = sub.add_parser(
        "start",
        help="Démarrer la connexion WebSocket",
        description="Lit config.json et lance la connexion WebSocket avec heartbeat. "
                    "Les arguments optionnels (--hub-url, --agent-key, --cluster-token) "
                    "priment sur les valeurs de config.json.",
    )
    p_start.add_argument(
        "--hub-url",
        help="URL du serveur hub (ex: https://hub.example.com) — prime sur config.json",
    )
    p_start.add_argument(
        "--agent-key",
        help="Clé d'agent — prime sur config.json",
    )
    p_start.add_argument(
        "--cluster-token",
        help="Token de cluster — prime sur config.json",
    )

    # ── status ────────────────────────────────────────────────────────────
    sub.add_parser(
        "status",
        help="Afficher le statut de l'agent",
        description="Indique si l'agent est configuré, connecté, et ses métadonnées.",
    )

    # ── stop ──────────────────────────────────────────────────────────────
    sub.add_parser(
        "stop",
        help="Arrêter l'agent",
        description="Tue le processus simforge-agent start en cours.",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    """Point d'entrée principal — dispatch vers la bonne commande.

    Supporte la rétrocompatibilité :
      simforge-agent <server_url> <agent_key> <cluster_token>
    """
    _argv = sys.argv[1:] if argv is None else list(argv)

    # ── Rétrocompatibilité : 3 args positionnels → start direct ────────────
    # On extrait les flags pour vérifier si le premier non-flag est une URL
    _positional_args = [a for a in _argv if not a.startswith("-")]
    if (
        len(_positional_args) >= 3
        and _positional_args[0] not in ("enroll", "start", "status", "stop")
    ):
        print(
            "ℹ Mode rétrocompatible : simforge-agent <url> <key> <token>",
            file=sys.stderr,
        )
        _setup_logging(verbose=("-v" in _argv or "--verbose" in _argv))
        cmd_start(
            argparse.Namespace(
                server_url=_positional_args[0],
                agent_key=_positional_args[1],
                cluster_token=_positional_args[2],
                verbose="-v" in _argv or "--verbose" in _argv,
            )
        )
        return

    parser = _build_parser()
    args = parser.parse_args(_argv)

    _setup_logging(args.verbose if hasattr(args, "verbose") else False)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Dispatch
    command_map = {
        "enroll": cmd_enroll,
        "start": cmd_start,
        "status": cmd_status,
        "stop": cmd_stop,
    }

    handler = command_map.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)
