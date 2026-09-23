"""End-to-end inventory of the tools an enikk agent session actually gets.

Replicates the tool wiring of Eternity.setup() + create_session(): registers
the AppController and cron tools into the real hermes registry, then builds a
real run_agent.AIAgent with eternity.ENABLED_TOOLSETS and asserts the exact
tool list the agent would offer the LLM (agent.valid_tool_names). Building
the agent is offline — no LLM call is made.

Complements tests/test_hermes_tools.py, which covers the frozen-build
registration path: this one checks the final agent schema end to end.

Skipped when hermes-agent is not really installed (conftest mocks missing
modules in minimal environments).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import run_agent

pytestmark = pytest.mark.skipif(
    not isinstance(getattr(run_agent, "AIAgent", None), type),
    reason="hermes-agent not installed (run_agent is mocked)",
)

# Tool names registered by enikk itself (toolsets "app_controller" and
# "enikk_cron"). Keep in sync with @tool-decorated methods in controller.py
# and the schemas in cron/tools.py. run_powershell/close_window are in the
# controller module but DEREGISTERED by setup (DISABLED_AGENT_TOOLS).
APP_CONTROLLER_TOOLS = frozenset({
    "analyze", "capture_desktop", "click", "drag",
    "edit_file", "find_files", "find_window", "hotkey", "launch",
    "list_apps", "list_windows", "move_mouse", "press_key", "read_file",
    "read_image", "register_app", "scroll", "type_text",
    "unregister_app", "wait", "wait_for", "write_file",
})

CRON_TOOLS = frozenset({
    "cron_create", "cron_delete", "cron_get", "cron_list",
    "cron_pause", "cron_resume", "cron_trigger", "cron_update",
})

# ioa_* desktop automation tools (toolset "ioa_tools"). Keep in sync with
# the @tool-decorated functions in enikk/ioa_tools.py.
IOA_TOOLS = frozenset({
    "ioa_list_windows", "ioa_pick_window", "ioa_unpick_window",
    "ioa_list_bound_windows", "ioa_switch_window", "ioa_analyze",
    "ioa_click", "ioa_type_text", "ioa_press_key", "ioa_type_keys",
    "ioa_create_file", "ioa_write_file", "ioa_delete_file",
    "ioa_list_my_files", "ioa_parser_status", "ioa_find_app",
    "ioa_launch_app", "ioa_search_kb", "ioa_set_clipboard_text",
    "ioa_get_clipboard_text", "ioa_paste_clipboard_file",
    "ioa_right_click", "ioa_swipe", "ioa_scroll", "ioa_uia_tree",
    "ioa_pick_foreground", "ioa_cleanup",
})

# web_* browser automation tools (toolset "ioa_web_tools"). web_eval_js is
# registered by the module but DEREGISTERED by setup (DISABLED_AGENT_TOOLS).
WEB_TOOLS = frozenset({
    "web_open", "web_snapshot", "web_click", "web_type", "web_press_key",
    "web_select_option", "web_extract", "web_scroll", "web_wait", "web_hover",
    "web_status", "web_close",
})


@pytest.fixture(scope="module")
def agent():
    """A real AIAgent built the same way Eternity.setup() + create_session()
    builds it: registers app_controller / enikk_cron / ioa_tools /
    ioa_web_tools, then deregisters the dangerous tools."""
    from enikk.controller import AppController
    from enikk.cron import register_cron_tools
    from enikk.eternity import ENABLED_TOOLSETS
    from enikk import ioa_tools, web_tools
    from tools.registry import registry

    with patch("enikk.controller.capture"), \
         patch("enikk.controller.input_mod"), \
         patch("enikk.controller.window"), \
         patch("enikk.controller.UIParser"):
        config = MagicMock()
        config.workspace.weights_dir = None
        config.workspace.screenshot_max_dim = 1366
        config.workspace.screenshot_dir = "/tmp/screenshots"
        config.apps = {}
        controller = AppController(config)
        controller.register_tools()
    register_cron_tools()
    ioa_tools.register_ioa_tools()
    web_tools.register_web_tools()
    # Mirror Eternity._disable_agent_dangerous_tools() — the real agent
    # never sees these, so the inventory must not include them either.
    from enikk.eternity import DISABLED_AGENT_TOOLS
    for tool_name in DISABLED_AGENT_TOOLS:
        if registry.get_entry(tool_name) is not None:
            registry.deregister(tool_name)

    return run_agent.AIAgent(
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-test",
        provider="openai",
        model="test-model",
        enabled_toolsets=list(ENABLED_TOOLSETS),
        quiet_mode=True,
        save_trajectories=False,
        skip_memory=True,
    )


def _expected_inventory() -> set[str]:
    """Full expected tool list for ENABLED_TOOLSETS in this environment."""
    from enikk.hermes_tools import REQUIRED_TOOLS
    from hermes_state import DEFAULT_DB_PATH

    expected = (set(APP_CONTROLLER_TOOLS) | set(CRON_TOOLS)
                | set(IOA_TOOLS) | set(WEB_TOOLS) | set(REQUIRED_TOOLS))
    # session_search's check_fn requires the hermes state dir to exist;
    # without it the tool is registered but filtered out of the schema.
    if not DEFAULT_DB_PATH.parent.exists():
        expected.discard("session_search")
    return expected


class TestAgentToolInventory:
    def test_app_controller_tools_present(self, agent):
        missing = APP_CONTROLLER_TOOLS - agent.valid_tool_names
        assert not missing, f"app_controller tools missing: {sorted(missing)}"

    def test_cron_tools_present(self, agent):
        missing = CRON_TOOLS - agent.valid_tool_names
        assert not missing, f"enikk_cron tools missing: {sorted(missing)}"

    def test_ioa_tools_present(self, agent):
        missing = IOA_TOOLS - agent.valid_tool_names
        assert not missing, f"ioa tools missing: {sorted(missing)}"

    def test_web_tools_present(self, agent):
        """The browser toolset must actually reach the model (review #1:
        it was registered but missing from ENABLED_TOOLSETS for weeks)."""
        missing = WEB_TOOLS - agent.valid_tool_names
        assert not missing, f"web tools missing: {sorted(missing)}"

    def test_dangerous_tools_absent(self, agent):
        """DISABLED_AGENT_TOOLS must never reach the model schema."""
        from enikk.eternity import DISABLED_AGENT_TOOLS
        present = DISABLED_AGENT_TOOLS & set(agent.valid_tool_names)
        assert not present, f"dangerous tools leaked into schema: {sorted(present)}"

    def test_hermes_tools_present(self, agent):
        from enikk.hermes_tools import REQUIRED_TOOLS
        from hermes_state import DEFAULT_DB_PATH

        expected = set(REQUIRED_TOOLS)
        if not DEFAULT_DB_PATH.parent.exists():
            expected.discard("session_search")
        missing = expected - agent.valid_tool_names
        assert not missing, f"hermes tools missing: {sorted(missing)}"

    def test_no_tools_outside_enabled_toolsets(self, agent):
        """enabled_toolsets filtering must keep unenabled tools (terminal,
        browser, ...) out of the agent schema."""
        from tools.registry import registry
        from enikk.eternity import ENABLED_TOOLSETS

        for name in sorted(agent.valid_tool_names):
            toolset = registry.get_toolset_for_tool(name)
            assert toolset in ENABLED_TOOLSETS, (
                f"tool {name!r} from toolset {toolset!r} is not in "
                f"ENABLED_TOOLSETS {ENABLED_TOOLSETS}"
            )

    def test_exact_inventory(self, agent):
        """Pin the complete tool list so any change is a conscious update."""
        assert agent.valid_tool_names == _expected_inventory()
