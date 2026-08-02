import structlog

logger = structlog.get_logger("ols_eval")


def emit_phase_event(phase: str, status: str, details: dict | None = None) -> None:
    logger.info(
        "pipeline.phase",
        phase=phase,
        status=status,
        **(details or {}),
    )
