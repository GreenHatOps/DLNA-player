from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
import time
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import imageio_ffmpeg
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from typing import TYPE_CHECKING

from async_upnp_client.aiohttp import AiohttpRequester
from async_upnp_client.client_factory import UpnpFactory

from soundtouch import SoundTouch
from ytmusic import YTM

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice, UpnpService

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SERVER_PORT = 8000
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
STATE_FILE = Path(__file__).parent / "cache" / "state.json"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# Renderers SSDP doesn't find (the garage SoundTouch SA-4 never answers the
# MediaRenderer search): comma-separated UPnP description URLs, always listed.
STATIC_DEVICES = [u.strip() for u in os.environ.get("BV_STATIC_DEVICES", "").split(",") if u.strip()]
# Downloaded MP3s are evicted oldest-first (never ones still in the queue)
# once the cache grows past this. 0 disables the cap.
CACHE_MAX_MB = int(os.environ.get("BV_CACHE_MAX_MB", "3000"))
# YouTube Music browser-headers file (owner creates it once with
# `ytmusicapi browser --file /app/cache/ytmusic_browser.json`); anonymous without it.
YTM_AUTH_FILE = CACHE_DIR / "ytmusic_browser.json"
GARAGE_PRESETS_FILE = Path(__file__).parent / "garage_presets.json"


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


SERVER_IP = get_lan_ip()
BASE_URL = f"http://{SERVER_IP}:{SERVER_PORT}"

log = logging.getLogger("bulb_voice")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Ring buffer for UI log viewer
from collections import deque
_log_buffer: deque[dict] = deque(maxlen=100)


class _UILogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        _log_buffer.append({
            "ts": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


log.addHandler(_UILogHandler())

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10),
    follow_redirects=True,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class Track:
    id: str
    title: str
    artist: str
    source_type: str  # "youtube", "radio", "url"
    source_url: str
    duration: int = 0
    local_path: str = ""  # path to downloaded file (youtube)
    direct_url: str = ""  # for radio/url streams
    content_type: str = "audio/mpeg"
    content_length: int = 0


@dataclass
class DownloadProgress:
    total: int = 0
    done: int = 0
    failed: int = 0
    current_title: str = ""
    track_ids: list[str] = field(default_factory=list)  # IDs of tracks in this batch
    started_at: float = 0.0  # time.time() when download batch started


@dataclass
class PlayerState:
    queue: list[Track] = field(default_factory=list)
    current_index: int = -1
    volume: int = 30
    play_mode: str = "NORMAL"  # NORMAL, REPEAT_ALL, REPEAT_ONE, SHUFFLE
    download: DownloadProgress = field(default_factory=DownloadProgress)
    last_device: str = ""  # friendly name of last connected device
    last_device_url: str = ""  # description URL of last connected device


def _save_state():
    """Persist queue and playback position to disk."""
    data = {
        "current_index": state.current_index,
        "volume": state.volume,
        "play_mode": state.play_mode,
        "last_device": state.last_device,
        "last_device_url": state.last_device_url,
        "queue": [
            {
                "id": t.id, "title": t.title, "artist": t.artist,
                "source_type": t.source_type, "source_url": t.source_url,
                "duration": t.duration, "local_path": t.local_path,
                "direct_url": t.direct_url, "content_type": t.content_type,
                "content_length": t.content_length,
            }
            for t in state.queue
        ],
    }
    STATE_FILE.write_text(json.dumps(data))


def _load_state():
    """Restore queue from disk if available."""
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text())
        for t in data.get("queue", []):
            # Skip tracks whose cached file is missing
            if t.get("local_path") and not Path(t["local_path"]).exists():
                continue
            state.queue.append(Track(**t))
        state.current_index = data.get("current_index", -1)
        state.volume = data.get("volume", 30)
        state.play_mode = data.get("play_mode", "NORMAL")
        state.last_device = data.get("last_device", "")
        state.last_device_url = data.get("last_device_url", "")
        # Clamp index to valid range
        if state.current_index >= len(state.queue):
            state.current_index = len(state.queue) - 1 if state.queue else -1
        log.info("Restored %d tracks from saved state (last device: %s)",
                 len(state.queue), state.last_device or "none")
    except Exception as e:
        log.warning("Failed to load state: %s", e)


class AddRequest(BaseModel):
    url: str
    type: str = "auto"


class VolumeRequest(BaseModel):
    level: int


class PlayRequest(BaseModel):
    track_id: str | None = None


class SearchRequest(BaseModel):
    query: str
    max_results: int = 10


class DeviceSelectRequest(BaseModel):
    name: str


class SeekRequest(BaseModel):
    position: int  # seconds


class PlayModeRequest(BaseModel):
    mode: str  # NORMAL, REPEAT_ALL, REPEAT_ONE, SHUFFLE


class YtmEnqueueRequest(BaseModel):
    kind: str            # song | video | playlist | album | radio
    id: str              # videoId (song/video/radio) | playlistId | album browseId
    title: str = ""      # for single songs/videos (avoids a lookup)
    artist: str = ""
    duration: int = 0
    mode: str = "end"    # now | next | end


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_state()
    # Immediately nudge last device to prevent deep sleep during restart gap
    if state.last_device_url:
        asyncio.create_task(_nudge_device(state.last_device_url))
    asyncio.create_task(_register_static_devices())
    asyncio.create_task(connect_loop())
    asyncio.create_task(auto_advance_loop())
    asyncio.create_task(keepalive_loop())
    log.info("Server at %s", BASE_URL)
    log.info("Open on your phone: %s", BASE_URL)
    yield
    # Ping device repeatedly before shutdown to keep it awake for restart
    if state.last_device_url:
        for _ in range(3):
            await _nudge_device(state.last_device_url)
            await asyncio.sleep(1)
    _save_state()


