"""The setup check says what's wrong and the exact next step; it never prints key values."""

import types

from jarvis import doctor


def test_ollama_missing_model_and_not_running():
    cfg = {"llm": {"ollama_host": "http://x", "ollama_model": "qwen3:8b"}}
    ok = lambda url, timeout: types.SimpleNamespace(json=lambda: {"models": [{"name": "llama3:latest"}]})  # noqa: E731
    checks = doctor.check_ollama(cfg, get=ok)
    model = next(c for c in checks if c.name == "AI model")
    assert model.status == doctor.FAIL and "ollama pull qwen3:8b" in model.fix

    def down(url, timeout):
        raise ConnectionError("refused")

    (c,) = doctor.check_ollama(cfg, get=down)
    assert c.name == "Ollama" and c.status in (doctor.WARN, doctor.FAIL)  # warn if installed (start-jarvis starts it)


def test_keys_report_presence_only():
    secrets = {"groq_api_key": "gsk_SECRET_VALUE_123"}
    checks = doctor.check_keys(secrets.get)
    text = doctor.report(checks)
    assert "SECRET" not in text and "Groq: set" in text
    gemini = next(c for c in checks if c.name == "Gemini")
    assert gemini.status == "info" and gemini.fix == r".\jarvis.ps1 set-key gemini"
    assert next(c for c in doctor.check_keys({}.get) if c.name == "Groq").status == doctor.WARN  # recommended


def test_gpu_and_report_summary():
    smi = lambda *a, **k: types.SimpleNamespace(stdout="NVIDIA GeForce RTX 5060 Laptop GPU, 8151\n")  # noqa: E731
    assert doctor.check_gpu(run=smi).detail == "NVIDIA GeForce RTX 5060 Laptop GPU, 8 GB"
    assert "Ready." in doctor.report([doctor.Check(doctor.OK, "Python", "3.12")])
    assert "1 problem(s)" in doctor.report([doctor.Check(doctor.FAIL, "Ollama", "not running", "Start it")])
