# PhysiClaw

**An AI agent that physically operates a phone — the way you do.**

[English docs](https://docs.physiclaw.ai/en/) · [Demo video](https://youtu.be/YJ1O6gW0KVw) · [Assembly video](https://youtu.be/Acmta1kzEko)

[中文文档](https://docs.physiclaw.ai/zh/) · [演示视频](https://b23.tv/jnegfgC) · [装配视频](https://www.bilibili.com/video/BV1b7av68E77/)

PhysiClaw watches a phone's screen with a camera and taps it with a stylus,
working the phone the way a person would. No APIs, no OAuth, no ADB cables,
nothing installed on the phone — just unlock it, set it on the desk, and let
the agent work.

It's built for the everyday errands that pile up — ordering takeout, shopping
for groceries, booking a ride, paying a bill, replying to a message. Anything
you can do by hand on your phone, PhysiClaw can do for you.

## Why a physical body?

The apps that run your daily life are closed off: most expose no public API,
and simulated input — desktop automation or Android's ADB — leaves software
fingerprints that anti-bot systems flag. So PhysiClaw treats the screen itself
as the API: a camera reads it, a stylus performs the gestures. To the phone
it's indistinguishable from a real finger — nothing to detect — and it reaches
virtually any app reliably.

The trade-off is speed: a few seconds per action, in exchange for universality
and reliability.

## How it works

PhysiClaw has its own dedicated phone running its own WhatsApp account.
Add it as a contact and chat with it like a real person — tell it what you need
in plain language:

```text
Order a latte on DoorDash.
What's the weather tomorrow?
Buy milk and eggs on Instacart.
```

Its runtime loops continuously, waking the agent when a scheduled task is due
or the screen lights up with a new message. The agent unlocks the phone, reads
your message, and does the task — pausing for confirmation when it matters,
like a payment — then replies with the result, saves its memory, and exits
until it's next woken.

## Getting started

You'll need the PhysiClaw hardware — the arm, stylus, and camera — plus an
**iPhone** for it to operate.

The `physiclaw` CLI runs on macOS, Windows, and Linux:

```bash
# Install the CLI (uv + Python 3.12 + physiclaw)
curl -fsSL https://physiclaw.ai/install.sh | bash
# Windows: iwr -useb https://physiclaw.ai/install.ps1 | iex

physiclaw models key <provider>        # add your LLM API key
physiclaw models use <provider/model>  # pick the model the agent runs on
physiclaw doctor                       # check your environment
physiclaw                              # start the server — opens the hardware-setup wizard, then runs the agent
```

## ESP32 HTTP hardware

An additional hardware backend supports calibrated devices exposing the
`/api/status`, `/transform`, and `/api/command` camera/stylus API with
`move_click` and `drag` commands. This is a firmware protocol, not generic
support for every ESP32 board. The original USB camera/GRBL backend remains
the default.

First align the camera corners and motor endpoints in the device's own web
page and verify them against the fixed phone. Keep the camera module and
motor controller powered. PhysiClaw reads the saved calibration; it does not
replace it with a GRBL millimetre mapping.

From a source checkout, use a separate environment and data home. Replace
`DEVICE_IP` with the device address and choose an unused control port; the
phone-plane listener uses the next port.

```bash
uv sync --locked --python 3.12
export PHYSICLAW_HOME="$PWD/data/esp32-dev"
uv run --locked physiclaw config set server.port 8148
uv run --locked physiclaw config set hardware.backend esp32
uv run --locked physiclaw config set hardware.device_url http://DEVICE_IP:8888
uv run --locked physiclaw config set hardware.image_rotation 270
uv run --locked physiclaw setup local-vision-model
uv run --locked physiclaw mcp
```

Rotation is clockwise degrees applied to the device-rectified image; `270`
matches a camera image with the phone's top at the right. Pointer coordinates
are converted back to the firmware's portrait control frame. Confirm that the
portrait view and touch axes agree before operating apps. `mcp` starts the
normal MCP endpoint without a built-in model runtime. It connects the HTTP
device automatically, unless `--no-setup-hardware` defers setup. The local
USB/GRBL setup wizard and calibration routes do not apply to this backend.
Serial `testdrive` and non-preview FluidNC `flash` are refused in HTTP mode.

The existing MCP tool names, arguments and result envelopes are retained.
Use an MCP-capable agent to describe an outcome, observe with `peek`, act on
grounded targets, and inspect the attached post-action view. Existing
`sequence` calls and macros remain optional tools for suitable fixed steps;
they are not a replacement for the agent's observe/act/replan loop.

The first HTTP implementation provides camera views, single/double/long
presses, ordinary swipes and the no-hold home gesture.
The HTTP home recipe starts at 99.5% of screen height with a slow stroke;
verify the saved bottom motor endpoint against the actual display edge.
The returned launcher view may be App Library rather than an ordinary Home
Screen page. Inspect it: App Library offers installed-app search at the top;
swipe right to return to an ordinary page and verify the result.
Native iPhone screenshots and clipboard transfer are deferred to a later bridge
integration. The firmware API does not expose drag endpoint holds, so
`go_back` and `force_quit` recipes requiring them report an explicit error;
use grounded in-app navigation instead. Unsupported steps are never silently
skipped. The selected capabilities are reported in `/api/status` and in the
agent's hardware instructions.

`unlock_phone` retains the original fixed test passcode `111111`. Do not run
it on a phone using another passcode; unlock that phone manually. This
backend does not require changing the phone's passcode or enabling Shortcuts
for the physical gesture tools.

Use only one active controller for the device. Motion requests are never
automatically retried. If acknowledgement is lost, the action is marked
unknown, further gestures are refused, and read-only camera observation
remains available. Inspect the phone and explicitly reconnect/restart after
resolving it. The firmware can retain an old controller error in its status
even after replying READY to a later command. Clear that device error before
connecting: PhysiClaw requires a clean status before motion so it can detect
new errors without attributing old ones to the current command. After checking
the controller, restarting the camera ESP32 may be needed to clear its cached
status. Closing an HTTP connection cannot cancel firmware motion or
provide an emergency stop; keep the physical motor-power disconnect accessible.

## License

[MIT](LICENSE) — both the CAD-as-code hardware and the agent software.
