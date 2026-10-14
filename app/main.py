"""
Application entry point.

Phase 1 scope only: prove the environment is wired correctly — virtual
env active, dependencies importable, `.env` loading through
`app.config.settings`, and the project folders present. It does not talk
to an LLM, a vector DB, or PX4 yet; those land in Phases 2, 3, and 7-8 and
will be wired into this same `main()` function as they're built, so this
file is the one place that will eventually run the full pipeline:

    request -> RAG -> LLM -> schema validation -> rule validation
             -> mission conversion -> MAVSDK -> PX4 SITL -> result

Run it with:
    python -m app.main
"""

import logging
import sys

from app.config import PROJECT_ROOT, settings


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _check_environment() -> list[str]:
    """
    Phase 1 sanity checks. Returns a list of problems found (empty = all good).
    This deliberately does not raise -- main() decides what to do with the
    results so the checks stay easy to unit test later (Phase 9).
    """
    problems: list[str] = []

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        problems.append(
            f".env not found at {env_file} -- copy .env.example to .env "
            f"(the app is currently running on built-in defaults only)."
        )

    if not settings.documents_dir.exists():
        problems.append(f"Expected knowledge-base folder missing: {settings.documents_dir}")

    required_dirs = [
        PROJECT_ROOT / "app" / "rag",
        PROJECT_ROOT / "app" / "llm",
        PROJECT_ROOT / "app" / "mission",
        PROJECT_ROOT / "app" / "mavlink",
        PROJECT_ROOT / "app" / "database",
        PROJECT_ROOT / "app" / "ui",
        PROJECT_ROOT / "data",
        PROJECT_ROOT / "tests",
    ]
    for d in required_dirs:
        if not d.exists():
            problems.append(f"Expected project folder missing: {d}")

    return problems


def main() -> int:
    _configure_logging()
    log = logging.getLogger("drone_mission_planner")

    log.info("Drone Mission Planner -- Phase 1 environment check")
    log.info("Project root: %s", PROJECT_ROOT)
    log.info("Python: %s", sys.version.split()[0])

    problems = _check_environment()

    if problems:
        log.warning("Environment check found %d issue(s):", len(problems))
        for p in problems:
            log.warning("  - %s", p)
    else:
        log.info("All Phase 1 checks passed.")

    log.info("Current configuration (from .env / defaults):")
    log.info("  LLM_PROVIDER        = %s", settings.llm_provider)
    log.info("  LLM_MODEL           = %s", settings.llm_model)
    log.info("  EMBEDDING_MODEL     = %s", settings.embedding_model)
    log.info("  CHROMA_PERSIST_DIR  = %s", settings.chroma_persist_path)
    log.info("  PX4_CONNECTION_URL  = %s", settings.px4_connection_url)
    log.info("  DATABASE_URL        = %s", settings.database_url)

    log.info(
        "Phase 1 complete: this entry point does not yet run RAG, the LLM, "
        "or PX4 -- those arrive in later phases and will be called from "
        "main() once built."
    )

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
