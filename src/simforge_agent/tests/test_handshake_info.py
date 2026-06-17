"""Tests pour les fonctions _collect_hardware_info et _detect_cluster_info du handshake enrichi."""

import os
from unittest.mock import patch

from simforge_agent.client import _collect_hardware_info, _detect_cluster_info


def test_hardware_info_contient_les_cles_requises():
    info = _collect_hardware_info()
    for key in ("cpu_cores", "ram_total_gb", "os_name", "hostname", "arch"):
        assert key in info, f"Clé manquante : {key}"


def test_hardware_info_types_corrects():
    info = _collect_hardware_info()
    assert isinstance(info["cpu_cores"], int)
    assert isinstance(info["ram_total_gb"], float)


def test_hardware_info_ne_leve_pas_sans_psutil():
    with patch.dict("sys.modules", {"psutil": None}):
        info = _collect_hardware_info()
    assert isinstance(info, dict)
    assert info["cpu_cores"] == 0


def test_cluster_info_sans_kubeconfig(tmp_path):
    with patch.dict(os.environ, {"KUBECONFIG": str(tmp_path / "absent.yaml")}):
        info = _detect_cluster_info()
    assert info["type"] == "none"
    assert info["kubeconfig_path"] == ""


def test_cluster_info_avec_kubeconfig(tmp_path):
    kube = tmp_path / "config"
    kube.write_text("apiVersion: v1\n")
    with patch.dict(os.environ, {"KUBECONFIG": str(kube)}):
        info = _detect_cluster_info()
    assert info["type"] in ("k3s", "k8s", "microk8s", "k0s", "unknown")
    assert info["kubeconfig_path"] == str(kube)


def test_cluster_info_detecte_k3s(tmp_path):
    kube = tmp_path / "config"
    kube.write_text("server: https://127.0.0.1:6443\nclusters:\n- name: k3s\n")
    with patch.dict(os.environ, {"KUBECONFIG": str(kube)}):
        info = _detect_cluster_info()
    assert info["type"] == "k3s"
