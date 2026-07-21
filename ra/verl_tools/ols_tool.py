"""verl BaseTool subclass for OLS K8s troubleshooting tools.

One class handles ALL 30 tools. verl creates one instance per tool schema
entry; self.name tells us which mock tool to dispatch to.
"""

import sys
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
            return ToolResponse(text="Error: no DB connection"), 0.0, {}

        try:
            result = call_tool(info["conn"], self.name, parameters or {})
            return ToolResponse(text=str(result)), 0.0, {}
        except Exception as e:
            return ToolResponse(text=f"Error: {e}"), 0.0, {}

    async def calc_reward(self, instance_id, **kwargs):
        """Not used -- reward is computed via the separate reward function."""
        return 0.0

    async def release(self, instance_id, **kwargs):
        """Cleanup: close connection, drop database."""
        info = OLSTroubleshootingTool._instances.pop(instance_id, None)
        if info:
            info["conn"].close()
            try:
                db.teardown_db(db_name=info["db_name"])
            except Exception:
                pass