async def _nudge_device(url: str):
    """Send a lightweight HTTP GET to keep the device's WiFi stack awake."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.get(url)
    except Exception:
        pass


async def connect_loop():
    """Discover DLNA renderers via SSDP and connect to the first one found."""
    # On first run, try direct connect to last device (faster than SSDP)
    if av_transport is None and state.last_device_url:
        try:
            log.info("Fast reconnect to %s", state.last_device_url)
            await connect_to_device(state.last_device_url)
        except Exception as e:
            log.debug("Fast reconnect failed, falling back to SSDP: %s", e)
    while True:
        try:
            if av_transport is None:
                await discover_and_connect()
        except Exception as e:
            log.warning("Device discovery failed, retrying in 10s: %s", e)
        await asyncio.sleep(10)

app = FastAPI(lifespan=lifespan)
state = PlayerState()


@app.middleware("http")
async def save_state_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.method in ("POST", "DELETE") and request.url.path.startswith("/api/"):
        _save_state()
    return response

av_transport: UpnpService | None = None
rendering_control: UpnpService | None = None
device: UpnpDevice | None = None
discovered_devices: dict[str, dict] = {}  # {friendly_name: {url, name, model}}
soundtouch: SoundTouch | None = None  # set while the connected device is a Bose SoundTouch

# ---------------------------------------------------------------------------
# DLNA Discovery & Control
# ---------------------------------------------------------------------------

async def discover_and_connect():
    """Use SSDP to find DLNA MediaRenderers on the network."""
    global av_transport, rendering_control, device

    from async_upnp_client.search import async_search

    found = {}
    async def on_response(headers: dict):
        location = headers.get("location", headers.get("LOCATION", ""))
        if location:
            found[location] = headers

    # Search for MediaRenderer devices
    await async_search(
        search_target="urn:schemas-upnp-org:device:MediaRenderer:1",
        timeout=5,
        async_callback=on_response,
    )
    for url in STATIC_DEVICES:  # renderers SSDP misses
        found.setdefault(url, {})

    if not found:
        log.debug("No DLNA renderers found on network")
        return

    factory = _upnp_factory()

    last_known = None
    for location in found:
        try:
            dev = await factory.async_create_device(location)
            avt = dev.service("urn:schemas-upnp-org:service:AVTransport:1")
            rc = dev.service("urn:schemas-upnp-org:service:RenderingControl:1")
            if avt and rc:
                discovered_devices[dev.friendly_name] = {
                    "url": location,
                    "name": dev.friendly_name,
                    "model": dev.model_name or "",
                }
                # Auto-connect to last known device by name or URL
                if state.last_device and dev.friendly_name == state.last_device:
                    last_known = (dev, avt, rc, location)
                elif state.last_device_url and location == state.last_device_url:
                    last_known = (dev, avt, rc, location)
        except Exception as e:
            log.debug("Device probe failed for %s: %s", location, e)

    # If name/URL match didn't work (device half-awake), try connecting by URL directly
    if not last_known and av_transport is None and state.last_device_url:
        # Match by IP, not exact URL (SSDP may return different paths)
        last_ip = state.last_device_url.split("//")[1].split("/")[0].split(":")[0]
        matching_url = next((loc for loc in found if last_ip in loc), None)
        if matching_url:
            try:
                log.info("Retrying direct connect to %s (matched via IP)", matching_url)
                await connect_to_device(matching_url)
                return
            except Exception as e:
                log.debug("Direct reconnect failed: %s", e)

    if last_known and av_transport is None:
        dev, avt, rc, location = last_known
        device = dev
        av_transport = avt
        rendering_control = rc
        await _detect_soundtouch(location)
        await _sync_device_volume(restore=True)
        log.info("Reconnected to last device: %s (%s)", dev.friendly_name, location)
    elif av_transport is None and discovered_devices:
        names = ", ".join(discovered_devices.keys())
        log.info("Found %d device(s): %s. Select one from the UI.", len(discovered_devices), names)


async def connect_to_device(description_url: str):
    """Connect to a specific device by its description URL."""
    global av_transport, rendering_control, device
    factory = _upnp_factory()
    device = await factory.async_create_device(description_url)
    av_transport = device.service("urn:schemas-upnp-org:service:AVTransport:1")
    rendering_control = device.service("urn:schemas-upnp-org:service:RenderingControl:1")
    await _detect_soundtouch(description_url)
    # Same device as last time (possibly rebooted, renamed to "unknown", or
    # found under a different description path) -> restore our saved volume.
    # A different device -> adopt whatever level it is currently at.
    await _sync_device_volume(restore=_is_last_device(device.friendly_name, description_url))
    state.last_device = device.friendly_name
    state.last_device_url = description_url
    _save_state()
    log.info("Connected to: %s", device.friendly_name)


def _upnp_factory() -> UpnpFactory:
    """Lenient UPnP parsing: the SoundTouch SA-4 also advertises a Tencent QPlay
    service whose SCPD has a non-standard root namespace, which the strict
    parser rejects ("Invalid document root"), losing the whole device."""
    return UpnpFactory(AiohttpRequester(), non_strict=True)


async def _detect_soundtouch(description_url: str):
    """Point the SoundTouch side-channel at the connected device, or clear it."""
    global soundtouch
    soundtouch = await SoundTouch.probe(_device_host(description_url))
    if soundtouch:
        log.info("Device is a Bose SoundTouch (%s): using its volume/wake API", soundtouch.host)


async def _register_static_devices():
    """List BV_STATIC_DEVICES at startup even when another device is already
    connected (SSDP discovery only runs while disconnected). List only:
    reconnecting is connect_loop's job (its fast reconnect + discovery include
    static devices); connecting here too raced it and connected twice."""
    if not STATIC_DEVICES:
        return
    factory = _upnp_factory()
    for url in STATIC_DEVICES:
        try:
            dev = await factory.async_create_device(url)
            if dev.service("urn:schemas-upnp-org:service:AVTransport:1") and \
               dev.service("urn:schemas-upnp-org:service:RenderingControl:1"):
                discovered_devices[dev.friendly_name] = {
                    "url": url, "name": dev.friendly_name, "model": dev.model_name or ""}
                log.info("Static device: %s (%s)", dev.friendly_name, url)
        except Exception as e:
            log.warning("Static device %s unreachable: %s", url, e)


def _device_host(url: str) -> str:
    """Host (IP) part of a description URL, or "" if unparseable."""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _is_last_device(friendly_name: str, description_url: str) -> bool:
    """True if this looks like the device we were connected to last time.
    Matches by friendly name, exact description URL, or host IP (the CY920
    renames itself to "unknown" after a power cycle and SSDP may return a
    different description path)."""
    if state.last_device and friendly_name == state.last_device:
        return True
    if state.last_device_url:
        if description_url == state.last_device_url:
            return True
        last_host = _device_host(state.last_device_url)
        if last_host and last_host == _device_host(description_url):
            return True
    return False


VOLUME_SYNC_GRACE = 5.0  # skip keepalive volume adoption this long after we set it
VOLUME_RESET_LEVEL = 100  # the CY920 resets its DLNA volume to this after a reboot
_volume_set_ts = 0.0  # time.monotonic() of the last SetVolume we issued
_volume_push_pending = False  # SetVolume failed; retry on next play/keepalive


async def _push_volume() -> bool:
    """Best-effort SetVolume(state.volume).  On failure, flags a retry for the
    next play (some renderers reject SetVolume before a URI is set)."""
    global _volume_push_pending, _volume_set_ts
    try:
        _volume_set_ts = time.monotonic()
        await dlna_set_volume(state.volume)
        _volume_push_pending = False
        return True
    except Exception as e:
        _volume_push_pending = True
        log.warning("Could not set volume to %d: %s (will retry on play)", state.volume, e)
        return False


async def _sync_device_volume(restore: bool):
    """Reconcile app and device volume right after connecting.

    restore=True  -> we are back on the last known device: push the saved
                     volume.  The CY920 comes up at 100 after a power cycle or
                     deep sleep, so the saved value is the truth, not the device.
    restore=False -> user picked a different device: adopt its current level."""
    global _volume_push_pending
    _volume_push_pending = False
    if soundtouch:
        # SoundTouch: the speaker's own volume is the truth. Its renderer reads 0
        # in standby, which is a reset, not a user choice.
        try:
            if not await soundtouch.is_standby():
                state.volume, _ = await soundtouch.volume()
        except Exception as e:
            log.debug("SoundTouch volume read failed: %s", e)
        return
    try:
        dev_vol = int(await dlna_get_volume())
    except Exception as e:
        log.debug("GetVolume not supported: %s", e)
        return
    if dev_vol == state.volume:
        return
    if restore:
        log.info("Device reports volume %d, restoring saved %d", dev_vol, state.volume)
        await _push_volume()
    else:
        log.info("Adopting device volume %d (was %d)", dev_vol, state.volume)
        state.volume = dev_vol


async def _poll_device_volume(recovering: bool = False):
    """Keepalive helper: keep the slider honest.

    A mismatch is treated as a device *reset* (re-push our saved volume) when
    the previous ping had failed (device likely rebooted faster than the
    3-strike disconnect) or the device reports VOLUME_RESET_LEVEL.  Any other
    mismatch is a change made outside the app (remote, other app) and is
    adopted.  Skipped shortly after our own SetVolume to avoid racing it."""
    if _volume_push_pending:
        await _push_volume()
        return
    if time.monotonic() - _volume_set_ts < VOLUME_SYNC_GRACE:
        return
    if soundtouch:
        await _poll_soundtouch_volume()
        return
    try:
        dev_vol = int(await dlna_get_volume())
    except Exception as e:
        log.debug("Keepalive GetVolume failed: %s", e)
        return
    # A SetVolume may have landed during the await above
    if time.monotonic() - _volume_set_ts < VOLUME_SYNC_GRACE:
        return
    if dev_vol == state.volume:
        return
    if recovering or dev_vol == VOLUME_RESET_LEVEL:
        log.info("Device volume reset to %d (saved %d), restoring", dev_vol, state.volume)
        await _push_volume()
    else:
        log.info("Volume changed on device: %d -> %d", state.volume, dev_vol)
        state.volume = dev_vol
        _save_state()


async def _poll_soundtouch_volume():
    """Keepalive for a SoundTouch: adopt changes made on the speaker or by Home
    Assistant (both move the speaker's own volume). Nothing in standby — the
    renderer's 0 there is a reset, and pushing would be pointless."""
    st = soundtouch
    try:
        if await st.is_standby():
            return
        dev_vol, _ = await st.volume()
    except Exception as e:
        log.debug("SoundTouch keepalive volume failed: %s", e)
        return
    if time.monotonic() - _volume_set_ts < VOLUME_SYNC_GRACE:
        return
    if dev_vol != state.volume:
        log.info("Volume changed on SoundTouch: %d -> %d", state.volume, dev_vol)
        state.volume = dev_vol
        _save_state()


async def _delayed_push_volume(delay: float):
    """SoundTouch: re-assert the renderer volume once the stream has started
    (it can still read 0 right after a wake from standby)."""
    await asyncio.sleep(delay)
    if soundtouch and _device_ready():
        await _push_volume()


def _require_avt() -> UpnpService:
    """Snapshot AVTransport; 503 if no device. Using the returned local
    reference avoids a race with keepalive nulling the global mid-call."""
    avt = av_transport
    if avt is None:
        raise HTTPException(status_code=503, detail="Device not connected")
    return avt


def _require_rc() -> UpnpService:
    rc = rendering_control
    if rc is None:
        raise HTTPException(status_code=503, detail="Device not connected")
    return rc


async def dlna_set_uri(track: Track):
    avt = _require_avt()
    stream_url = f"{BASE_URL}/stream/{track.id}"
    profile = _dlna_profile(track.content_type)
    size_attr = f' size="{track.content_length}"' if track.content_length else ''
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f'<dc:title>{_xml_escape(track.title)}</dc:title>'
        f'<dc:creator>{_xml_escape(track.artist)}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{track.content_type}:{profile}"{size_attr}>'
        f'{stream_url}</res>'
        '</item></DIDL-Lite>'
    )
    action = avt.action("SetAVTransportURI")
    await action.async_call(InstanceID=0, CurrentURI=stream_url, CurrentURIMetaData=didl)


