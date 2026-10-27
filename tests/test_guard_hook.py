import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "guard", Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard.py"
)
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push --force origin feat/x",
        "terraform apply -auto-approve",
        "gcloud run deploy api",
        "gcloud storage cp a gs://b",
        "docker push ghcr.io/x/y",
    ],
)
def test_blocks_dangerous_commands(command: str) -> None:
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize("command", ["git push origin feat/retry-policy", "uv run pytest"])
def test_allows_normal_commands(command: str) -> None:
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": command}}) is None


@pytest.mark.parametrize("path", [".env", "config/.env.prod", ".dev-keys/private.pem", "key.pem"])
def test_blocks_secret_files(path: str) -> None:
    assert guard.check({"tool_name": "Write", "tool_input": {"file_path": path}})


def test_allows_env_example() -> None:
    assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": ".env.example"}}) is None


@pytest.mark.parametrize(
    "command",
    [
        "git -C . push origin main",
        "git -c core.x=y push origin MAIN",
        "git push -f origin feat/x",
        "git push origin +feat/x",
        "cat .env",
        "echo X > .env",
        "type config/.env.prod",
        "cat .dev-keys/private.pem",
    ],
)
def test_blocks_known_bypass_shapes(command: str) -> None:
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.mark.parametrize("path", [".ENV", "KEY.PEM"])
def test_secret_paths_are_case_insensitive(path: str) -> None:
    assert guard.check({"tool_name": "MultiEdit", "tool_input": {"file_path": path}})


def test_env_example_stays_editable_from_the_shell() -> None:
    assert (
        guard.check({"tool_name": "Bash", "tool_input": {"command": "cp .env.example x"}}) is None
    )
