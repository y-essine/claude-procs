import json
import os

os.environ.setdefault("CLAUDE_PROCS_STATE", "/tmp/claude-procs-test-state")

from claude_procs import install  # noqa: E402


def test_hooks_are_added_once_and_removed_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "exe_path", lambda: "/opt/bin/claude-procs")
    d = tmp_path / ".claude-work"
    d.mkdir()
    other = {"type": "command", "command": "python3 my-hook.py"}
    (d / "settings.json").write_text(json.dumps({"theme": "dark", "hooks": {"PreToolUse": [{"hooks": [other]}],
                                                                          "SessionStart": [{"hooks": [other]}]}}))
    install.add_hooks(str(d))
    install.add_hooks(str(d))  # idempotent
    cfg = json.loads((d / "settings.json").read_text())
    cmds = [h["command"] for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]]
    assert cmds == ["python3 my-hook.py", "/opt/bin/claude-procs hook"]
    assert install.hooks_state(str(d)) == {"SessionStart": True, "UserPromptSubmit": True}
    assert (d / ("settings.json" + install.BACKUP_SUFFIX)).exists()

    assert install.remove_hooks(str(d))
    cfg = json.loads((d / "settings.json").read_text())
    assert cfg == {"theme": "dark", "hooks": {"PreToolUse": [{"hooks": [other]}], "SessionStart": [{"hooks": [other]}]}}


def test_config_dirs_finds_accounts(tmp_path, monkeypatch):
    for name in (".claude", ".claude-personal", ".claude-work", ".claude-empty"):
        (tmp_path / name).mkdir()
    for name in (".claude", ".claude-personal", ".claude-work"):
        (tmp_path / name / "settings.json").write_text("{}")
    monkeypatch.setattr(install, "HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    found = [os.path.basename(p) for p in install.config_dirs()]
    assert found == [".claude", ".claude-personal", ".claude-work"]