async def dlna_play():
    await _require_avt().action("Play").async_call(InstanceID=0, Speed="1")


async def dlna_pause():
    await _require_avt().action("Pause").async_call(InstanceID=0)


async def dlna_stop():
    await _require_avt().action("Stop").async_call(InstanceID=0)


async def dlna_set_volume(level: int):
    await _require_rc().action("SetVolume").async_call(
        InstanceID=0, Channel="Master", DesiredVolume=level
    )


async def dlna_get_volume() -> int:
    r = await _require_rc().action("GetVolume").async_call(InstanceID=0, Channel="Master")
    return r.get("CurrentVolume", 0)


async def dlna_get_position() -> dict:
    r = await _require_avt().action("GetPositionInfo").async_call(InstanceID=0)
    return {
        "position": _parse_duration(r.get("RelTime", "0:00:00")),
        "duration": _parse_duration(r.get("TrackDuration", "0:00:00")),
        "track_uri": r.get("TrackURI") or "",
    }


async def dlna_get_transport_state() -> str:
    r = await _require_avt().action("GetTransportInfo").async_call(InstanceID=0)
    return r.get("CurrentTransportState", "UNKNOWN")


async def dlna_seek(position_secs: int):
    h = position_secs // 3600
    m = (position_secs % 3600) // 60
    s = position_secs % 60
    target = f"{h}:{m:02d}:{s:02d}"
    await _require_avt().action("Seek").async_call(
        InstanceID=0, Unit="REL_TIME", Target=target
    )


async def dlna_set_next_uri(track: Track):
    """Pre-load next track for gapless playback."""
    avt = _require_avt()
    stream_url = f"{BASE_URL}/stream/{track.id}"
    profile = _dlna_profile(track.content_type)
    size_attr = f' size="{track.content_length}"' if track.content_length else ''
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f'<dc:title>{_xml_escape(track.title)}</dc:title>'
        f'<dc:creator>{_xml_escape(track.artist)}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:{track.content_type}:{profile}"{size_attr}>'
        f'{stream_url}</res>'
        '</item></DIDL-Lite>'
    )
    await avt.action("SetNextAVTransportURI").async_call(
        InstanceID=0, NextURI=stream_url, NextURIMetaData=didl
    )


async def dlna_set_play_mode(mode: str):
    await _require_avt().action("SetPlayMode").async_call(
        InstanceID=0, NewPlayMode=mode
    )


def _parse_duration(s: str) -> int:
    if not s or s == "NOT_IMPLEMENTED":
        return 0
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
    return 0


def _dlna_profile(content_type: str) -> str:
    return {
        "audio/mpeg": "DLNA.ORG_PN=MP3",
        "audio/mp4": "DLNA.ORG_PN=AAC_ISO",
        "audio/flac": "*",
        "audio/wav": "*",
        "audio/x-wav": "*",
    }.get(content_type, "*")


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# yt-dlp Integration — download to local file
# ---------------------------------------------------------------------------

async def extract_playlist(url: str) -> list[dict]:
    """Extract metadata for all entries in a playlist/channel/single video."""
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--flat-playlist", "--dump-json", "--", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"yt-dlp error: {stderr.decode()[:300]}")

    entries = []
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        info = json.loads(line)
        video_url = info.get("url") or info.get("webpage_url") or info.get("original_url", "")
        # flat-playlist gives minimal ids; build full URL if needed
        if video_url and not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={video_url}"
        entries.append({
            "title": info.get("title", "Unknown"),
            "artist": info.get("uploader", info.get("channel", "Unknown")),
            "duration": int(info.get("duration") or 0),
            "url": video_url,
        })
    return entries


async def download_youtube(url: str, track_id: str) -> dict:
    """Download a single YouTube video's audio and convert to MP3."""
    output_template = str(CACHE_DIR / f"{track_id}.%(ext)s")
    mp3_path = CACHE_DIR / f"{track_id}.mp3"

    proc = await asyncio.create_subprocess_exec(
        "yt-dlp",
        "--ffmpeg-location", FFMPEG,
        "-f", "bestaudio",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "2",
        "--no-playlist",
        "--print-json",
        "-o", output_template,
        "--", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"yt-dlp error: {stderr.decode()[:300]}")

    info = json.loads(stdout)

    if not mp3_path.exists():
        matches = list(CACHE_DIR.glob(f"{track_id}*.mp3"))
        if matches:
            mp3_path = matches[0]
        else:
            raise HTTPException(status_code=500, detail="MP3 conversion failed")

    result = {
        "title": info.get("title", "Unknown"),
        "artist": info.get("uploader", info.get("channel", "Unknown")),
        "duration": int(info.get("duration", 0)),
        "local_path": str(mp3_path),
        "content_type": "audio/mpeg",
        "content_length": mp3_path.stat().st_size,
    }
    _enforce_cache_cap(keep={str(mp3_path)})
    return result


