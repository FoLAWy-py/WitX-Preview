"""Local ports for each device's Appium session. Devices on one Appium must not share a
port: a WebDriverAgent port shared by two iOS devices once sent one device's input to
the other. Phones are numbered in the order the Gateway first meets them, whatever
their platform, and keep their ports while it runs: the driver's own server from 8100 and
the screen broadcast it forwards from 9100."""

# Where the Gateway looks for Appium unless the configuration says otherwise.
DEFAULT_APPIUM_URL = "http://127.0.0.1:4723"
DRIVER_BASE = 8100
BROADCAST_BASE = 9100
# The capability that names the driver's server port: WebDriverAgent on iOS, the
# UiAutomator2 server on Android.
DRIVER_PORT = {"android": "appium:systemPort", "ios": "appium:wdaLocalPort"}


class Ports:
    def __init__(self) -> None:
        self._taken = 0

    def take(self, platform: str) -> dict[str, int] | None:
        """The next free ports for a device of this platform (None: it needs none)."""
        if platform not in DRIVER_PORT:
            return None
        index, self._taken = self._taken, self._taken + 1
        return {
            DRIVER_PORT[platform]: DRIVER_BASE + index,
            "appium:mjpegServerPort": BROADCAST_BASE + index,
        }
