import structlog

logger = structlog.get_logger("simulate_mcp")


def emit_phase_event(phase: str, status: str, details: dict | None = None) -> None:
    logger.info(
        "pipeline.phase",
        phase=phase,
        status=status,
        **(details or {}),
    )
