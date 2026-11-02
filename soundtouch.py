"""Bose SoundTouch side-channel for DLNA renderers that are SoundTouch speakers.

A SoundTouch (e.g. the garage SA-4) exposes a normal UPnP MediaRenderer, which
bulb_voice drives as usual, plus its own HTTP API on port 8090. The renderer
alone is not enough:

- The speaker has TWO volumes: its own (port 8090 /volume) and the UPnP
  renderer's. The renderer volume drops to 0 every time the speaker goes to
  standby, so a stream started then is silent, and a 0 read in standby is not
  "the user turned it down".
- From standby it must be woken with the POWER key first. POWER is a toggle,
  so it is only pressed when /now_playing reports source STANDBY.
- Mute is a toggle key too; its state is in /volume <muteenabled>.

Verified on the garage SA-4 (bose_out project, 2026-10-09/10).
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

log = logging.getLogger("bulb_voice")

ST_PORT = 8090
WAKE_TIMEOUT = 10.0   # seconds to wait for the speaker to leave STANDBY
WAKE_SETTLE = 1.0     # extra settle time before it accepts a URI


class SoundTouch:
    def __init__(self, host: str, timeout: float = 4.0):
        self.host = host
        self.base = f"http://{host}:{ST_PORT}"
        self._timeout = timeout

    @classmethod
    async def probe(cls, host: str) -> "SoundTouch | None":
        """A SoundTouch if `host` answers the SoundTouch /info API, else None."""
        if not host:
            return None
        st = cls(host, timeout=3.0)
        try:
            if "deviceID" in await st._get("/info"):
                return st
        except Exception as e:
            log.debug("SoundTouch probe %s: %s", host, e)
        return None

    async def _get(self, path: str) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(self.base + path)
            r.raise_for_status()
            return r.text

    async def _post(self, path: str, body: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            # No Content-Type, exactly like libsoundtouch (proven against the
            # SA-4's RomPager server).
            r = await c.post(self.base + path, content=body.encode())
            r.raise_for_status()

    async def press(self, key: str) -> None:
        for st in ("press", "release"):
            await self._post("/key", f'<key state="{st}" sender="Gabbo">{key}</key>')

    async def now_playing(self) -> dict:
        """{'source': 'STANDBY'|'UPNP'|..., 'play_status': ...}"""
        xml = await self._get("/now_playing")
        src = re.search(r'<nowPlaying[^>]*\bsource="([A-Z_]+)"', xml)
        ps = re.search(r"<playStatus>([A-Z_]+)</playStatus>", xml)
        return {"source": src.group(1) if src else "", "play_status": ps.group(1) if ps else ""}

    async def is_standby(self) -> bool:
        return (await self.now_playing())["source"] == "STANDBY"

    async def volume(self) -> tuple[int, bool]:
        """(actual volume 0-100, muted)."""
        xml = await self._get("/volume")
        vol = re.search(r"<actualvolume>(\d+)</actualvolume>", xml)
        mute = re.search(r"<muteenabled>(true|false)</muteenabled>", xml)
        return (int(vol.group(1)) if vol else 0, bool(mute and mute.group(1) == "true"))

    async def set_volume(self, level: int) -> None:
        level = max(0, min(100, int(level)))
        await self._post("/volume", f"<volume>{level}</volume>")

    async def wake(self) -> bool:
        """Leave standby if needed. True if it had to be woken."""
        if not await self.is_standby():
            return False
        log.info("SoundTouch %s: waking from standby", self.host)
        await self.press("POWER")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAKE_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(0.5)
            try:
                if not await self.is_standby():
                    break
            except Exception:
                pass  # the speaker's API is flaky while it boots; keep polling until the deadline
        await asyncio.sleep(WAKE_SETTLE)
        return True

    async def standby(self) -> None:
        if not await self.is_standby():
            await self.press("POWER")

    async def prepare(self, level: int) -> None:
        """Make the speaker ready to play audibly: awake, unmuted, at `level`."""
        await self.wake()
        _, muted = await self.volume()
        if muted:
            await self.press("MUTE")
        await self.set_volume(level)
