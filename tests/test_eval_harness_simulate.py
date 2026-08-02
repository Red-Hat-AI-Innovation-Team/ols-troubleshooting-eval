"""Tests for SIMULATE_WORKFLOW integration in run_eval.sh."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent.parent
RUN_EVAL = SCRIPT_DIR / "run_eval.sh"
SKILL_FILE = SCRIPT_DIR / "skills" / "simulate-validate" / "skill.md"


class TestRunEvalSimulateWorkflowConfig:
    """Validate run_eval.sh handles SIMULATE_WORKFLOW correctly."""

    def test_simulate_workflow_env_var_present_in_script(self) -> None:
        content = RUN_EVAL.read_text()
        assert 'SIMULATE_WORKFLOW="${SIMULATE_WORKFLOW:-}"' in content

    def test_simulate_workflow_enables_simulate(self) -> None:
        content = RUN_EVAL.read_text()
        assert 'SIMULATE_ENABLED="${SIMULATE_ENABLED:-1}"' in content

    def test_factory_cli_warning_present(self) -> None:
        content = RUN_EVAL.read_text()
        assert "factory CLI not found" in content

    def test_timeout_increase_to_600(self) -> None:
        content = RUN_EVAL.read_text()
        assert "OLS_QUERY_TIMEOUT" in content
        assert "600" in content

    def test_simulate_validate_tag_added_conditionally(self) -> None:
        content = RUN_EVAL.read_text()
        assert "simulate_validate" in content
        assert 'TAGS+=(simulate_validate)' in content

    def test_system_simulate_yaml_used_for_tag(self) -> None:
        content = RUN_EVAL.read_text()
        assert 'system-simulate.yaml' in content
        assert '"simulate_validate"' in content

    def test_skill_file_exists(self) -> None:
        assert SKILL_FILE.exists(), f"Skill file missing: {SKILL_FILE}"

    def test_skill_file_has_content(self) -> None:
        content = SKILL_FILE.read_text()
        assert len(content) > 100
        assert "Fix Validation Protocol" in content


class TestSystemSimulateYamlGeneration:
    """Test that the Python snippet in run_eval.sh correctly injects the skill."""

    @pytest.fixture
    def tmp_work(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Create a minimal system.yaml and skill file for testing."""
        system_yaml = tmp_path / "system.yaml"
        system_yaml.write_text(textwrap.dedent("""\
            llm:
              model: "test-model"
            api:
              system_prompt: null
              timeout: 600
        """))

        skill_file = tmp_path / "skill.md"
        skill_file.write_text(textwrap.dedent("""\
            # Simulate-Validate Skill

            When you have diagnosed an issue:
            1. Propose a fix
            2. Validate via simulation
        """))

        out_yaml = tmp_path / "system-simulate.yaml"
        return system_yaml, skill_file, out_yaml

    def test_injection_replaces_null_with_skill_content(
        self, tmp_work: tuple[Path, Path, Path]
    ) -> None:
        sys_in, skill_file, sys_out = tmp_work

        env = os.environ.copy()
        env["_SKILL_FILE"] = str(skill_file)
        env["_SYS_IN"] = str(sys_in)
        env["_SYS_OUT"] = str(sys_out)

        result = subprocess.run(
            ["python3", "-c", textwrap.dedent("""\
                import os
                skill_path = os.environ['_SKILL_FILE']
                sys_in = os.environ['_SYS_IN']
                sys_out = os.environ['_SYS_OUT']
                with open(skill_path) as f:
                    skill = f.read()
                with open(sys_in) as f:
                    content = f.read()
                indent = '  '
                block = indent + 'system_prompt: |'
                for line in skill.splitlines():
                    block += '\\n' + indent + '  ' + line
                content = content.replace(indent + 'system_prompt: null', block)
                with open(sys_out, 'w') as f:
                    f.write(content)
            """)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert sys_out.exists()

        generated = sys_out.read_text()
        assert "system_prompt: null" not in generated
        assert "system_prompt: |" in generated
        assert "Simulate-Validate Skill" in generated
        assert "Propose a fix" in generated

    def test_non_simulate_fields_preserved(
        self, tmp_work: tuple[Path, Path, Path]
    ) -> None:
        sys_in, skill_file, sys_out = tmp_work

        env = os.environ.copy()
        env["_SKILL_FILE"] = str(skill_file)
        env["_SYS_IN"] = str(sys_in)
        env["_SYS_OUT"] = str(sys_out)

        subprocess.run(
            ["python3", "-c", textwrap.dedent("""\
                import os
                skill_path = os.environ['_SKILL_FILE']
                sys_in = os.environ['_SYS_IN']
                sys_out = os.environ['_SYS_OUT']
                with open(skill_path) as f:
                    skill = f.read()
                with open(sys_in) as f:
                    content = f.read()
                indent = '  '
                block = indent + 'system_prompt: |'
                for line in skill.splitlines():
                    block += '\\n' + indent + '  ' + line
                content = content.replace(indent + 'system_prompt: null', block)
                with open(sys_out, 'w') as f:
                    f.write(content)
            """)],
            env=env,
            capture_output=True,
            text=True,
        )
        generated = sys_out.read_text()
        assert "test-model" in generated
        assert "timeout: 600" in generated


class TestEvalsYamlSimulateEntry:
    """Verify the simulate_validate eval entry is well-formed."""

    def test_evals_yaml_has_simulate_validate_tag(self) -> None:
        evals_path = SCRIPT_DIR / "eval_scenarios" / "evals.yaml"
        content = evals_path.read_text()
        assert "tag: simulate_validate" in content

    def test_evals_yaml_has_setup_and_cleanup(self) -> None:
        evals_path = SCRIPT_DIR / "eval_scenarios" / "evals.yaml"
        content = evals_path.read_text()
        assert "scenarios/simulate_validate/setup.sh" in content
        assert "scenarios/simulate_validate/cleanup.sh" in content

    def test_scenario_setup_exists(self) -> None:
        setup = SCRIPT_DIR / "eval_scenarios" / "scenarios" / "simulate_validate" / "setup.sh"
        assert setup.exists()
        assert os.access(setup, os.X_OK)

    def test_scenario_cleanup_exists(self) -> None:
        cleanup = SCRIPT_DIR / "eval_scenarios" / "scenarios" / "simulate_validate" / "cleanup.sh"
        assert cleanup.exists()
        assert os.access(cleanup, os.X_OK)

    def test_fixture_creates_broken_state(self) -> None:
        fixture = (
            SCRIPT_DIR / "eval_scenarios" / "scenarios"
            / "simulate_validate" / "fixtures" / "deployment.yaml"
        )
        assert fixture.exists()
        content = fixture.read_text()
        assert "APP_CONFIG_PATH" in content
        assert "exit 1" in content
