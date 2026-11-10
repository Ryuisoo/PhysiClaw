"""HTTP rig dispatch and lifecycle checks, independent of real hardware."""

from unittest.mock import create_autospec

import numpy as np
import pytest

from physiclaw.common.config import HardwareConfig
from physiclaw.core.hardware.esp32 import ESP32Device
from physiclaw.core.hardware.frame_reader import PublishedFrame
from physiclaw.core.orchestration import rig as rig_module
from physiclaw.core.orchestration.orchestrator import PhysiClaw


@pytest.fixture
def connected_http_pc(monkeypatch, mocker):
    monkeypatch.setattr(
        rig_module.CONFIG,
        "hardware",
        HardwareConfig(
            backend="esp32",
            device_url="http://device.test:8888",
        ),
    )
    device = create_autospec(ESP32Device, instance=True)
    device.connected = True
    device.action_uncertain = False
    device.frame_size = (4, 6)
    device.capture.return_value = PublishedFrame(
        np.zeros((6, 4, 3), dtype=np.uint8), 123.0
    )
    mocker.patch.object(rig_module, "ESP32Device", return_value=device)
    local_arm = mocker.patch.object(rig_module, "StylusArm")
    local_camera = mocker.patch.object(rig_module, "Camera")
    pc = PhysiClaw()
    with pc.rig.locked():
        pc.rig.connect_device()
    assert pc.become_ready()
    yield pc, device
    pc.shutdown()
    local_arm.assert_not_called()
    local_camera.assert_not_called()


def test_ready_does_not_invent_grbl_calibration(connected_http_pc):
    pc, _ = connected_http_pc
    assert pc.rig.ready
    assert pc.rig.calibration.pct_to_grbl is None
    assert not pc.rig.origin_pinned
    assert pc.rig.arm is None and pc.rig.cam is None
    assert pc.rig.status()["backend"] == "esp32"
    with pytest.raises(RuntimeError, match="GRBL millimetre"):
        pc.rig.require_transforms()


def test_press_dispatches_screen_coordinates_without_raw_motion(connected_http_pc):
    pc, device = connected_http_pc
    with pc.rig.locked():
        pc.rig.press_bbox([0.2, 0.3, 0.6, 0.5], "tap")
    device.press.assert_called_once_with(0.4, 0.4, "tap")


def test_swipe_uses_same_geometry_as_gesture_validation(connected_http_pc):
    pc, device = connected_http_pc
    with pc.rig.locked():
        pc.rig.swipe_from_bbox([0.2, 0.3, 0.6, 0.5], "up", 0.2, "medium")
    start, end, speed = device.swipe.call_args.args
    assert start == pytest.approx((0.4, 0.4))
    assert end == pytest.approx((0.4, 0.2))
    assert speed == "medium"


def test_home_screen_uses_the_http_bottom_edge_without_changing_plain_swipes(
    connected_http_pc, mocker
):
    pc, device = connected_http_pc
    # Only suppress image acquisition/detection in this unit test; the
    # public macro still acquires the rig lock and uses normal dispatch.
    mocker.patch.object(pc._observer, "with_view", side_effect=lambda act: act())
    pc.home_screen()
    start, end, speed = device.swipe.call_args.args
    assert start == pytest.approx((0.5, 0.995))
    assert end == pytest.approx((0.5, 0.245))
    assert speed == "slow"
    assert device.swipe.call_args.kwargs == {"start_dwell": 0.0, "end_dwell": 0.0}


def test_observation_binds_rectified_pixels_to_their_timestamp(connected_http_pc):
    pc, device = connected_http_pc
    with pc.rig.locked():
        view = pc.perception.cropped_snapshot()
    np.testing.assert_array_equal(view.frame, device.capture.return_value.frame)
    assert view.published_at == 123.0
    assert pc.rig.transforms.pixel_to_pct(2, 3) == (0.5, 0.5)


def test_unknown_action_is_not_reported_ready_but_allows_read_only_observation(
    connected_http_pc,
):
    pc, device = connected_http_pc
    device.action_uncertain = True
    assert not pc.rig.ready
    assert pc.rig.status()["action_uncertain"]
    with pc.rig.locked():
        assert pc.rig.capture_frame() is not None


def test_bridge_tools_fail_explicitly_before_queuing_text(connected_http_pc):
    pc, device = connected_http_pc
    with pytest.raises(RuntimeError, match="bridge is not connected"):
        pc.send_to_clipboard("sample-text")
    with pytest.raises(RuntimeError, match="bridge is not connected"):
        pc.screenshot()
    device.press.assert_not_called()
    assert not pc.rig.status()["capabilities"]["clipboard"]


def test_shutdown_closes_http_owner_once(connected_http_pc):
    pc, device = connected_http_pc
    pc.shutdown()
    pc.shutdown()
    device.close.assert_called_once()
    assert not pc.rig.ready