def _enforce_cache_cap(keep: set[str] = frozenset()):
    """Delete the oldest cached MP3s until the cache is under CACHE_MAX_MB.
    Never deletes a file referenced by the queue or listed in `keep` (the
    download that just finished isn't in the queue yet)."""
    if CACHE_MAX_MB <= 0:
        return
    limit = CACHE_MAX_MB * 1024 * 1024
    files = sorted(CACHE_DIR.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    if total <= limit:
        return
    protected = {t.local_path for t in state.queue if t.local_path} | set(keep)
    freed = 0
    for p in files:
        if total - freed <= limit:
            break
        if str(p) in protected:
            continue
        size = p.stat().st_size
        p.unlink(missing_ok=True)
        freed += size
    if freed:
        log.info("Cache cap %d MB: evicted %.0f MB of old downloads", CACHE_MAX_MB, freed / 1048576)


async def background_download(track: Track):
    """Download a track's audio in the background."""
    try:
        info = await download_youtube(track.source_url, track.id)
        track.local_path = info["local_path"]
        track.content_type = info["content_type"]
        track.content_length = info["content_length"]
        track.title = info["title"]  # update with full title
        track.artist = info["artist"]
        track.duration = info["duration"]
        # If this is the current track and nothing is playing, start it
        if _device_ready():
            idx = next((i for i, t in enumerate(state.queue) if t.id == track.id), -1)
            if idx == state.current_index:
                ts = await dlna_get_transport_state()
                if ts in ("STOPPED", "NO_MEDIA_PRESENT"):
                    await _play_current()
    except Exception as e:
        log.error("Background download failed for %s (%s): %s", track.id, track.source_url, e)


# ---------------------------------------------------------------------------
# Stream Endpoint (called by DLNA device)
# ---------------------------------------------------------------------------

@app.head("/stream/{track_id}")
@app.get("/stream/{track_id}")
async def stream_audio(track_id: str, request: Request):
    track = next((t for t in state.queue if t.id == track_id), None)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    if track.local_path and Path(track.local_path).exists():
        # Serve local file with Range support
        return FileResponse(
            track.local_path,
            media_type=track.content_type,
            headers={
                "Accept-Ranges": "bytes",
                "transferMode.dlna.org": "Streaming",
                "contentFeatures.dlna.org": "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000",
            },
        )
    elif track.direct_url:
        # Proxy stream for radio/url sources
        return await _proxy_stream(track, request)
    else:
        raise HTTPException(status_code=404, detail="No audio source")


async def _proxy_stream(track: Track, request: Request):
    url = track.direct_url or track.source_url
    headers = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    if request.method == "HEAD":
        return Response(status_code=200, headers={
            "Content-Type": track.content_type,
            "Accept-Ranges": "bytes",
            "transferMode.dlna.org": "Streaming",
        })

    upstream = await http_client.send(
        http_client.build_request("GET", url, headers=headers),
        stream=True,
    )

    response_headers = {
        "Content-Type": track.content_type,
        "Accept-Ranges": "bytes",
        "transferMode.dlna.org": "Streaming",
    }
    if "content-length" in upstream.headers:
        response_headers["Content-Length"] = upstream.headers["content-length"]
    if "content-range" in upstream.headers:
        response_headers["Content-Range"] = upstream.headers["content-range"]

    async def generate():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(generate(), status_code=upstream.status_code, headers=response_headers)


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

def _detect_type(url: str) -> str:
    lower = url.lower()
    if any(d in lower for d in ["youtube.com", "youtu.be", "music.youtube.com"]):
        return "youtube"
    if any(lower.endswith(ext) for ext in [".mp3", ".flac", ".aac", ".wav", ".m4a", ".ogg"]):
        return "url"
    return "radio"


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """Search YouTube via yt-dlp and return results."""
    query = f"ytsearch{req.max_results}:{req.query}"
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--flat-playlist", "--dump-json", "--", query,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=f"Search failed: {stderr.decode()[:200]}")

    results = []
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        info = json.loads(line)
        vid = info.get("id", "")
        results.append({
            "id": vid,
            "title": info.get("title", "Unknown"),
            "artist": info.get("uploader", info.get("channel", "")),
            "duration": int(info.get("duration") or 0),
            "url": f"https://www.youtube.com/watch?v={vid}" if vid else "",
            "thumbnail": info.get("thumbnail", ""),
        })
    return {"results": results}


# ---------------------------------------------------------------------------
# YouTube Music (ytmusic.py) — browse with ytmusicapi, play via the existing
# yt-dlp download path (music.youtube.com watch URLs).
# ---------------------------------------------------------------------------

ytm = YTM(YTM_AUTH_FILE)


async def _ytm_call(fn, *args):
    """ytmusicapi is blocking and breaks when YouTube changes: run it in a
    thread and turn failures into a clean 502 for the UI."""
    try:
        return await asyncio.to_thread(fn, *args)
    except HTTPException:
        raise
    except Exception as e:
        log.warning("YouTube Music call %s failed: %s", getattr(fn, "__name__", fn), e)
        raise HTTPException(status_code=502, detail=f"YouTube Music: {str(e)[:160]}")


@app.get("/api/ytm/status")
async def ytm_status():
    signed_in = await asyncio.to_thread(lambda: ytm.signed_in)
    return {"signed_in": signed_in, "auth_file": YTM_AUTH_FILE.exists()}


@app.get("/api/ytm/search")
async def ytm_search(q: str, kind: str = "songs", limit: int = 20):
    if not q.strip():
        return {"results": []}
    return {"results": await _ytm_call(ytm.search, q.strip(), kind, max(1, min(50, limit)))}


@app.get("/api/ytm/moods")
async def ytm_moods():
    return {"sections": await _ytm_call(ytm.moods)}


@app.get("/api/ytm/mood")
async def ytm_mood(params: str):
    return {"items": await _ytm_call(ytm.mood_items, params)}


@app.get("/api/ytm/home")
async def ytm_home():
    return {"shelves": await _ytm_call(ytm.home)}


@app.get("/api/ytm/library")
async def ytm_library():
    return {"items": await _ytm_call(ytm.library)}


@app.get("/api/ytm/tracks")
async def ytm_tracks(kind: str, id: str):
    """Preview what a playlist / album / song radio contains."""
    return {"tracks": await _ytm_resolve(kind, id)}


async def _ytm_resolve(kind: str, iid: str, title: str = "", artist: str = "",
                       duration: int = 0) -> list[dict]:
    if kind in ("song", "video"):
        return [{"videoId": iid, "title": title or "Loading...", "artist": artist, "duration": duration}]
    if kind == "playlist":
        return await _ytm_call(ytm.playlist_tracks, iid)
    if kind == "album":
        return await _ytm_call(ytm.album_tracks, iid)
    if kind == "radio":
        return await _ytm_call(ytm.radio, iid)
    raise HTTPException(status_code=400, detail=f"Unknown kind {kind!r}")


@app.post("/api/ytm/enqueue")
async def ytm_enqueue(req: YtmEnqueueRequest):
    """Queue YouTube Music tracks: mode 'end' appends, 'next' plays after the
    current track, 'now' interrupts and plays the first one as soon as it is
    downloaded. Already-queued tracks are skipped (a single one is jumped to
    for 'now')."""
    mode = req.mode if req.mode in ("now", "next", "end") else "end"
    found = await _ytm_resolve(req.kind, req.id, req.title, req.artist, req.duration)
    tracks, skipped = [], 0
    for t in found:
        url = f"https://music.youtube.com/watch?v={t['videoId']}"
        if _is_duplicate(url):
            skipped += 1
            continue
        tracks.append(Track(id=str(uuid.uuid4())[:8], title=t["title"], artist=t["artist"],
                            source_type="youtube", source_url=url, duration=t["duration"]))
    if not tracks:
        if mode == "now" and req.kind in ("song", "video"):
            vid = req.id
            idx = next((i for i, t in enumerate(state.queue)
                        if _normalize_yt_url(t.source_url) == vid and t.local_path), None)
            if idx is not None and _device_ready():
                state.current_index = idx
                await _play_current()
                return {"ok": True, "count": 0, "skipped": skipped, "jumped": True}
        return {"ok": True, "count": 0, "skipped": skipped}

    was_empty = state.current_index == -1
    pos = len(state.queue) if (mode == "end" or was_empty) else state.current_index + 1
    state.queue[pos:pos] = tracks
    play_first = was_empty or mode == "now"
    if play_first:
        state.current_index = pos
    if mode == "now" and not was_empty and _device_ready():
        # Stop the old track now (marked as a user stop, so auto-advance
        # ignores it). Keep this even though `force` starts the new track:
        # an old track left playing could end or gapless-advance during the
        # download, move current_index off the new track, and make the
        # force check skip it ("queue moved").
        _mark_user_stop()
        try:
            await dlna_stop()
        except Exception as e:
            log.debug("Stop before play-now failed: %s", e)
    # 'now' takes the renderer even if something else is playing by the time
    # the download lands (Volumio restarts its own queue after a UPnP Stop).
    asyncio.create_task(_download_playlist_tracks(tracks, play_first=play_first,
                                                  force=mode == "now"))
    log.info("YouTube Music: queued %d track(s) [%s %s] mode=%s", len(tracks), req.kind, req.id, mode)
    return {"ok": True, "count": len(tracks), "skipped": skipped,
            "tracks": [_track_dict(t) for t in tracks[:10]]}


# ---------------------------------------------------------------------------
# Garage controller (static/garage.html): the Bose SoundTouch in the garage.
# Radio presets are sent as their RAW station URL (not via /stream), so Home
# Assistant's preset sensor and the Morpheus bridge see the same URL as when
# they start a preset themselves.
# ---------------------------------------------------------------------------

def _garage_cfg() -> dict:
    try:
        return json.loads(GARAGE_PRESETS_FILE.read_text())
    except Exception as e:
        log.warning("garage_presets.json unreadable: %s", e)
        return {"device": "Bose_out", "presets": []}


def _dial_stations(presets: list) -> list:
    """The presets as stations on the garage page's FM dial. "freq" (MHz) and
    "ps" (8-char station name, like FM RDS) are optional in garage_presets.json:
    a missing or out-of-band freq is spread evenly across 87.5-108."""
    out = []
    for i, p in enumerate(presets):
        f = p.get("freq")
        if not isinstance(f, (int, float)) or isinstance(f, bool) or not 87.5 <= f <= 108:
            f = round(88.5 + (i + 0.5) * 19 / len(presets), 1)
        ps = str(p.get("ps") or p["name"].upper())[:8]
        out.append({"n": i + 1, "name": p["name"], "freq": float(f), "ps": ps})
    return out


async def _ensure_garage_device() -> str:
    """Make the garage speaker the active renderer (bulb_voice drives one
    device at a time). Returns its name; 503 if it is not known/reachable."""
    name = _garage_cfg().get("device", "Bose_out")
    if device is not None and device.friendly_name == name and _device_ready():
        return name
    if name not in discovered_devices:
        await _register_static_devices()
    if name not in discovered_devices:
        raise HTTPException(status_code=503, detail=f"{name} not found")
    global av_transport, rendering_control
    av_transport = None
    rendering_control = None
    await connect_to_device(discovered_devices[name]["url"])
    return name


async def dlna_set_raw_uri(url: str, title: str, content_type: str):
    """SetAVTransportURI with an external URL (radio station), not /stream."""
    avt = _require_avt()
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="1" parentID="0" restricted="1">'
        f'<dc:title>{_xml_escape(title)}</dc:title>'
        '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
        f'<res protocolInfo="http-get:*:{content_type}:*">{_xml_escape(url)}</res>'
        '</item></DIDL-Lite>'
    )
    await avt.action("SetAVTransportURI").async_call(
        InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl)


