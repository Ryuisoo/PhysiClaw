"""Isolated HTTP contract checks; real-device acceptance is a separate step."""

from urllib.parse import parse_qs

import cv2
import httpx
import numpy as np
import pytest

from physiclaw.core.hardware.esp32 import ESP32Device

BASE = "http://device.test:8888"


def status(**overrides):
    data = {
        "device_id": "test-device",
        "serial_connected": True,
        "serial_status": "",
        "system_state": "IDLE",
        "camera_busy": False,
        "audio_busy": False,
        "points": [
            {"x": 0, "y": 0},
            {"x": 12, "y": 0},
            {"x": 12, "y": 8},
            {"x": 0, "y": 8},
        ],
    }
    data.update(overrides)
    return {"success": True, "data": data}


@pytest.fixture
def device(respx_mock):
    respx_mock.get(f"{BASE}/api/status", name="status").respond(json=status())
    pixels = np.repeat(np.arange(96, dtype=np.uint8).reshape(8, 12, 1), 3, axis=2)
    ok, jpeg = cv2.imencode(".jpg", pixels)
    assert ok
    respx_mock.get(f"{BASE}/transform", name="image").respond(
        content=jpeg.tobytes(), headers={"Content-Type": "image/jpeg"}
    )
    device = ESP32Device(BASE)
    device.connect()
    yield device
    device.close()


def acknowledge(request):
    command = parse_qs(request.content.decode())["cmd"][0]
    verb, arguments = command.split(",", 1)
    return httpx.Response(
        200,
        json={
            "success": True,
            "message": "Command executed",
            "cmd": f"{verb}, 10, 20, 110, 220, {arguments}",
        },
    )


