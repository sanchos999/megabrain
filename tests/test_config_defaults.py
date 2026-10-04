from core.config import DEFAULTS


def test_default_onnx_threads_match_measured_cpu_profile():
    assert DEFAULTS["onnx_threads"] == 4


def test_experimental_e5_route_is_disabled_by_default():
    assert DEFAULTS["retrieval_e5_fast_path"] is False
    assert DEFAULTS["retrieval_e5_min_similarity"] == 0.80
    assert DEFAULTS["retrieval_e5_min_margin"] == 0.02


def test_e5_thresholds_parse_as_floats(monkeypatch):
    from core.config import load_config

    monkeypatch.setenv("MB_RETRIEVAL_E5_MIN_SIMILARITY", "0.83")
    monkeypatch.setenv("MB_RETRIEVAL_E5_MIN_MARGIN", "0.04")
    cfg = load_config()
    assert cfg["retrieval_e5_min_similarity"] == 0.83
    assert cfg["retrieval_e5_min_margin"] == 0.04
