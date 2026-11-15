import asyncio

class LedOutput:
    """One serialized blink task. Stop awaits cancellation before any new task."""
    def __init__(self, settings, simulated, log, fault):
        self.config, self.simulated, self.log, self.fault = settings["led"], simulated, log, fault
        self.strip = None
        self.task = None
        self.direction = None

    def initialize(self):
        if self.simulated:
            return
        from rpi_ws281x import PixelStrip, Color
        self.Color = Color
        c = self.config
        self.strip = PixelStrip(c["count"], c["gpio"], 800000, c["dma"],
                               False, c["brightness"], c["channel"])
        self.strip.begin()
        self.show(None)

    def show(self, direction):
        if not self.simulated:
            active = set(self.config[f"{direction}_indices"]) if direction else set()
            for i in range(self.config["count"]):
                self.strip.setPixelColor(i, self.Color(*self.config["color"]) if i in active else self.Color(0, 0, 0))
            self.strip.show()

    async def set(self, side, enabled):
        await self.stop()
        if enabled:
            self.direction = side
            self.show(side)
            self.log.write("led_driver_on", side=side, details={"simulated": self.simulated})
            self.task = asyncio.create_task(self.blink(side))

    async def blink(self, side):
        try:
            while True:
                await asyncio.sleep(self.config["on_ms"] / 1000)
                self.show(None)
                await asyncio.sleep(self.config["off_ms"] / 1000)
                self.show(side)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fault(f"LED driver: {error}")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.strip is not None or self.simulated:
            self.show(None)
        if self.direction:
            self.log.write("led_driver_off", side=self.direction, details={"simulated": self.simulated})
        self.direction = None