def test_connect_rotates_rectified_frame_and_stationary_capture_omits_motion_flag(
    device, respx_mock
):
    assert device.connected
    assert device.frame_size == (8, 12)
    first = respx_mock["image"].calls[0].request
    assert first.url.params["pre_move"] == "true"
    sample = device.capture()
    assert sample.frame.shape == (12, 8, 3)
    raw = cv2.imdecode(
        np.frombuffer(respx_mock["image"].calls[0].response.content, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    np.testing.assert_array_equal(sample.frame, np.rot90(raw))
    assert sample.published_at > 0
    latest = respx_mock["image"].calls[-1].request
    assert "pre_move" not in latest.url.params


def test_press_sends_normalized_values_once_and_waits_for_execution(device, respx_mock):
    route = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    device.press(0.38, 0.67, "tap")
    assert route.call_count == 1
    assert parse_qs(route.calls[0].request.content.decode()) == {
        "cmd": ["move_click,0.38,0.67,1,100"],
        "wait": ["true"],
    }
    assert not device.action_uncertain


@pytest.mark.parametrize(
    "rotation,wire_point",
    [
        (270, "0.2,0.3"),
        (180, "0.7,0.2"),
        (90, "0.8,0.7"),
        (0, "0.3,0.8"),
    ],
)
def test_click_coordinates_follow_the_selected_view_rotation(
    device, respx_mock, rotation, wire_point
):
    alternate = ESP32Device(BASE, rotation=rotation)
    route = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    try:
        alternate.connect()
        alternate.press(0.2, 0.3, "tap")
        assert parse_qs(route.calls[-1].request.content.decode())["cmd"] == [
            f"move_click,{wire_point},1,100"
        ]
    finally:
        alternate.close()


def test_uncertain_action_is_not_retried_or_automatically_parked(device, respx_mock):
    route = respx_mock.post(f"{BASE}/api/command").mock(
        side_effect=httpx.ReadTimeout("lost acknowledgement")
    )
    image_calls = respx_mock["image"].call_count
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        device.press(0.5, 0.5, "tap")
    with pytest.raises(RuntimeError, match="do not repeat"):
        device.press(0.5, 0.5, "tap")
    device.park()
    assert route.call_count == 1
    assert respx_mock["image"].call_count == image_calls
    # A read-only image remains available for inspecting an unknown outcome.
    device.capture()
    assert device.action_uncertain


def test_wrong_command_acknowledgement_does_not_count_as_success(device, respx_mock):
    respx_mock.post(f"{BASE}/api/command").respond(
        json={
            "success": True,
            "message": "Command executed",
            "cmd": "move,0,0",
        }
    )
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        device.press(0.5, 0.5, "tap")
    assert device.action_uncertain


def test_read_only_capture_refuses_a_different_device(device, respx_mock):
    respx_mock["status"].respond(json=status(device_id="another-device"))
    count = respx_mock["image"].call_count
    with pytest.raises(RuntimeError, match="identity changed"):
        device.capture()
    assert respx_mock["image"].call_count == count


@pytest.mark.parametrize(
    "change, message",
    [
        ({"device_id": "another-device"}, "identity changed"),
        ({"serial_connected": False}, "not connected"),
        ({"system_state": "BUSY"}, "busy"),
        (
            {
                "points": [
                    {"x": 1, "y": 1},
                    {"x": 13, "y": 1},
                    {"x": 13, "y": 9},
                    {"x": 1, "y": 9},
                ]
            },
            "calibration changed",
        ),
    ],
)
def test_preflight_refuses_invalid_device_state_before_motion(
    device, respx_mock, change, message
):
    respx_mock.get(f"{BASE}/api/status").respond(json=status(**change))
    route = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    with pytest.raises(RuntimeError, match=message):
        device.press(0.5, 0.5, "tap")
    assert not route.called


def test_swipe_preserves_screen_coordinates_and_rejects_unavailable_holds(
    device, respx_mock
):
    route = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    device.swipe((0.5, 0.75), (0.5, 0.45), "medium")
    assert parse_qs(route.calls[0].request.content.decode())["cmd"] == [
        "drag,0.5,0.75,0.5,0.45,400",
    ]
    with pytest.raises(RuntimeError, match="not supported yet"):
        device.swipe((0.5, 0.75), (0.5, 0.45), "medium", end_dwell=0.5)
    assert route.call_count == 1


def test_drag_uses_compact_vendor_coordinate_precision(device, respx_mock):
    route = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    device.swipe((0.12345, 0.72001), (0.50001, 0.42001), "medium")
    assert parse_qs(route.calls[0].request.content.decode())["cmd"] == [
        "drag,0.1235,0.72,0.5,0.42,400"
    ]


@pytest.mark.parametrize("serial_status", ["CMD_TOO_LONG", "UNKNOWN_COMMAND", None])
def test_http_success_does_not_hide_controller_rejection(
    device, respx_mock, serial_status
):
    respx_mock["status"].mock(
        side_effect=[
            httpx.Response(200, json=status()),
            httpx.Response(200, json=status(serial_status=serial_status)),
        ]
    )
    command = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    with pytest.raises(RuntimeError, match="outcome is unknown") as error:
        device.swipe((0.5, 0.72), (0.5, 0.42), "medium")
    assert (serial_status or "status is missing") in str(error.value)
    assert device.action_uncertain
    with pytest.raises(RuntimeError, match="do not repeat"):
        device.press(0.5, 0.5, "tap")
    device.park()
    assert command.call_count == 1


def test_latched_controller_error_is_refused_before_motion_but_allows_inspection(
    device, respx_mock
):
    respx_mock["status"].respond(json=status(serial_status="CMD_TOO_LONG"))
    command = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    with pytest.raises(RuntimeError, match="clear its error before moving"):
        device.swipe((0.5, 0.72), (0.5, 0.42), "medium")
    assert not device.action_uncertain
    assert not command.called
    device.capture()


def test_connect_with_latched_error_does_not_start_capture_avoidance(respx_mock):
    respx_mock.get(f"{BASE}/api/status").respond(
        json=status(serial_status="CMD_TOO_LONG")
    )
    images = respx_mock.get(f"{BASE}/transform").respond(content=b"unused")
    device = ESP32Device(BASE)
    with pytest.raises(RuntimeError, match="clear its error before moving"):
        device.connect()
    assert not images.called
    assert device._client.is_closed


@pytest.mark.parametrize(
    "x,y", [(float("nan"), 0.5), (-0.1, 0.5), (0.5, 1.1), (True, 0.5)]
)
def test_invalid_screen_coordinates_never_reach_the_device(device, respx_mock, x, y):
    route = respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    with pytest.raises(ValueError, match="screen coordinates"):
        device.press(x, y, "tap")
    assert not route.called


def test_failed_parking_capture_is_not_repeated(device, respx_mock):
    respx_mock.post(f"{BASE}/api/command").mock(side_effect=acknowledge)
    device.press(0.5, 0.5, "tap")
    images = respx_mock.get(f"{BASE}/transform").mock(
        side_effect=httpx.ReadTimeout("camera timeout")
    )
    previous_calls = images.call_count
    with pytest.raises(RuntimeError, match="request failed"):
        device.park()
    assert device.action_uncertain
    device.park()
    assert images.call_count == previous_calls + 1


def test_bad_calibration_never_captures_or_moves(respx_mock):
    respx_mock.get(f"{BASE}/api/status").respond(
        json=status(points=[{"x": 0, "y": 0}] * 4)
    )
    images = respx_mock.get(f"{BASE}/transform").respond(content=b"not an image")
    device = ESP32Device(BASE)
    with pytest.raises(RuntimeError, match="degenerate"):
        device.connect()
    assert not images.called
    assert device._client.is_closed


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://device.test",
        "http://user:secret@device.test",
        "http://device.test/path",
    ],
)
def test_device_origin_rejects_credentials_and_non_origin_urls(url):
    with pytest.raises(ValueError, match="HTTP\\(S\\) origin"):
        ESP32Device(url)
