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

    def test_factory_cli_warning_present(self) -> None:
        content = RUN_EVAL.read_text()
        assert "factory CLI not found" in content

    def test_timeout_increase_to_600(self) -> None:
        content = RUN_EVAL.read_text()
        assert "OLS_QUERY_TIMEOUT" in content
        assert "600" in content

    def test_simulate_injects_into_system_yaml(self) -> None:
        """When SIMULATE_WORKFLOW is set, the skill is injected into system.yaml directly."""
        content = RUN_EVAL.read_text()
        assert "system.yaml" in content
        assert "simulate-validate/skill.md" in content

    def test_no_simulate_validate_tag(self) -> None:
        """simulate_validate tag should not exist — simulation applies to all scenarios."""
        content = RUN_EVAL.read_text()
        assert "simulate_validate" not in content

    def test_skill_file_exists(self) -> None:
        assert SKILL_FILE.exists(), f"Skill file missing: {SKILL_FILE}"

    def test_skill_file_has_content(self) -> None:
        content = SKILL_FILE.read_text()
        assert len(content) > 100
        assert "Fix Validation Protocol" in content


class TestSystemSimulateYamlGeneration:
    """Test that the Python snippet in run_eval.sh correctly injects the skill."""

    @pytest.fixture
    def tmp_work(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a minimal system.yaml and skill file for testing."""
        system_yaml = tmp_path / "system.yaml"
        system_yaml.write_text(textwrap.dedent("""\
            llm:
              model: "test-model"
            api:
              system_prompt: null # System prompt (default None)
              timeout: 600
        """))

        skill_file = tmp_path / "skill.md"
        skill_file.write_text(textwrap.dedent("""\
            # Simulate-Validate Skill

            When you have diagnosed an issue:
            1. Propose a fix
            2. Validate via simulation
        """))

        return system_yaml, skill_file

    def test_injection_replaces_null_with_skill_content(
        self, tmp_work: tuple[Path, Path]
    ) -> None:
        sys_yaml, skill_file = tmp_work

        env = os.environ.copy()
        env["_SKILL_FILE"] = str(skill_file)
        env["_SYS"] = str(sys_yaml)

        result = subprocess.run(
            ["python3", "-c", textwrap.dedent("""\
                import os, re
                skill_path = os.environ['_SKILL_FILE']
                sys_path = os.environ['_SYS']
                with open(skill_path) as f:
                    skill = f.read()
                with open(sys_path) as f:
                    content = f.read()
                indent = '  '
                block = indent + 'system_prompt: |'
                for line in skill.splitlines():
                    block += '\\n' + indent + '  ' + line
                content = re.sub(r'^  system_prompt: null.*$', block, content, count=1, flags=re.MULTILINE)
                with open(sys_path, 'w') as f:
                    f.write(content)
            """)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        generated = sys_yaml.read_text()
        assert "system_prompt: null" not in generated
        assert "system_prompt: |" in generated
        assert "Simulate-Validate Skill" in generated
        assert "Propose a fix" in generated

    def test_non_simulate_fields_preserved(
        self, tmp_work: tuple[Path, Path]
    ) -> None:
        sys_yaml, skill_file = tmp_work

        env = os.environ.copy()
        env["_SKILL_FILE"] = str(skill_file)
        env["_SYS"] = str(sys_yaml)

        subprocess.run(
            ["python3", "-c", textwrap.dedent("""\
                import os, re
                skill_path = os.environ['_SKILL_FILE']
                sys_path = os.environ['_SYS']
                with open(skill_path) as f:
                    skill = f.read()
                with open(sys_path) as f:
                    content = f.read()
                indent = '  '
                block = indent + 'system_prompt: |'
                for line in skill.splitlines():
                    block += '\\n' + indent + '  ' + line
                content = re.sub(r'^  system_prompt: null.*$', block, content, count=1, flags=re.MULTILINE)
                with open(sys_path, 'w') as f:
                    f.write(content)
            """)],
            env=env,
            capture_output=True,
            text=True,
        )
        generated = sys_yaml.read_text()
        assert "test-model" in generated
        assert "timeout: 600" in generated
