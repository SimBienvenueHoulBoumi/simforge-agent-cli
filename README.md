# SimForge Agent CLI

Agent open-source de la plateforme **SimDevForge**. Il s'installe sur **votre**
serveur, s'enrôle auprès du hub via un token à usage unique, puis maintient une
connexion WebSocket sécurisée pour exécuter les tâches d'orchestration
(déploiements Helm, probes cluster, runners GitLab, rollouts…).

Le code est public pour une raison simple : **vous devez pouvoir auditer ce qui
tourne sur vos machines avant de l'installer.** L'agent ne contient aucun secret
en dur — tous les credentials sont obtenus au moment de l'enrôlement et stockés
localement (`config.json`, permissions `600` en root).

## Installation

```bash
# Depuis les sources
pip install .

# Ou construire le wheel
pip install build && python -m build
pip install dist/simforge_agent-*.whl
```

Prérequis : Python ≥ 3.10. Dépendances runtime : `websockets`, `httpx`.

## Utilisation

```bash
# 1. Enrôlement — échange le token contre des credentials, écrit config.json
simforge-agent enroll https://hub.example.com <ENROLLMENT_TOKEN>

# 2. Démarrage — connexion WebSocket + heartbeat
simforge-agent start

# 3. Statut — configuré ? connecté ? métadonnées
simforge-agent status

# 4. Arrêt
simforge-agent stop
```

Le token d'enrôlement se récupère depuis l'UI **Fleets** de la plateforme. Il est
à usage unique et valable 1 h.

### Options de `start`

| Option | Effet |
|--------|-------|
| `--hub-url` | URL du hub (prime sur `config.json`) |
| `--agent-key` | Clé d'agent (prime sur `config.json`) |
| `--cluster-token` | Token de cluster (prime sur `config.json`) |

## Exécution en service (systemd) — recommandé en production

En production, l'agent doit survivre aux crashs, aux coupures réseau et aux
redémarrages du serveur. Un service systemd le supervise et le relance
automatiquement. Une unit prête à l'emploi est fournie dans
[`packaging/simforge-agent.service`](./packaging/simforge-agent.service).

```bash
# 1. Enrôler d'abord (écrit /etc/simforge-agent/config.json)
sudo simforge-agent enroll https://hub.example.com <ENROLLMENT_TOKEN>

# 2. Installer l'unit (ajuster ExecStart si `which simforge-agent` diffère)
sudo cp packaging/simforge-agent.service /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Démarrer au boot + maintenant
sudo systemctl enable --now simforge-agent

# Suivi des logs
journalctl -u simforge-agent -f
```

Garanties apportées :

- **Relance automatique** (`Restart=always`) après crash, perte réseau ou reboot.
- **Arrêt propre** : `systemctl stop` envoie `SIGTERM`, que l'agent intercepte
  pour fermer la connexion WebSocket avant de quitter (code de sortie `0`).
- **Persistance au boot** (`WantedBy=multi-user.target`).

## Configuration

L'agent persiste sa config dans :

- `/etc/simforge-agent/config.json` (exécution en root — permissions forcées à `600`)
- `~/.config/simforge-agent/config.json` (utilisateur non-root)

### Variables d'environnement

| Variable | Rôle |
|----------|------|
| `SIMFORGE_AGENT_CA_BUNDLE` | Chemin d'un CA bundle custom (recommandé pour un certificat auto-signé) |
| `SIMFORGE_AGENT_INSECURE` | `1`/`true` désactive la vérification TLS — **dev local uniquement**, ignoré si un CA bundle est fourni |

## Sécurité

- Aucun secret n'est embarqué dans le binaire : les tokens viennent de l'enrôlement.
- `config.json` contient des tokens sensibles → permissions `600` appliquées et vérifiées en root.
- Préférez `SIMFORGE_AGENT_CA_BUNDLE` à `SIMFORGE_AGENT_INSECURE`.

## Développement

```bash
pip install -e '.[dev]'
ruff check src/
pytest
```

## Licence

[MIT](./LICENSE)
