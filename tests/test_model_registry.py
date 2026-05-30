from pathlib import Path

from wrekker.setup import model_registry


def test_model_path_uses_models_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WREKKER_MODELS_PATH", str(tmp_path))
    assert model_registry.model_path("beat_this") == tmp_path / "beat_this"


def test_user_python_path_is_persistent_target(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "python" / "site-packages"
    monkeypatch.setenv("WREKKER_PYTHON_PACKAGES_PATH", str(target))
    assert model_registry.ensure_user_python_path() == target
    assert str(target) in model_registry.sys.path


def test_beat_this_readiness_is_package_based() -> None:
    spec = model_registry.MODELS["beat_this"]
    assert spec.get("check_import") == "beat_this"
    assert "check_file" not in spec
    assert "url" not in spec