@app.get("/api/garage/state")
async def garage_state():
    cfg = _garage_cfg()
    presets = cfg.get("presets", [])
    connected = device is not None and device.friendly_name == cfg.get("device") and _device_ready()
    out = {"device": cfg.get("device"), "connected": connected,
           "active_device": device.friendly_name if device else None,
           "presets": [p["name"] for p in presets], "stations": _dial_stations(presets),
           "standby": None, "source": None,
           "volume": state.volume, "preset": None, "transport": None, "track": None}
    if not connected:
        return out
    try:
        if soundtouch:
            np = await soundtouch.now_playing()
            out["source"], out["standby"] = np["source"], np["source"] == "STANDBY"
            if not out["standby"]:
                out["volume"], _ = await soundtouch.volume()
        out["transport"] = await dlna_get_transport_state()
        uri = (await dlna_get_position()).get("track_uri", "")
    except Exception as e:
        log.debug("garage state poll failed: %s", e)
        return out
    out["preset"] = next((i + 1 for i, p in enumerate(presets) if p["url"] == uri), None)
    tid = _track_id_from_stream_uri(uri) if uri.startswith(f"{BASE_URL}/stream/") else None
    if tid:
        t = next((t for t in state.queue if t.id == tid), None)
        if t:
            out["track"] = _track_dict(t)
    return out


@app.post("/api/garage/connect")
async def garage_connect():
    return {"ok": True, "device": await _ensure_garage_device()}


@app.post("/api/garage/preset/{n}")
async def garage_preset(n: int):
    presets = _garage_cfg().get("presets", [])
    if not 1 <= n <= len(presets):
        raise HTTPException(status_code=404, detail=f"No preset {n}")
    p = presets[n - 1]
    await _ensure_garage_device()
    if soundtouch:
        try:
            await soundtouch.prepare(state.volume)
        except Exception as e:
            log.warning("SoundTouch prepare failed: %s", e)
    _mark_user_stop()  # leaving our queue on purpose: no auto-advance
    await dlna_set_raw_uri(p["url"], p["name"], p.get("type", "audio/mpeg"))
    await dlna_play()
    await _push_volume()  # SoundTouch renderer volume is 0 after standby
    if soundtouch:
        asyncio.create_task(_delayed_push_volume(3.0))
    log.info("Garage preset %d: %s", n, p["name"])
    return {"ok": True, "preset": n, "name": p["name"]}


@app.post("/api/garage/power")
async def garage_power():
    """Music off: SoundTouch to standby (a plain DLNA stop otherwise)."""
    _mark_user_stop()
    if soundtouch:
        await soundtouch.standby()
    elif _device_ready():
        await dlna_stop()
    return {"ok": True}


@app.get("/api/devices")
async def api_devices():
    """List discovered DLNA renderers."""
    current_name = device.friendly_name if device else None
    return {
        "devices": list(discovered_devices.values()),
        "current": current_name,
        "garage": _garage_cfg().get("device"),  # this speaker opens /garage in the main app
    }


@app.post("/api/devices/select")
async def api_device_select(req: DeviceSelectRequest):
    """Switch to a different DLNA device by name."""
    name = req.name
    if name not in discovered_devices:
        raise HTTPException(status_code=404, detail="Device not found")
    global av_transport, rendering_control
    av_transport = None
    rendering_control = None
    await connect_to_device(discovered_devices[name]["url"])
    return {"ok": True, "device": name}


@app.post("/api/devices/refresh")
async def api_devices_refresh():
    """Re-scan the network for DLNA devices."""
    global av_transport, rendering_control
    av_transport = None
    rendering_control = None
    discovered_devices.clear()
    await discover_and_connect()
    return {
        "devices": list(discovered_devices.values()),
        "current": device.friendly_name if device else None,
    }


def _is_playlist_url(url: str) -> bool:
    lower = url.lower()
    return "list=" in lower or "/playlist" in lower or "/@" in lower or "/channel/" in lower


