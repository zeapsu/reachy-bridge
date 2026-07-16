import sys
import importlib
from types import ModuleType
from pathlib import Path

import pytest

import reachy_chat.config as config_mod


def _reload_core_tools() -> ModuleType:
    """Reload core_tools after config object has been patched."""
    for module_name in list(sys.modules):
        if module_name.startswith("reachy_chat.tools."):
            sys.modules.pop(module_name, None)
    # External file-loaded modules are registered by bare tool name.
    sys.modules.pop("ext_ping", None)
    sys.modules.pop("local_ping", None)
    sys.modules.pop("ext_dup_a", None)
    sys.modules.pop("ext_dup_b", None)

    sys.modules.pop("reachy_chat.tools.core_tools", None)
    core_tools_mod = importlib.import_module("reachy_chat.tools.core_tools")
    core_tools_mod.initialize_tools()
    return core_tools_mod


# A minimal profile-local tool, written into a tmp profile to exercise the
# profile-local loader without depending on any shipping tool (sweep_look used
# to serve this role before it was promoted to a core tool).
_PROFILE_LOCAL_TOOL_SRC = "\n".join(
    [
        "from typing import Any, Dict",
        "from reachy_chat.tools.core_tools import Tool, ToolDependencies",
        "",
        "class LocalPingTool(Tool):",
        '    name = "local_ping"',
        '    description = "Profile-local ping tool"',
        '    parameters_schema = {"type": "object", "properties": {}, "required": []}',
        "",
        "    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:",
        '        return {"status": "ok"}',
        "",
    ]
)


