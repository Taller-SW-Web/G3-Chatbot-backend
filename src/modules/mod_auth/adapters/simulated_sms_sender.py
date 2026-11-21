"""Simulated SMS adapter (SPEC-04): logs the OTP in development only.

No real provider exists for this cycle. The code is always logged at DEBUG in
development and never logged in production. With `MODO_QA=true` the use case may
also echo the code in the response for automated tests.
"""
import logging

from src.modules.mod_auth.ports.sms_sender_port import SmsSenderPort

logger = logging.getLogger(__name__)


class SimulatedSmsSender(SmsSenderPort):
    def __init__(self, *, log_code: bool = False):
        self._log_code = log_code
        self.sent: list[tuple[str, str]] = []

    async def enviar(self, phone: str, code: str) -> None:
        self.sent.append((phone, code))
        if self._log_code:
            logger.info("SimulatedSmsSender: OTP for %s is %s", phone, code)
        else:
            logger.debug("SimulatedSmsSender: OTP generated for %s", phone)