def _normalize_yt_url(url: str) -> str:
    """Extract video ID to compare YouTube URLs regardless of format."""
    import re
    m = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
    return m.group(1) if m else url


def _is_duplicate(url: str) -> bool:
    """Check if a URL is already in the queue."""
    existing = {t.source_url for t in state.queue}
    if url in existing:
        return True
    # Also compare by YouTube video ID (handles youtu.be vs youtube.com)
    norm = _normalize_yt_url(url)
    return any(_normalize_yt_url(t.source_url) == norm for t in state.queue)


@app.post("/api/queue/add")
async def queue_add(req: AddRequest):
    source_type = req.type if req.type != "auto" else _detect_type(req.url)

    if source_type == "youtube" and _is_playlist_url(req.url):
        entries = await extract_playlist(req.url)
        if not entries:
            raise HTTPException(status_code=400, detail="No entries found in playlist")

        tracks = []
        skipped = 0
        for entry in entries:
            if not entry["url"]:
                continue
            if _is_duplicate(entry["url"]):
                skipped += 1
                continue
            track_id = str(uuid.uuid4())[:8]
            track = Track(
                id=track_id,
                title=entry["title"],
                artist=entry["artist"],
                source_type="youtube",
                source_url=entry["url"],
                duration=entry["duration"],
            )
            state.queue.append(track)
            tracks.append(track)

        if not tracks and skipped:
            return {"ok": True, "count": 0, "skipped": skipped, "tracks": []}

        was_empty = state.current_index == -1
        if was_empty and tracks:
            state.current_index = len(state.queue) - len(tracks)

        asyncio.create_task(_download_playlist_tracks(tracks, play_first=was_empty))

        return {
            "ok": True,
            "count": len(tracks),
            "skipped": skipped,
            "tracks": [_track_dict(t) for t in tracks[:10]],
        }

    # Single track duplicate check
    if _is_duplicate(req.url):
        return {"ok": True, "duplicate": True, "detail": "Already in queue"}

    if source_type == "youtube":
        track_id = str(uuid.uuid4())[:8]
        # Claim the progress card only if no playlist batch is using it
        show_card = state.download.total == 0
        if show_card:
            state.download = DownloadProgress(
                total=1, done=0, current_title="Downloading...",
                track_ids=[track_id], started_at=time.time(),
            )
        try:
            info = await download_youtube(req.url, track_id)
        finally:
            # Clear only if the card is still ours (a batch may have claimed it
            # meanwhile); also clears on download failure instead of sticking.
            if show_card and state.download.track_ids == [track_id]:
                state.download = DownloadProgress()
        track = Track(
            id=track_id,
            title=info["title"],
            artist=info["artist"],
            source_type="youtube",
            source_url=req.url,
            duration=info["duration"],
            local_path=info["local_path"],
            content_type=info["content_type"],
            content_length=info["content_length"],
        )
    else:
        track_id = str(uuid.uuid4())[:8]
        track = Track(
            id=track_id,
            title=req.url.split("/")[-1] or req.url,
            artist="",
            source_type=source_type,
            source_url=req.url,
            direct_url=req.url,
        )

    state.queue.append(track)

    if state.current_index == -1:
        state.current_index = len(state.queue) - 1
        try:
            await _play_current()
        except Exception as e:
            log.warning("Auto-play failed for track %s: %s", track.id, e)

    return {"ok": True, "track": _track_dict(track)}


_batch_download_lock = asyncio.Lock()


async def _download_playlist_tracks(tracks: list[Track], play_first: bool = False,
                                    force: bool = False):
    """Download playlist tracks sequentially in the background.  Batches are
    serialized via a lock so concurrent playlist adds don't compete for
    bandwidth or clobber each other's progress card."""
    async with _batch_download_lock:
        await _run_playlist_batch(tracks, play_first, force)


async def _start_first_download(track: Track, batch_ids: set[str], force: bool):
    """Start playback when a batch's first track has downloaded. Normally only
    on an idle renderer; force ('play now') takes it over whatever it plays,
    as long as the queue still points at this batch (the user may have picked
    something else during the download). Every skip is logged."""
    if force:
        cur = (state.queue[state.current_index]
               if 0 <= state.current_index < len(state.queue) else None)
        if cur is None or cur.id not in batch_ids or not cur.local_path:
            log.info("Not starting %s: the queue moved to another track during the download",
                     track.title)
            return
        await _play_current()
        return
    ts = await dlna_get_transport_state()
    if ts not in ("STOPPED", "NO_MEDIA_PRESENT"):
        log.info("Not auto-starting %s: renderer is %s", track.title, ts)
        return
    idx = next((j for j, t in enumerate(state.queue) if t.id == track.id), None)
    if idx is not None:
        state.current_index = idx
        await _play_current()


async def _run_playlist_batch(tracks: list[Track], play_first: bool, force: bool = False):
    # Tracks may have been removed from the queue while waiting for the lock
    queued_ids = {t.id for t in state.queue}
    tracks = [t for t in tracks if t.id in queued_ids]
    if not tracks:
        return
    state.download = DownloadProgress(
        total=len(tracks), done=0, current_title="",
        track_ids=[t.id for t in tracks], started_at=time.time(),
    )
    started = not play_first
    for i, track in enumerate(tracks):
        if not any(t.id == track.id for t in state.queue):
            state.download.done = i + 1
            continue  # removed from queue mid-batch; skip download
        state.download.current_title = track.title
        try:
            info = await download_youtube(track.source_url, track.id)
            track.local_path = info["local_path"]
            track.content_type = info["content_type"]
            track.content_length = info["content_length"]
            track.title = info["title"]
            track.artist = info["artist"]
            track.duration = info["duration"]

            # Start playback on the first track that actually downloads
            # (earlier ones may have failed and been dropped).
            if not started and _device_ready():
                started = True
                try:
                    await _start_first_download(track, {t.id for t in tracks}, force)
                except Exception as e:
                    log.warning("Auto-play first playlist track failed: %s", e)

            state.download.done = i + 1
            log.info("Downloaded [%d/%d]: %s", i + 1, len(tracks), track.title)
            _save_state()
        except Exception as e:
            state.download.done = i + 1
            state.download.failed += 1
            log.error("Failed to download %s: %s", track.source_url, e)
            # Drop the failed track so it doesn't sit in the queue as
            # "downloading" forever, and clean up any partial files.
            f_idx = next((j for j, t in enumerate(state.queue) if t.id == track.id), None)
            if f_idx is not None:
                _drop_track_at(f_idx)
            for p in CACHE_DIR.glob(f"{track.id}*"):
                p.unlink(missing_ok=True)
            _save_state()
    state.download = DownloadProgress()  # clear when done


def _drop_track_at(idx: int) -> Track:
    """Remove queue[idx], delete its cached file, and adjust current_index."""
    track = state.queue.pop(idx)
    if track.local_path and Path(track.local_path).exists():
        Path(track.local_path).unlink(missing_ok=True)
    if idx < state.current_index:
        state.current_index -= 1
    elif idx == state.current_index and state.current_index >= len(state.queue):
        state.current_index = -1
    return track


@app.delete("/api/queue/{track_id}")
async def queue_remove(track_id: str):
    idx = next((i for i, t in enumerate(state.queue) if t.id == track_id), None)
    if idx is None:
        raise HTTPException(status_code=404)
    was_current = idx == state.current_index
    _drop_track_at(idx)
    if was_current:
        # Stop playback best-effort: removal must succeed even if the
        # device is offline or the stop fails.
        _mark_user_stop()
        try:
            await dlna_stop()
        except Exception as e:
            log.warning("Stop after removing playing track failed: %s", e)
    return {"ok": True}


