"""ESP32 camera/stylus HTTP boundary over the device's saved calibration.

Input is a normalized phone-screen gesture, never GRBL millimetres. The
device inserts its motor calibration into move_click/drag commands. One
rig lock must serialize all calls, including capture with pre_move=true.
Commands are never retried: a lost acknowledgement leaves the outcome
unknown and disables further motion until an explicit reconnect.
"""

import json
import math
import time
from typing import Any

import cv2
import httpx
import numpy as np

from physiclaw.core.hardware.frame_reader import PublishedFrame

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
ROTATIONS = {
    0: -1,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


class ESP32Device:
    """Own the HTTP session, device identity, frame acquisition and gestures.

    Setup requires a device that has already been visually and mechanically
    calibrated in its own web interface. It does not write that calibration.
    Closing this client cannot cancel a command already accepted by firmware.
    """

    def __init__(self, url: str, *, rotation: int = 270, timeout: float = 30.0):
        target = httpx.URL(url)
        if (
            target.scheme not in {"http", "https"}
            or not target.host
            or target.userinfo
            or target.path not in {"", "/"}
            or target.query
            or target.fragment
        ):
            raise ValueError(
                "hardware.device_url must be an HTTP(S) origin without credentials or a path"
            )
        if rotation not in ROTATIONS or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Invalid ESP32 rotation or timeout")
        self._client = httpx.Client(
            base_url=str(target).rstrip("/"),
            timeout=httpx.Timeout(
                timeout, connect=min(3.0, timeout), pool=min(3.0, timeout)
            ),
            trust_env=False,
            follow_redirects=False,
        )
        self._timeout = timeout
        self._image_rotation = rotation
        self._rotation = ROTATIONS[rotation]
        self._device_id: str | None = None
        self._points: tuple[tuple[float, float], ...] | None = None
        self._closed = False
        self._parked = False
        self.connected = False
        self.action_uncertain = False
        self.frame_size: tuple[int, int] | None = None

    def _read(self, method: str, path: str, *, limit: int, **kwargs) -> bytes:
        if self._closed:
            raise RuntimeError("ESP32 connection is closed")
        started = time.monotonic()
        try:
            with self._client.stream(method, path, **kwargs) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > limit:
                        raise RuntimeError("ESP32 response exceeded its size limit")
                    if time.monotonic() - started > self._timeout:
                        raise TimeoutError("ESP32 response exceeded its time limit")
                    body.extend(chunk)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                "ESP32 request failed; check the device address, network and power"
            ) from exc
        return bytes(body)

    def _json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        result = json.loads(self._read(method, path, limit=MAX_JSON_BYTES, **kwargs))
        if not isinstance(result, dict) or result.get("success") is not True:
            raise RuntimeError(
                "ESP32 rejected the request or returned an invalid acknowledgement"
            )
        return result

    @staticmethod
    def _corners(data: dict[str, Any]) -> tuple[tuple[float, float], ...]:
        points = data.get("points")
        if not isinstance(points, list) or len(points) != 4:
            raise RuntimeError(
                "ESP32 visual calibration is missing; calibrate in the device web page"
            )
        parsed = []
        for point in points:
            if not isinstance(point, dict):
                raise RuntimeError("ESP32 returned invalid calibration points")
            xy: list[float] = []
            for key in ("x", "y"):
                value = point.get(key)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise RuntimeError("ESP32 returned invalid calibration points")
                xy.append(float(value))
            parsed.append((xy[0], xy[1]))
        contour = np.array(parsed, dtype=np.float32)
        if not cv2.isContourConvex(contour) or cv2.contourArea(contour) <= 1:
            raise RuntimeError("ESP32 visual calibration is degenerate")
        return tuple(parsed)

    def check_status(self, *, require_idle: bool = True) -> None:
        """Read status; require a clean controller baseline before any motion.

        serial_status can retain an old error after a successful command.
        Refuse that state BEFORE moving so a later error is not confused
        with history. Stationary capture may inspect an error state.
        """
        result = self._json("GET", "/api/status")
        data = result.get("data")
        if not isinstance(data, dict) or data.get("serial_connected") is not True:
            self.connected = False
            raise RuntimeError("ESP32 motor controller is not connected")
        device_id = data.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise RuntimeError("ESP32 did not identify itself")
        points = self._corners(data)
        if self._device_id is not None and device_id != self._device_id:
            self.connected = False
            raise RuntimeError(
                "ESP32 device identity changed; check the configured address"
            )
        if self._points is not None and points != self._points:
            self.connected = False
            raise RuntimeError(
                "ESP32 calibration changed; reconnect after verifying the phone position"
            )
        if require_idle and (
            data.get("system_state") != "IDLE"
            or data.get("serial_busy")
            or data.get("camera_busy")
            or data.get("audio_busy")
        ):
            raise RuntimeError(
                "ESP32 is busy; wait for its current operation to finish"
            )
        if require_idle:
            serial_status = data.get("serial_status")
            if not isinstance(serial_status, str):
                raise RuntimeError("ESP32 controller completion status is missing")
            if serial_status.strip() not in {"", "READY", "OK"}:
                raise RuntimeError(
                    f"ESP32 controller reports {serial_status[:80]}; clear its error before moving"
                )
        self._device_id, self._points = device_id, points

    def connect(self) -> None:
        """Verify status and obtain one unobstructed, calibrated frame."""
        try:
            self.check_status()
            self.capture(pre_move=True)
            self.connected = True
        except BaseException:
            self.close()
            raise

    def capture(self, *, pre_move: bool = False) -> PublishedFrame:
        """Fetch a fresh rectified portrait frame; pre_move may park the stylus."""
        if pre_move and self.action_uncertain:
            raise RuntimeError(
                "Previous motion outcome is unknown; capture without moving and inspect the device"
            )
        if not pre_move:
            self.check_status(require_idle=False)
        params: dict[str, str | int] = {"_t": int(time.time() * 1000)}
        if pre_move:
            # The device UI omits this argument for a stationary capture.
            # Do not assume that merely passing pre_move=false is equivalent.
            params["pre_move"] = "true"
            self._parked = False
            self.action_uncertain = True
        payload = self._read(
            "GET",
            "/transform",
            limit=MAX_IMAGE_BYTES,
            params=params,
        )
        if not payload:
            raise RuntimeError("ESP32 returned an empty image")
        try:
            frame = cv2.imdecode(
                np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR
            )
        except cv2.error as exc:
            raise RuntimeError("ESP32 did not return a valid JPEG frame") from exc
        if frame is None:
            raise RuntimeError("ESP32 did not return a valid JPEG frame")
        if self._rotation != -1:
            frame = cv2.rotate(frame, self._rotation)
        size = (frame.shape[1], frame.shape[0])
        if self.frame_size is not None and size != self.frame_size:
            self.connected = False
            raise RuntimeError(
                "ESP32 frame dimensions changed; reconnect and verify calibration"
            )
        self.frame_size = size
        if pre_move:
            self._parked = True
            self.action_uncertain = False
        return PublishedFrame(frame, time.time())

    def park(self) -> None:
        """Use firmware capture-avoidance; never guess a raw motor coordinate.

        An uncertain command is deliberately left alone: an automatic park
        would be another motion while the previous result is unknown.
        """
        if self.connected and not self._parked and not self.action_uncertain:
            self.check_status()
            self.capture(pre_move=True)

    def _point(self, x: float, y: float) -> str:
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
            for v in (x, y)
        ):
            raise ValueError("ESP32 screen coordinates must be finite values in [0, 1]")
        # Firmware percentages use the portrait view (270 degrees). This
        # also matches the vendor client's landscape mapping (1-y, x).
        delta = (270 - self._image_rotation) % 360
        if delta == 90:
            x, y = 1 - y, x
        elif delta == 180:
            x, y = 1 - x, 1 - y
        elif delta == 270:
            x, y = y, 1 - x
        # Match the vendor client's four-decimal coordinate precision and
        # omit redundant zeroes. The ESP32 inserts motor bounds and spaces;
        # padded drag coordinates overflow the controller's serial buffer.
        return ",".join(f"{v:.4f}".rstrip("0").rstrip(".") for v in (x, y))

    def _command(self, command: str) -> None:
        if self.action_uncertain:
            raise RuntimeError(
                "Previous ESP32 action outcome is unknown; do not repeat it. Inspect the phone and reconnect"
            )
        self.check_status()
        self._parked = False
        self.action_uncertain = True
        try:
            result = self._json(
                "POST", "/api/command", data={"cmd": command, "wait": "true"}
            )
            if result.get("message") != "Command executed":
                raise RuntimeError("ESP32 did not confirm command execution")
            echo = result.get("cmd")
            if not isinstance(echo, str):
                raise RuntimeError("ESP32 acknowledgement did not identify its command")
            actual, expected = (
                [part.strip() for part in echo.split(",")],
                command.split(","),
            )
            if actual != expected and not (
                len(actual) == len(expected) + 4
                and actual[0] == expected[0]
                and actual[5:] == expected[1:]
            ):
                raise RuntimeError("ESP32 acknowledged a different command")
            # HTTP success only means the ESP32 sent the command. Its
            # controller can still reject it (observed: CMD_TOO_LONG).
            self.check_status()
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            raise RuntimeError(
                f"ESP32 action outcome is unknown ({exc}); do not repeat it. Inspect the phone and reconnect"
            ) from exc
        self.action_uncertain = False

    def press(self, x: float, y: float, gesture: str) -> None:
        """One normalized press; firmware adds its saved motor bounds."""
        presses = {"tap": (1, 100), "double_tap": (2, 100), "long_press": (1, 1200)}
        if gesture not in presses:
            raise ValueError(f"Unsupported press gesture: {gesture}")
        count, duration = presses[gesture]
        self._command(f"move_click,{self._point(x, y)},{count},{duration}")

    def swipe(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        speed: str,
        *,
        start_dwell: float = 0.0,
        end_dwell: float = 0.0,
    ) -> None:
        """A normalized drag. Endpoint holds are not exposed by this firmware."""
        if start_dwell or end_dwell:
            raise RuntimeError(
                "ESP32 firmware does not expose drag endpoint holds; this macro is not supported yet"
            )
        durations = {"slow": 800, "medium": 400, "fast": 200}
        if speed not in durations:
            raise ValueError(f"Unsupported swipe speed: {speed}")
        self._command(
            f"drag,{self._point(*start)},{self._point(*end)},{durations[speed]}"
        )

    def close(self) -> None:
        """Release HTTP resources without issuing further physical commands."""
        if not self._closed:
            self._closed = True
            self.connected = False
            self._client.close()
