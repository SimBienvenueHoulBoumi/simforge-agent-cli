"""Normalisation du timeout helm — le backend envoie un int (timeout_seconds)."""

from simforge_agent.handlers.helm import _normalize_helm_timeout


def test_int_devient_duration_secondes():
    assert _normalize_helm_timeout(360) == "360s"


def test_str_passe_tel_quel():
    assert _normalize_helm_timeout("5m") == "5m"


def test_none_et_vide_donnent_le_defaut():
    assert _normalize_helm_timeout(None) == "5m"
    assert _normalize_helm_timeout("") == "5m"


def test_float_tronque_en_secondes():
    assert _normalize_helm_timeout(120.7) == "120s"
