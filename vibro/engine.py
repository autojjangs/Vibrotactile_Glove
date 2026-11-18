"""Deterministic experiment rules. No Bluetooth, sleeps or hardware imports."""
from . import protocol as p

class Engine:
    def __init__(self, condition, log):
        if condition not in ("baseline", "proposed"):
            raise ValueError("condition must be baseline or proposed")
        self.condition, self.log = condition, log
        self.running = False
        self.active = None
        self.allowed = False
        self.lit = False
        self.cancelled = False
        self.pre_ref = None
        self.preview_request_at = None
        self.contact = {"left": False, "right": False}

    def state(self):
        return dict(running=self.running, allowed=self.allowed, lit=self.lit,
                    cancelled=self.cancelled, point=self.active["id"] if self.active else None)

    def record(self, event, received=None, point=None, **fields):
        point = point or self.active or {}
        self.log.write(event, received=received, point=point.get("id", ""),
                       direction=point.get("direction", ""),
                       expected_action=point.get("expected_action", ""), **fields)

    def start(self):
        self.running = True
        self.record("trial_start")

    def scheduled(self, kind, point, at):
        if not self.running:
            return []
        if kind == "cue":
            self.record("video_cue_marker", received=at, point=point,
                        details="Scheduled annotation; video rendering is not measured")
            return []
        if kind == "preview":
            self.active = point
            self.allowed = self.lit = self.cancelled = False
            self.pre_ref = None
            self.preview_request_at = at
            self.record("preview_requested", received=at)
            return [("haptic", point["direction"], p.PREVIEW)]
        if kind == "auto" and self.condition == "proposed":
            if self.cancelled:
                self.record("auto_suppressed", received=at, reason="cancelled")
                return []
            if not self.allowed:
                raise RuntimeError("First preview vibration was not acknowledged before auto-on")
            self.lit = True
            self.record("auto_on", received=at, after=self.state())
            return [("led", point["direction"], True), ("haptic", point["direction"], p.FEEDBACK)]
        if kind == "pass":
            # Passing ends the indicator and feedback normally in both conditions.
            self.record("point_pass", received=at, before=self.state())
            side = point["direction"]
            self.active = None
            self.allowed = self.lit = self.cancelled = False
            return [("led", side, False), ("haptic", side, p.STOP)]
        if kind == "end":
            self.running = False
            self.record("trial_complete", received=at)
        return []

    def packet(self, packet, at):
        if packet.kind == p.STATE:
            self.contact[packet.side] = bool(packet.value)
            self.record("contact_snapshot", received=at, side=packet.side, input=packet.value,
                        reason="synchronization_only")
            return []
        if packet.kind == p.HAPTIC:
            self.record("haptic_applied", received=at, side=packet.side,
                        device_ms=packet.device_ms, reference=packet.reference,
                        details={"mode": packet.value})
            if (self.running and self.active and packet.side == self.active["direction"]
                    and packet.value == p.PREVIEW and packet.reference == self.pre_ref):
                self.allowed = True
                self.record("input_window_open", received=at, after=self.state())
            return []
        if packet.kind != p.CONTACT:
            return []
        before = self.state()
        old = self.contact[packet.side]
        pressed = bool(packet.value)
        self.contact[packet.side] = pressed
        reason = ""
        effects = []
        result = "ignored"
        if old == pressed:
            reason = "duplicate_state"
        elif not pressed:
            reason, result = "release_no_action", "recorded"
        elif not self.running:
            reason = "trial_not_running"
        elif not self.active:
            reason = "outside_point"
        elif not self.allowed:
            reason = "before_first_vibration"
        elif packet.side != self.active["direction"]:
            reason = "wrong_hand"
        elif self.condition == "baseline":
            if self.lit:
                reason = "already_manual_on"
            else:
                self.lit = True
                result = "on"
                effects = [("led", packet.side, True), ("haptic", packet.side, p.FEEDBACK)]
        elif self.cancelled:
            reason = "already_cancelled"
        elif self.lit:
            reason = "after_automatic_on"
        else:
            self.cancelled = True
            result = "cancel"
            # Cancellation is latched. The remaining two-pulse preview still completes.
        self.record("input", received=at, side=packet.side, input="contact" if pressed else "release",
                    device_ms=packet.device_ms, sequence=packet.sequence, reference=packet.reference,
                    before=before, after=self.state(), result=result, reason=reason)
        return effects

    def abort(self, reason):
        self.record("trial_aborted", before=self.state(), reason=reason, result="invalid_trial")
        self.running = False
        self.active = None
        self.allowed = self.lit = self.cancelled = False
