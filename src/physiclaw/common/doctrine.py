"""Doctrine `{{token}}` rendering — shared by every doctrine delivery path.

Doctrine files quote engine-enforced thresholds via `{{token}}`
placeholders so the prose can never drift from the config the engine
actually enforces. Two paths deliver doctrine and both must render:
the engine's SYSTEM composer (`agent.engine.prompt`) and the MCP
`initialize` instructions (`core.server.mcp`, which ships
`PHYSICLAW.md` to external clients).

Lives in common because core must not import agent (see
`tests/test_architecture.py`) — before this module existed the renderer
was private to `agent.engine.prompt`, and the MCP path shipped a
`{{token}}` in `PHYSICLAW.md` to external clients as literal braces.

Values come from CONFIG, which is process-constant, so rendered bytes
are stable across calls — both the engine's prompt cache and the MCP
instructions computed once at import stay byte-stable.
"""

import logging
import re

from physiclaw.common.config import CONFIG

log = logging.getLogger(__name__)

# Engine-enforced thresholds quoted by the doctrine files.
DOCTRINE_TOKENS = {
    "{{same_target_warn}}": str(CONFIG.engine.same_target_warn),
    "{{same_target_block}}": str(CONFIG.engine.same_target_block),
    "{{step_stuck_warn}}": str(CONFIG.engine.step_stuck_warn),
    "{{step_stuck_urgent}}": str(CONFIG.engine.step_stuck_urgent),
    "{{plan_required_after}}": str(CONFIG.engine.plan_required_after),
}


def _hardware_guidance() -> str:
    """Describe the configured hardware without changing engine thresholds."""
    return (
        "This rig uses ESP32 HTTP hardware with device-owned calibration. Use `peek`, "
        "`tap`, `double_tap`, `long_press`, `swipe`, `home_screen`, and supported "
        "`sequence`/macro steps through the normal tools. Motor units are private to "
        "the device: never send raw motor coordinates. `screenshot` and "
        "`send_to_clipboard` are not connected yet; enter text through grounded "
        "screen-keyboard targets when feasible. `go_back` and `force_quit` currently "
        "require drag holds that this firmware does not expose; use grounded in-app "
        "navigation instead. If an action reports an unknown outcome, inspect with "
        "`peek` and ask the operator to resolve/reconnect; never repeat that action. "
        "Do not replace the user's task with a fixed script: observe, act, inspect "
        "the attached result and replan. Existing scripts/macros are optional tools "
        "for suitable fixed steps. If the camera cannot ground a target, ask for "
        "help rather than calling an unavailable screenshot tool."
        " `unlock_phone` assumes the original test passcode 111111; only call it "
        "after the operator confirms that exact test-phone setup. For another "
        "passcode, ask the operator to unlock manually; never try the test code."
        if CONFIG.hardware.backend == "esp32"
        else "`screenshot` takes a phone capture and **changes the phone state**; apps may "
        "open overlays. If the camera listing misses a target, take one screenshot, "
        "save its bbox in the scratchpad, then `peek` to check for side effects before "
        "using it. Re-ground if the layout changed."
    )


def fill_tokens(name: str, body: str) -> str:
    """Substitute `{{token}}` placeholders; warn on leftovers (a typo'd
    token would otherwise reach the model as literal braces). `name` is
    the source label for the warning (e.g. the doctrine filename).

    NOTE: skill `{{box}}` placeholders are a different token system
    (learned screen elements, resolved by `agent.layout`) — never pass skill bodies through this.
    """
    for token, value in DOCTRINE_TOKENS.items():
        body = body.replace(token, value)
    body = body.replace("{{hardware_tool_guidance}}", _hardware_guidance())
    leftover = re.search(r"\{\{[^}\n]*\}\}|\{\{", body)
    if leftover:
        log.warning(
            "doctrine %s: unresolved placeholder %r",
            name,
            leftover.group(),
        )
    return body
