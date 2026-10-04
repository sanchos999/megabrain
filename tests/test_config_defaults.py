from core.config import DEFAULTS


def test_default_onnx_threads_match_measured_cpu_profile():
    assert DEFAULTS["onnx_threads"] == 4
