"""Tests pour le module cli.py — CLI de l'agent.

Teste le parsing des arguments, la validation, et le dispatch.
Les appels réseau (httpx) sont mockés.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from simforge_agent.cli import main
from simforge_agent.config import AgentConfig


class TestHelp:
    """Tests de l'affichage d'aide."""

    def test_help_exit_code(self):
        """simforge-agent --help → exit 0."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_no_args_exit(self):
        """simforge-agent sans argument → exit 1 (aucune commande)."""
        with pytest.raises(SystemExit) as exc:
            main([])
        # Si aucune commande donnée, notre code fait sys.exit(1)
        assert exc.value.code == 1

    def test_unknown_command(self):
        """simforge-agent <inconnu> → exit 2 (argparse invalid choice)."""
        with pytest.raises(SystemExit) as exc:
            main(["unknown-cmd"])
        assert exc.value.code == 2


class TestEnrollValidation:
    """Tests de validation des arguments de la commande enroll."""

    def test_enroll_missing_server_url(self):
        """enroll sans URL → erreur."""
        with pytest.raises(SystemExit) as exc:
            main(["enroll", "", "valid-token-here"])
        assert exc.value.code == 1

    def test_enroll_invalid_url(self):
        """enroll avec URL invalide → erreur."""
        with pytest.raises(SystemExit) as exc:
            main(["enroll", "not-a-url", "valid-token-here"])
        assert exc.value.code == 1

    def test_enroll_missing_token(self):
        """enroll sans token → erreur."""
        with pytest.raises(SystemExit) as exc:
            main(["enroll", "https://hub.example.com", ""])
        assert exc.value.code == 1

    def test_enroll_short_token(self):
        """enroll avec token trop court → erreur."""
        with pytest.raises(SystemExit) as exc:
            main(["enroll", "https://hub.example.com", "abc"])
        assert exc.value.code == 1

    @patch("simforge_agent.cli.httpx.post")
    def test_enroll_success(self, mock_post, tmp_path: Path):
        """enroll réussi → écrit config.json."""
        # Mock de la réponse API
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "agent_key": "key-abc",
            "agent_jwt": "jwt-xyz",
            "agent_id": "agent-uuid",
            "cluster_token": "cluster-tok",
            "fleet_id": "fleet-uuid",
            "organisation_slug": "test-org",
        }
        mock_post.return_value = mock_resp

        # Patching de _config_dir pour utiliser tmp_path
        with patch("simforge_agent.config._config_dir", return_value=tmp_path):
            main(["enroll", "https://hub.example.com", "valid-enrollment-token"])

        # Vérifie que config.json a été écrit
        config_file = tmp_path / "config.json"
        assert config_file.exists()
        import json

        data = json.loads(config_file.read_text())
        assert data["server_url"] == "https://hub.example.com"
        assert data["agent_id"] == "agent-uuid"
        assert data["agent_key"] == "key-abc"
        assert data["agent_token"] == "jwt-xyz"
        assert data["organisation_slug"] == "test-org"

        # Vérifie l'appel API
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["json"]["enrollment_token"] == "valid-enrollment-token"
        assert "hardware" in call_kwargs["json"]
        assert call_kwargs["json"]["hardware"]["hostname"] != ""

    @patch("simforge_agent.cli.httpx.post")
    def test_enroll_token_already_used(self, mock_post):
        """enroll avec token déjà utilisé → exit 1."""
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.is_success = False
        mock_post.return_value = mock_resp

        with pytest.raises(SystemExit) as exc:
            main(["enroll", "https://hub.example.com", "used-token"])
        assert exc.value.code == 1

    @patch("simforge_agent.cli.httpx.post")
    def test_enroll_token_expired(self, mock_post):
        """enroll avec token expiré → exit 1."""
        mock_resp = MagicMock()
        mock_resp.status_code = 410
        mock_resp.is_success = False
        mock_post.return_value = mock_resp

        with pytest.raises(SystemExit) as exc:
            main(["enroll", "https://hub.example.com", "expired-token"])
        assert exc.value.code == 1

    @patch("simforge_agent.cli.httpx.post")
    def test_enroll_server_error(self, mock_post):
        """enroll avec erreur serveur 500 → exit 2."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.is_success = False
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp

        with pytest.raises(SystemExit) as exc:
            main(["enroll", "https://hub.example.com", "some-token"])
        assert exc.value.code == 2


class TestStartCommand:
    """Tests de la commande start."""

    def test_start_no_config(self):
        """start sans config.json → exit 1."""
        with (
            patch("simforge_agent.cli.load_config", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            main(["start"])
        assert exc.value.code == 1

    @patch("simforge_agent.cli._run_client")
    def test_start_with_config(self, mock_run):
        """start avec une config valide → lance le client."""
        cfg = AgentConfig(
            server_url="https://hub.example.com",
            agent_id="agent-uuid",
            agent_key="key-abc",
            agent_token="jwt-xyz",
            cluster_token="cluster-tok",
            fleet_id="fleet-uuid",
            organisation_slug="test-org",
        )
        with patch("simforge_agent.cli.load_config", return_value=cfg):
            main(["start"])

        mock_run.assert_called_once_with(
            server_url="https://hub.example.com",
            agent_key="key-abc",
            cluster_token="cluster-tok",
            heartbeat_interval=60,
        )


class TestBackwardCompat:
    """Tests de la rétrocompatibilité : simforge-agent <url> <key> <token>."""

    @patch("simforge_agent.cli.cmd_start")
    def test_three_positional_args(self, mock_start):
        """3 args positionnels → mode rétrocompatible."""
        main(["https://hub.example.com", "key-abc", "cluster-tok"])

        mock_start.assert_called_once()
        ns = mock_start.call_args[0][0]
        assert ns.server_url == "https://hub.example.com"
        assert ns.agent_key == "key-abc"
        assert ns.cluster_token == "cluster-tok"

    @patch("simforge_agent.cli.cmd_start")
    def test_three_args_with_verbose(self, mock_start):
        """3 args + --verbose → mode rétrocompatible avec verbose."""
        main(["--verbose", "https://hub.example.com", "key-abc", "cluster-tok"])

        mock_start.assert_called_once()


class TestStatusCommand:
    """Tests de la commande status."""

    def test_status_no_config(self):
        """status sans config → message 'non configuré'."""
        with (
            patch("simforge_agent.cli.get_status_dict") as mock_status,
        ):
            mock_status.return_value = {
                "configured": False,
                "message": "Agent non configuré. Lancez 'simforge-agent enroll'.",
            }
            # Ne doit pas exit avec erreur
            main(["status"])
            mock_status.assert_called_once()

    def test_status_configured(self):
        """status avec config → affiche les infos."""
        with (
            patch("simforge_agent.cli.get_status_dict") as mock_status,
        ):
            mock_status.return_value = {
                "configured": True,
                "server_url": "https://hub.example.com",
                "agent_id": "agent-uuid",
                "fleet_id": "fleet-uuid",
                "organisation": "test-org",
                "connected": True,
                "pid": "12345",
                "enrolled_at": "2026-06-01T10:00:00Z",
                "last_connected_at": None,
                "config_path": "/etc/simforge-agent/config.json",
            }
            main(["status"])
            mock_status.assert_called_once()


class TestStopCommand:
    """Tests de la commande stop."""

    @patch("simforge_agent.cli.subprocess.run")
    def test_stop_success(self, mock_run):
        """stop avec processus trouvé → message succès."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        main(["stop"])
        mock_run.assert_called_once()

    @patch("simforge_agent.cli.subprocess.run")
    def test_stop_no_process(self, mock_run):
        """stop sans processus → pas d'erreur."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        main(["stop"])
        mock_run.assert_called_once()


class TestRunClientExitCodes:
    """Contrat de code de sortie de _run_client (sémantique de relance systemd).

    - Arrêt volontaire (run() se termine / annulé par SIGTERM) → exit 0.
    - Erreur fatale → exit 3 (systemd relance).
    """

    @patch("simforge_agent.cli.AgentClient")
    def test_arret_propre_exit_0(self, mock_client_cls):
        async def _noop():
            return None

        mock_client_cls.return_value.run.return_value = _noop()
        with pytest.raises(SystemExit) as exc:
            from simforge_agent.cli import _run_client

            _run_client("https://hub.example.com", "key", "tok")
        assert exc.value.code == 0

    @patch("simforge_agent.cli.AgentClient")
    def test_erreur_fatale_exit_3(self, mock_client_cls):
        async def _boom():
            raise RuntimeError("connexion impossible")

        mock_client_cls.return_value.run.return_value = _boom()
        with pytest.raises(SystemExit) as exc:
            from simforge_agent.cli import _run_client

            _run_client("https://hub.example.com", "key", "tok")
        assert exc.value.code == 3
