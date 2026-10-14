"""
Phase 1 tests -- run with:  pytest tests/test_phase1_environment.py -v

These only prove the environment/config layer works. They do not touch
the LLM, RAG, or PX4 -- dedicated test files for those arrive in the
matching phase (Phase 9 adds the full automated evaluation harness).
"""

from app.config import PROJECT_ROOT, Settings, settings
from app.main import _check_environment


def test_settings_load_with_defaults():
    """Settings must construct even if .env is missing (defaults kick in)."""
    s = Settings(_env_file=None)  # ignore any real .env for this test
    # Groq is the default provider (see app/config.py) -- chosen over
    # Ollama partway through Phase 3 because it needs no local RAM/VRAM.
    # Ollama is still fully supported via LLM_PROVIDER=ollama in .env.
    assert s.llm_provider == "groq"
    assert s.max_altitude_m == 120
    # "udpin://0.0.0.0:14540" -- fixed in Phase 6 after checking current
    # MAVSDK docs; the old "udp://:14540" placeholder isn't a documented
    # scheme for this MAVSDK version (see app/config.py's comment).
    assert s.px4_connection_url == "udpin://0.0.0.0:14540"


def test_settings_singleton_is_usable():
    assert settings.llm_model  # non-empty string
    assert settings.rag_top_k > 0


def test_project_folders_exist():
    for sub in ["app", "app/rag", "app/llm", "app/mission", "app/mavlink",
                "app/database", "app/ui", "data", "tests"]:
        assert (PROJECT_ROOT / sub).exists(), f"missing folder: {sub}"


def test_documents_dir_resolves_under_data():
    assert settings.documents_dir == PROJECT_ROOT / "data" / "documents"


def test_check_environment_runs_without_raising():
    # Should return a list (possibly with warnings, e.g. missing .env),
    # never throw.
    problems = _check_environment()
    assert isinstance(problems, list)