def _make_local_tool_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a tmp profile that ships its own profile-local tool .py file."""
    profile_name = "local_tool_profile"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / profile_name
    profile_dir.mkdir(parents=True)
    (profile_dir / "instructions.txt").write_text("hi\n", encoding="utf-8")
    (profile_dir / "tools.txt").write_text("local_ping\n", encoding="utf-8")
    (profile_dir / "local_ping.py").write_text(_PROFILE_LOCAL_TOOL_SRC, encoding="utf-8")
    monkeypatch.setattr(config_mod.config, "REACHY_MINI_CUSTOM_PROFILE", profile_name)
    monkeypatch.setattr(config_mod.config, "PROFILES_DIRECTORY", profiles_root)
    monkeypatch.setattr(config_mod.config, "TOOLS_DIRECTORY", None)
    monkeypatch.setattr(config_mod.config, "AUTOLOAD_EXTERNAL_TOOLS", False)


def test_external_profile_can_use_builtin_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """External profile tools.txt can reference built-in src tools."""
    profile_name = "ext_profile_test"
    external_profiles_root = tmp_path / "external_profiles"
    profile_dir = external_profiles_root / profile_name
    profile_dir.mkdir(parents=True)
    (profile_dir / "instructions.txt").write_text("hello\n", encoding="utf-8")
    (profile_dir / "tools.txt").write_text("dance\n", encoding="utf-8")

    monkeypatch.setattr(config_mod.config, "REACHY_MINI_CUSTOM_PROFILE", profile_name)
    monkeypatch.setattr(config_mod.config, "PROFILES_DIRECTORY", external_profiles_root)
    monkeypatch.setattr(config_mod.config, "TOOLS_DIRECTORY", None)
    monkeypatch.setattr(config_mod.config, "AUTOLOAD_EXTERNAL_TOOLS", False)

    core_tools_mod = _reload_core_tools()

    assert "dance" in core_tools_mod.ALL_TOOLS
    assert "dance" not in sys.modules


def test_external_tools_can_be_loaded_without_external_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External tools can be loaded with built-in profile via autoload mode."""
    external_tools_root = tmp_path / "external_tools"
    external_tools_root.mkdir(parents=True)

    (external_tools_root / "ext_ping.py").write_text(
        "\n".join(
            [
                "from typing import Any, Dict",
                "from reachy_chat.tools.core_tools import Tool, ToolDependencies",
                "",
                "class ExtPingTool(Tool):",
                '    name = "ext_ping"',
                '    description = "External ping tool"',
                '    parameters_schema = {"type": "object", "properties": {}, "required": []}',
                "",
                "    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:",
                '        return {"status": "ok"}',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_mod.config, "REACHY_MINI_CUSTOM_PROFILE", "default")
    monkeypatch.setattr(config_mod.config, "PROFILES_DIRECTORY", config_mod.DEFAULT_PROFILES_DIRECTORY)
    monkeypatch.setattr(config_mod.config, "TOOLS_DIRECTORY", external_tools_root)
    monkeypatch.setattr(config_mod.config, "AUTOLOAD_EXTERNAL_TOOLS", True)

    core_tools_mod = _reload_core_tools()

    assert "ext_ping" in core_tools_mod.ALL_TOOLS


def test_external_tools_fail_on_duplicate_tool_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading must fail if multiple tools declare the same Tool.name."""
    external_tools_root = tmp_path / "external_tools"
    external_tools_root.mkdir(parents=True)

    duplicate_tool_source = "\n".join(
        [
            "from typing import Any, Dict",
            "from reachy_chat.tools.core_tools import Tool, ToolDependencies",
            "",
            "class DupTool(Tool):",
            '    name = "dup_tool"',
            '    description = "Duplicate tool name"',
            '    parameters_schema = {"type": "object", "properties": {}, "required": []}',
            "",
            "    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:",
            '        return {"status": "ok"}',
            "",
        ]
    )
    (external_tools_root / "ext_dup_a.py").write_text(duplicate_tool_source, encoding="utf-8")
    (external_tools_root / "ext_dup_b.py").write_text(duplicate_tool_source, encoding="utf-8")

    monkeypatch.setattr(config_mod.config, "REACHY_MINI_CUSTOM_PROFILE", "default")
    monkeypatch.setattr(config_mod.config, "PROFILES_DIRECTORY", config_mod.DEFAULT_PROFILES_DIRECTORY)
    monkeypatch.setattr(config_mod.config, "TOOLS_DIRECTORY", external_tools_root)
    monkeypatch.setattr(config_mod.config, "AUTOLOAD_EXTERNAL_TOOLS", True)

    with pytest.raises(RuntimeError, match="Duplicate Tool.name values detected"):
        _reload_core_tools()


def test_profile_can_load_profile_local_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile-local tool .py shipped in the profile folder should load."""
    _make_local_tool_profile(tmp_path, monkeypatch)

    core_tools_mod = _reload_core_tools()

    assert "local_ping" in core_tools_mod.ALL_TOOLS


def test_tool_registry_reloads_when_profile_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime profile changes should refresh enabled tools without restarting Python."""
    monkeypatch.setattr(config_mod.config, "REACHY_MINI_CUSTOM_PROFILE", "default")
    monkeypatch.setattr(config_mod.config, "PROFILES_DIRECTORY", config_mod.DEFAULT_PROFILES_DIRECTORY)
    monkeypatch.setattr(config_mod.config, "TOOLS_DIRECTORY", None)
    monkeypatch.setattr(config_mod.config, "AUTOLOAD_EXTERNAL_TOOLS", False)

    core_tools_mod = _reload_core_tools()

    initial_tool_names = {spec["name"] for spec in core_tools_mod.get_tool_specs()}
    assert "sweep_look" in initial_tool_names
    assert "camera" in initial_tool_names

    monkeypatch.setattr(config_mod.config, "REACHY_MINI_CUSTOM_PROFILE", "mars_rover")

    reloaded_tool_names = {spec["name"] for spec in core_tools_mod.get_tool_specs()}
    assert "camera" in reloaded_tool_names
    assert "move_head" in reloaded_tool_names
    assert "sweep_look" not in reloaded_tool_names
    assert "camera" in core_tools_mod.ALL_TOOLS
    assert "sweep_look" not in core_tools_mod.ALL_TOOLS


def test_forced_tool_registry_reload_does_not_duplicate_profile_local_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading a profile-local tool should ignore stale class objects from the previous load."""
    _make_local_tool_profile(tmp_path, monkeypatch)

    core_tools_mod = _reload_core_tools()
    core_tools_mod.initialize_tools(force=True)

    assert "local_ping" in core_tools_mod.ALL_TOOLS