@app.get("/api/queue")
async def queue_list():
    return [_track_dict(t) for t in state.queue]


@app.post("/api/play")
async def api_play(req: PlayRequest = PlayRequest()):
    if req.track_id:
        idx = next((i for i, t in enumerate(state.queue) if t.id == req.track_id), None)
        if idx is None:
            raise HTTPException(status_code=404)
        state.current_index = idx
        await _play_current()
    else:
        transport = await dlna_get_transport_state()
        if transport in ("PAUSED_PLAYBACK", "PAUSED"):
            await dlna_play()
        elif state.current_index >= 0:
            await _play_current()
    return {"ok": True}


@app.post("/api/pause")
async def api_pause():
    await dlna_pause()
    return {"ok": True}


@app.post("/api/stop")
async def api_stop():
    _mark_user_stop()
    await dlna_stop()
    return {"ok": True}


@app.post("/api/next")
async def api_next():
    next_idx = _get_next_index(manual=True)
    if next_idx is None:
        return {"ok": False, "reason": "end of queue"}
    state.current_index = next_idx
    await _play_current()
    return {"ok": True}


@app.post("/api/prev")
async def api_prev():
    prev_idx = _get_prev_index()
    if prev_idx is None:
        return {"ok": False, "reason": "start of queue"}
    state.current_index = prev_idx
    await _play_current()
    return {"ok": True}


@app.post("/api/volume")
async def api_volume(req: VolumeRequest):
    global _volume_set_ts, _volume_push_pending
    level = max(0, min(100, req.level))
    _volume_set_ts = time.monotonic()
    if soundtouch:  # the speaker's own volume; the renderer follows while playing
        try:
            await soundtouch.set_volume(level)
        except Exception as e:
            log.warning("SoundTouch set_volume failed: %s", e)
    await dlna_set_volume(level)
    state.volume = level
    _volume_push_pending = False
    return {"ok": True}


@app.post("/api/seek")
async def api_seek(req: SeekRequest):
    if not _device_ready():
        raise HTTPException(status_code=503, detail="Device not connected")
    await dlna_seek(max(0, req.position))
    _note_seek(max(0, req.position))
    return {"ok": True}


