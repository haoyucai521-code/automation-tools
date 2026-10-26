"""Logical reset polarity and cancellation-safe pulse control."""

from embedded_auto.core.errors import ToolError


class ResetController:
    def __init__(self, session, config: dict, ctx):
        self.session = session
        self.config = config
        self.ctx = ctx
        self.state = 'UNKNOWN'

    def _set(self, asserted):
        try:
            active = self.config['reset']['asserted_rts_value']
            self.session.set_rts(active if asserted else not active)
        except Exception as exc:
            self.state = 'UNKNOWN'
            raise ToolError('RESET_CONTROL_FAILED', str(exc), {'requested_state': 'ASSERTED' if asserted else 'RELEASED'}) from exc
        self.state = 'ASSERTED' if asserted else 'RELEASED'
        self.ctx.event('RESET_' + self.state, rts_value=active if asserted else not active)

    def assert_reset(self):
        self.ctx.check()
        self._set(True)

    def release_reset(self):
        # Cleanup must remain possible after cancellation or deadline expiry.
        self._set(False)

    def pulse(self, duration_ms=None):
        duration_ms = self.config['reset'].get('pulse_ms', 100) if duration_ms is None else duration_ms
        original = None
        try:
            self.assert_reset()
            self.ctx.sleep(duration_ms / 1000)
        except BaseException as exc:
            original = exc
            raise
        finally:
            try:
                self.release_reset()
            except Exception as exc:
                if original is None:
                    raise
                if isinstance(original, ToolError):
                    original.data = dict(original.data or {}, reset_release_error=str(exc))
                else:
                    original.add_note('Reset release failed: ' + str(exc))
        delay = self.config['reset'].get('post_pulse_delay_ms', 0)
        if delay:
            self.ctx.sleep(delay / 1000)
