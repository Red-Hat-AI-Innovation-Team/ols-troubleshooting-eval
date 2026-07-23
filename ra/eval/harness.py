"""Eval harness orchestrator: run scenarios and judge agent responses."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from agent import Agent
from eval.judge import judge
from eval.scenarios import get_scenario, list_scenarios, load_seed
from llm.base import LLMClient
from run_agent import SYSTEM_PROMPT
import db
import mock_tools


@dataclass
class TurnResult:
    turn_id: str
    query: str
    response: str
    expected_response: str
    score: float
    reason: str
    conversation: list[dict]


@dataclass
class ScenarioResult:
    scenario_id: str
    turns: list[TurnResult]
    mean_score: float
    passed: bool  # mean_score >= 0.7


def run_scenario(
    scenario_id: str,
    troubleshooter_client: LLMClient,
    troubleshooter_model: str,
    judge_client: LLMClient,
    judge_model: str = "gpt-5-mini",
    db_name: str | None = None,
) -> ScenarioResult:
    """Run a single scenario through the agent and judge each turn.

    1. Load seed data and init DB
    2. For each turn: run agent with tools, judge response
    3. Teardown DB
    4. Return ScenarioResult
    """
    scenario = get_scenario(scenario_id)
    seed = load_seed(scenario_id)
    effective_db = db_name or f"eval_{scenario_id}"

    db.init_db(seed, db_name=effective_db)

    try:
        dsn = db._dsn_for(effective_db)
        turn_results: list[TurnResult] = []
        agent: Agent | None = None

        with db.connect(dsn) as conn:
            tool_defs = mock_tools.load_tool_defs()
            tool_handler = mock_tools.make_tool_handler(conn)

            for turn in scenario.turns:
                if agent is None:
                    # Create agent for first turn
                    agent = Agent(
                        system_prompt=SYSTEM_PROMPT,
                        model=troubleshooter_model,
                        tool_defs=tool_defs,
                        tool_handler=tool_handler,
                        client=troubleshooter_client,
                        max_tokens=2048,
                    )

                # Run the agent on this turn's query
                response = agent.run(turn.query)

                # Judge the response
                judge_result = judge(
                    query=turn.query,
                    response=response,
                    expected_response=turn.expected_response,
                    client=judge_client,
                    model=judge_model,
                )

                turn_results.append(TurnResult(
                    turn_id=str(turn.turn_id),
                    query=turn.query,
                    response=response,
                    expected_response=turn.expected_response,
                    score=judge_result.score,
                    reason=judge_result.reason,
                    conversation=[m.model_dump() for m in agent.messages],
                ))

                # For single-turn scenarios (1 turn), agent is done.
                # For multi-turn (wrong_networkpolicy), agent keeps history
                # and next turn's query is appended in the next iteration.

        mean_score = sum(t.score for t in turn_results) / len(turn_results)
        return ScenarioResult(
            scenario_id=scenario_id,
            turns=turn_results,
            mean_score=mean_score,
            passed=mean_score >= 0.7,
        )

    finally:
        db.teardown_db(effective_db)


def run_all_scenarios(
    troubleshooter_client: LLMClient,
    troubleshooter_model: str,
    judge_client: LLMClient,
    judge_model: str = "gpt-5-mini",
    concurrency: int = 5,
    scenarios: list[str] | None = None,
) -> dict[str, ScenarioResult]:
    """Run all or selected scenarios with ThreadPoolExecutor.

    Prints a summary table at the end. Returns dict of results.
    """
    scenario_ids = scenarios or list_scenarios()
    results: dict[str, ScenarioResult] = {}

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                run_scenario,
                scenario_id=sid,
                troubleshooter_client=troubleshooter_client,
                troubleshooter_model=troubleshooter_model,
                judge_client=judge_client,
                judge_model=judge_model,
            ): sid
            for sid in scenario_ids
        }

        for future in as_completed(futures):
            sid = futures[future]
            try:
                result = future.result()
                results[sid] = result
                status = "PASS" if result.passed else "FAIL"
                print(f"  {sid}: {status} (score={result.mean_score:.2f})")
            except Exception as e:
                print(f"  {sid}: ERROR ({e})", file=sys.stderr)

    # Print summary table
    print("\n" + "=" * 60)
    print(f"{'SCENARIO':<35} {'SCORE':>8} {'STATUS':>8}")
    print("-" * 60)
    for sid in sorted(results):
        r = results[sid]
        status = "PASS" if r.passed else "FAIL"
        print(f"{sid:<35} {r.mean_score:>8.2f} {status:>8}")

    passed = sum(1 for r in results.values() if r.passed)
    total = len(results)
    print("-" * 60)
    print(f"{'TOTAL':<35} {passed}/{total} passed")
    print("=" * 60)

    return results