@app.post("/api/play-mode")
async def api_play_mode(req: PlayModeRequest):
    mode = req.mode.upper()
    if mode not in ("NORMAL", "REPEAT_ALL", "REPEAT_ONE", "SHUFFLE"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    state.play_mode = mode
    if _device_ready():
        try:
            await dlna_set_play_mode(mode)
        except Exception as e:
            log.warning("SetPlayMode failed (device may not support it): %s", e)
    return {"ok": True, "mode": mode}


@app.get("/api/status")
async def api_status():
    transport_state = "STOPPED"
    position = {"position": 0, "duration": 0}

    try:
        transport_state = await dlna_get_transport_state()
        position = await dlna_get_position()
    except Exception as e:
        log.debug("Status poll failed: %s", e)
    # Volume is cached: refreshed on connect, after volume API calls, and by
    # the keepalive loop (_poll_device_volume) -- not on every 2s status poll

    current = None
    if 0 <= state.current_index < len(state.queue):
        current = _track_dict(state.queue[state.current_index])

    dl = state.download
    download = None
    if dl.total > 0:
        dl_tracks = []
        for tid in dl.track_ids:
            t = next((t for t in state.queue if t.id == tid), None)
            if t:
                dl_tracks.append({
                    "id": t.id,
                    "title": t.title,
                    "ready": bool(t.local_path),
                    "downloading": t.id == next(
                        (tr.id for tr in state.queue
                         if tr.id in dl.track_ids and not tr.local_path),
                        None
                    ),
                })
        elapsed = time.time() - dl.started_at if dl.started_at else 0
        download = {
            "total": dl.total, "done": dl.done, "failed": dl.failed,
            "current": dl.current_title, "tracks": dl_tracks,
            "elapsed": round(elapsed),
        }

    return {
        "current_track": current,
        "current_index": state.current_index,
        "transport_state": transport_state,
        "position": position["position"],
        "duration": position["duration"],
        "volume": state.volume,
        "device_connected": _device_ready(),
        "device_name": device.friendly_name if device else None,
        "play_mode": state.play_mode,
        "download": download,
        "queue": [_track_dict(t) for t in state.queue],
    }


@app.get("/api/logs")
async def api_logs():
    return {"logs": list(_log_buffer)}


# ---------------------------------------------------------------------------
# Auto-advance
# ---------------------------------------------------------------------------

_last_transport_state = "STOPPED"
_user_stop_ts = 0.0  # time.monotonic() of last user-initiated stop
_uri_change_ts = 0.0  # time.monotonic() of last SetAVTransportURI we issued
USER_STOP_WINDOW = 10.0  # suppress auto-advance this long after a user stop
URI_SYNC_GRACE = 5.0  # skip gapless index sync this long after we set a URI
_playing_foreign = False  # renderer's last PLAYING URI was not one of ours
# Renderer position/duration (s) at the last PLAYING poll; 0 = unknown.
_last_position = 0
_last_duration = 0
_last_position_at = 0.0  # time.monotonic() of that poll
END_OF_TRACK_SLACK = 15  # a stop this close to the end is the track ending


def _is_foreign_uri(uri: str) -> bool:
    """True if the renderer is playing something bulb_voice did not send
    (Home Assistant radio/announcement, another app). Unknown/empty URIs count
    as ours, so renderers that don't report TrackURI behave as before."""
    return bool(uri) and not uri.startswith(f"{BASE_URL}/stream/")


async def _note_playback_owner():
    """On a PLAYING poll, record who owns the renderer and how far into the
    track it is; auto-advance uses both when the renderer stops."""
    global _playing_foreign, _last_position, _last_duration, _last_position_at
    try:
        info = await dlna_get_position()
    except Exception as e:
        log.debug("TrackURI read failed: %s", e)
        _last_position = _last_duration = 0  # unknown: never block an advance on stale data
        return
    _playing_foreign = _is_foreign_uri(info.get("track_uri", ""))
    _last_position = info.get("position", 0)
    _last_duration = info.get("duration", 0)
    _last_position_at = time.monotonic()


def _note_seek(position: int):
    """A seek near the end can finish the track before the next poll; without
    this the stop would be judged against the pre-seek position."""
    global _last_position, _last_position_at
    if _last_position > 0:  # only where the renderer reports positions
        _last_position, _last_position_at = position, time.monotonic()


def _stopped_mid_track() -> bool:
    """True if the renderer stopped well before the end of our track: another
    controller replaced it (Volumio UI/app, HA) or someone pressed stop on the
    device. Not the track ending, so auto-advance must not take the renderer
    back. The renderer's duration and our own (from yt-dlp) must both put the
    stop before the end: a renderer that misjudges a VBR MP3's length still
    advances. Unknown position or duration counts as the end (old behaviour)."""
    if _last_position <= 0:
        return False
    played = _last_position + (time.monotonic() - _last_position_at)
    ours = (state.queue[state.current_index].duration or 0
            if 0 <= state.current_index < len(state.queue) else 0)
    known = [d for d in (_last_duration, ours) if d > 0]
    return bool(known) and all(d - played > END_OF_TRACK_SLACK for d in known)


def _mark_user_stop():
    """Record a user-initiated stop so auto-advance doesn't mistake the
    resulting PLAYING -> STOPPED transition for end-of-track."""
    global _user_stop_ts
    _user_stop_ts = time.monotonic()


def _track_id_from_stream_uri(uri: str) -> str | None:
    """Extract the track id from a /stream/{id} URL reported by the device."""
    marker = "/stream/"
    if not uri or marker not in uri:
        return None
    tail = uri.rsplit(marker, 1)[1].strip("/")
    # Strip any query string a device might append
    tail = tail.split("?", 1)[0]
    return tail or None


async def _sync_index_with_device():
    """Detect gapless auto-advance: with SetNextAVTransportURI the device
    switches tracks by itself while transport stays PLAYING, so current_index
    must follow the URI the device is actually playing."""
    if time.monotonic() - _uri_change_ts < URI_SYNC_GRACE:
        return  # we just set a URI ourselves; device may still report the old one
    info = await dlna_get_position()
    track_id = _track_id_from_stream_uri(info.get("track_uri", ""))
    if track_id is None:
        return
    idx = next((i for i, t in enumerate(state.queue) if t.id == track_id), None)
    if idx is None or idx == state.current_index:
        return
    state.current_index = idx
    log.info("Device advanced via gapless to track %d (%s)", idx, state.queue[idx].title)
    await _preload_next()
    _save_state()


async def auto_advance_loop():
    global _last_transport_state
    while True:
        await asyncio.sleep(3)
        if not _device_ready():
            continue
        try:
            ts = await dlna_get_transport_state()
            if ts == "PLAYING":
                await _note_playback_owner()
                try:
                    await _sync_index_with_device()
                except Exception as e:
                    log.debug("Gapless index sync failed: %s", e)
            if _last_transport_state == "PLAYING" and ts in ("STOPPED", "NO_MEDIA_PRESENT"):
                if time.monotonic() - _user_stop_ts < USER_STOP_WINDOW:
                    log.debug("Ignoring STOPPED transition after user stop")
                    _last_transport_state = ts
                    continue
                # Someone else's stream ended (e.g. a Home Assistant radio
                # preset or announcement on the same renderer): not our queue.
                if _playing_foreign:
                    log.info("Renderer stopped after a stream we didn't send; not advancing")
                    _last_transport_state = ts
                    continue
                # A SoundTouch put into standby (HA "music off", power key) is a
                # user stop; advancing would wake the speaker straight back up.
                if soundtouch:
                    try:
                        in_standby = await soundtouch.is_standby()
                    except Exception:
                        in_standby = False
                    if in_standby:
                        log.info("SoundTouch went to standby; treating as a user stop")
                        _last_transport_state = ts
                        continue
                # Our track stopped well before its end: another controller
                # (Volumio UI/app, HA) took the renderer, or someone stopped
                # it on the device. Advancing would play over what they started.
                if _stopped_mid_track():
                    log.info("Renderer stopped mid-track (last seen at %ds); not advancing",
                             _last_position)
                    _last_transport_state = ts
                    continue
                next_idx = _get_next_index()
                if next_idx is not None:
                    state.current_index = next_idx
                    log.info("Auto-advancing to track %d (%s)", state.current_index, state.play_mode)
                    try:
                        await _play_current()
                    except Exception as e:
                        log.error("Auto-advance play failed: %s. Waiting for device...", e)
                        # Wait for TRANSITIONING to clear before retry
                        for _ in range(5):
                            await asyncio.sleep(2)
                            try:
                                ts2 = await dlna_get_transport_state()
                                if ts2 != "TRANSITIONING":
                                    break
                            except Exception:
                                pass
                        try:
                            await _play_current()
                        except Exception as e2:
                            log.error("Auto-advance retry failed: %s", e2)
                    _save_state()
            _last_transport_state = ts
        except Exception as e:
            log.debug("Auto-advance poll error: %s", e)


async def keepalive_loop():
    """Ping the connected device every 15s to prevent it from sleeping and
    re-check its volume (see _poll_device_volume).
    Retries up to 3 times before marking disconnected.  When disconnected,
    nudges the device with a raw HTTP GET to its description URL so it stays
    on Wi-Fi long enough for SSDP rediscovery."""
    global av_transport, rendering_control
    fail_count = 0
    max_failures = 3
    while True:
        await asyncio.sleep(15)
        if _device_ready():
            try:
                await dlna_get_transport_state()
                recovering = fail_count > 0
                fail_count = 0
                await _poll_device_volume(recovering=recovering)
            except Exception as e:
                fail_count += 1
                log.warning("Device keepalive failed (%d/%d): %s",
                            fail_count, max_failures, e)
                if fail_count >= max_failures:
                    log.warning("Device unresponsive after %d pings, "
                                "marking disconnected", max_failures)
                    av_transport = None
                    rendering_control = None
                    fail_count = 0
        elif state.last_device_url:
            # Device disconnected — nudge it with a lightweight HTTP request
            # to prevent it from going into deep sleep before SSDP finds it.
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.get(state.last_device_url)
            except Exception:
                pass  # best-effort; device may already be asleep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _device_ready():
    return av_transport is not None and rendering_control is not None


async def _play_current():
    global _uri_change_ts
    if state.current_index < 0 or state.current_index >= len(state.queue):
        return
    if not _device_ready():
        raise HTTPException(status_code=503, detail="Device not connected")
    track = state.queue[state.current_index]
    log.info("Playing track %d: %s", state.current_index, track.title)
    if soundtouch:
        # Wake from standby, unmute, set its own volume — or the stream is silent.
        try:
            await soundtouch.prepare(state.volume)
        except Exception as e:
            log.warning("SoundTouch prepare failed: %s", e)
    _uri_change_ts = time.monotonic()
    await dlna_set_uri(track)
    await dlna_play()
    if _volume_push_pending or soundtouch:
        await _push_volume()  # SoundTouch renderer volume resets to 0 in standby
    if soundtouch:
        asyncio.create_task(_delayed_push_volume(3.0))
    # Pre-load next track for gapless playback
    await _preload_next()


async def _preload_next():
    """Set the next track URI for gapless playback."""
    if not _device_ready():
        return
    next_idx = _get_next_index()
    if next_idx is None:
        return
    next_track = state.queue[next_idx]
    if not next_track.local_path and next_track.source_type == "youtube":
        return  # Not downloaded yet
    try:
        await dlna_set_next_uri(next_track)
        log.debug("Pre-loaded next track: %s", next_track.title)
    except Exception as e:
        log.debug("SetNextAVTransportURI not supported or failed: %s", e)


def _get_next_index(manual: bool = False) -> int | None:
    """Get the next track index based on play mode.

    manual=True is a user-initiated skip: REPEAT_ONE then advances to the
    next track (repeat-one only governs automatic end-of-track repeats)."""
    if not state.queue:
        return None
    mode = state.play_mode
    if mode == "REPEAT_ONE":
        if not manual:
            return state.current_index
        mode = "NORMAL"
    if mode == "SHUFFLE":
        import random
        candidates = [i for i in range(len(state.queue)) if i != state.current_index]
        return random.choice(candidates) if candidates else None
    # NORMAL or REPEAT_ALL
    next_idx = state.current_index + 1
    if next_idx >= len(state.queue):
        if mode == "REPEAT_ALL":
            return 0
        return None
    return next_idx


def _get_prev_index() -> int | None:
    """Previous track index: sequential (SHUFFLE has no history, so it also
    steps back sequentially); REPEAT_ALL wraps from first to last."""
    if not state.queue or state.current_index < 0:
        return None
    prev_idx = state.current_index - 1
    if prev_idx < 0:
        if state.play_mode == "REPEAT_ALL":
            return len(state.queue) - 1
        return None
    return prev_idx


def _track_dict(t: Track) -> dict:
    ready = bool(t.local_path or t.direct_url or t.source_type != "youtube")
    return {
        "id": t.id,
        "title": t.title,
        "artist": t.artist,
        "source_type": t.source_type,
        "duration": t.duration,
        "ready": ready,
    }


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR))


@app.get("/garage")
async def garage_page():
    return FileResponse(STATIC_DIR / "garage.html")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
