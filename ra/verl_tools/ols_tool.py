"""verl BaseTool subclass for OLS K8s troubleshooting tools.

One class handles ALL 30 tools. verl creates one instance per tool schema
entry; self.name tells us which mock tool to dispatch to.
"""

import logging
import sys
import time
import threading
from pathlib import Path

# Ensure ra/ is on sys.path so `import db`, `import mock_tools` etc. work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uuid

import psycopg2

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import ToolResponse

import db
from eval.scenarios import load_seed
from mock_tools import call_tool


logger = logging.getLogger("ols_tool")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(_handler)


class _ToolExecutionCounter:
    """Thread-safe counter for tracking tool executions across training steps."""

    def __init__(self):
        self._lock = threading.Lock()
        self._total = 0
        self._per_step: dict[str, int] = {}  # step_id -> count

    def increment(self, step_id: str = "default") -> int:
        with self._lock:
            self._total += 1
            self._per_step[step_id] = self._per_step.get(step_id, 0) + 1
            return self._total

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    def get_step_count(self, step_id: str = "default") -> int:
        with self._lock:
            return self._per_step.get(step_id, 0)

    def reset_step(self, step_id: str = "default") -> int:
        with self._lock:
            count = self._per_step.pop(step_id, 0)
            return count


_exec_counter = _ToolExecutionCounter()


class OLSTroubleshootingTool(BaseTool):
    """A single BaseTool subclass that dispatches to any of the 30 mock K8s tools.

    verl instantiates one copy per tool_schema entry. ``self.name`` is set from
    ``tool_schema.function.name`` and determines which mock tool function runs.
    """

    _instances: dict[str, dict] = {}

    async def create(self, instance_id=None, create_kwargs=None, **kwargs):
        """Called once per rollout to set up the DB for this scenario.

        ``create_kwargs`` comes from the dataset row's ``tools_kwargs`` field.
        It contains: ``scenario_id``, ``expected_response``.

        We: load seed, create DB, store connection.
        """
        instance_id = instance_id or str(uuid.uuid4())

        logger.info(
            "create() tool=%s instance_id=%s scenario=%s",
            self.name,
            instance_id[:12],
            create_kwargs.get("scenario_id", "?") if create_kwargs else "none",
        )

        if create_kwargs:
            scenario_id = create_kwargs.get("scenario_id", "")
            db_name = f"verl_{self.name}_{instance_id[:8]}"
            seed = load_seed(scenario_id)
            db.init_db(seed, db_name=db_name)

            OLSTroubleshootingTool._instances[instance_id] = {
                "db_name": db_name,
                "scenario_id": scenario_id,
                "expected_response": create_kwargs.get("expected_response", ""),
                "conn": psycopg2.connect(db._dsn_for(db_name)),
            }

        return instance_id, ToolResponse()

    async def execute(self, instance_id, parameters, **kwargs):
        """Execute the tool against the PostgreSQL mock.

        ``self.name`` tells us which tool (pods_list, pods_log, etc.).
        ``parameters`` are the parsed JSON arguments from the model.
        """
        info = OLSTroubleshootingTool._instances.get(instance_id)
        if not info:
            logger.warning(
                "execute() tool=%s instance_id=%s — no DB connection (instance not found)",
                self.name, instance_id[:12],
            )
            return ToolResponse(text="Error: no DB connection"), 0.0, {}

        step_id = info.get("scenario_id", "default")
        count = _exec_counter.increment(step_id)

        t0 = time.monotonic()
        try:
            result = call_tool(info["conn"], self.name, parameters or {})
            elapsed = time.monotonic() - t0
            logger.info(
                "execute() tool=%s instance_id=%s scenario=%s elapsed=%.3fs total_execs=%d params=%s",
                self.name, instance_id[:12], step_id, elapsed, count,
                str(parameters)[:200],
            )
            return ToolResponse(text=str(result)), 0.0, {}
        except Exception as e:
            elapsed = time.monotonic() - t0
            logger.error(
                "execute() tool=%s instance_id=%s ERROR: %s elapsed=%.3fs",
                self.name, instance_id[:12], e, elapsed,
            )
            return ToolResponse(text=f"Error: {e}"), 0.0, {}

    async def calc_reward(self, instance_id, **kwargs):
        """Not used -- reward is computed via the separate reward function."""
        return 0.0

    async def release(self, instance_id, **kwargs):
        """Cleanup: close connection, drop database."""
        info = OLSTroubleshootingTool._instances.pop(instance_id, None)
        if info:
            step_id = info.get("scenario_id", "default")
            step_count = _exec_counter.get_step_count(step_id)
            logger.info(
                "release() tool=%s instance_id=%s scenario=%s tool_execs_this_scenario=%d",
                self.name, instance_id[:12], step_id, step_count,
            )
            info["conn"].close()
            try:
                db.teardown_db(db_name=info["db_name"])
            except Exception:
                pass
        else:
            logger.warning(
                "release() tool=%s instance_id=%s — instance not found (already released?)",
                self.name, instance_id[:12],
            )
