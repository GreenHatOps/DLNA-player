"""Tests for the garage SoundTouch support: SoundTouch side-channel, volume
truth, playback-owner guards, static devices, cache cap. No network."""
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app
import soundtouch as st_mod

SoundTouch = st_mod.SoundTouch

NOW_STANDBY = '<?xml version="1.0"?><nowPlaying deviceID="X" source="STANDBY"><ContentItem source="STANDBY"/></nowPlaying>'
NOW_UPNP = ('<?xml version="1.0"?><nowPlaying deviceID="X" source="UPNP"><ContentItem source="UPNP"/>'
            '<playStatus>PLAY_STATE</playStatus></nowPlaying>')
VOL = '<volume deviceID="X"><targetvolume>{v}</targetvolume><actualvolume>{v}</actualvolume><muteenabled>{m}</muteenabled></volume>'


def fake_st(standby=False, volume=25, muted=False):
    st = SoundTouch("192.168.1.37")
    st.is_standby = AsyncMock(return_value=standby)
    st.volume = AsyncMock(return_value=(volume, muted))
    st.set_volume = AsyncMock()
    st.prepare = AsyncMock()
    return st


# ---------------------------------------------------------------------------
# soundtouch.py parsing and wake logic
# ---------------------------------------------------------------------------

class TestSoundTouchApi:
    @pytest.mark.asyncio
    async def test_probe_detects_soundtouch(self):
        with patch.object(SoundTouch, "_get", new_callable=AsyncMock,
                          return_value='<info deviceID="000C8AAC574C"><name>Bose_out</name></info>'):
            assert isinstance(await SoundTouch.probe("192.168.1.37"), SoundTouch)

    @pytest.mark.asyncio
    async def test_probe_rejects_other_devices_and_errors(self):
        with patch.object(SoundTouch, "_get", new_callable=AsyncMock, return_value="<html>nope</html>"):
            assert await SoundTouch.probe("192.168.1.116") is None
        with patch.object(SoundTouch, "_get", new_callable=AsyncMock, side_effect=OSError("refused")):
            assert await SoundTouch.probe("192.168.1.116") is None
        assert await SoundTouch.probe("") is None

    @pytest.mark.asyncio
    async def test_now_playing_and_volume_parsing(self):
        st = SoundTouch("h")
        with patch.object(st, "_get", new_callable=AsyncMock, return_value=NOW_UPNP):
            assert await st.now_playing() == {"source": "UPNP", "play_status": "PLAY_STATE"}
        with patch.object(st, "_get", new_callable=AsyncMock, return_value=NOW_STANDBY):
            assert await st.is_standby() is True
        with patch.object(st, "_get", new_callable=AsyncMock, return_value=VOL.format(v=36, m="true")):
            assert await st.volume() == (36, True)

    @pytest.mark.asyncio
    async def test_wake_only_presses_power_in_standby(self):
        """POWER is a toggle: pressing it on an awake speaker would turn it OFF."""
        st = SoundTouch("h")
        st.is_standby = AsyncMock(return_value=False)
        st.press = AsyncMock()
        assert await st.wake() is False
        st.press.assert_not_called()

    @pytest.mark.asyncio
    async def test_wake_from_standby(self):
        st = SoundTouch("h")
        st.is_standby = AsyncMock(side_effect=[True, True, False])
        st.press = AsyncMock()
        with patch.object(st_mod, "WAKE_SETTLE", 0), patch("asyncio.sleep", new_callable=AsyncMock):
            assert await st.wake() is True
        st.press.assert_awaited_once_with("POWER")

    @pytest.mark.asyncio
    async def test_prepare_unmutes_only_when_muted(self):
        st = SoundTouch("h")
        st.wake = AsyncMock(return_value=False)
        st.press = AsyncMock()
        st.set_volume = AsyncMock()
        st.volume = AsyncMock(return_value=(10, True))
        await st.prepare(30)
        st.press.assert_awaited_once_with("MUTE")
        st.set_volume.assert_awaited_once_with(30)
        st.press.reset_mock()
        st.volume = AsyncMock(return_value=(10, False))
        await st.prepare(30)
        st.press.assert_not_called()


# ---------------------------------------------------------------------------
# Volume: the SoundTouch's own volume is the truth
# ---------------------------------------------------------------------------

class _STBase:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_state_file = app.STATE_FILE
        app.STATE_FILE = Path(self.tmpdir) / "state.json"
        app.state.volume = 30
        app._volume_push_pending = False
        app._volume_set_ts = 0.0
        app._playing_foreign = False

    def teardown_method(self):
        app.STATE_FILE = self.orig_state_file
        app.state.volume = 30
        app._volume_push_pending = False
        app._volume_set_ts = 0.0
        app._playing_foreign = False
        app.soundtouch = None


class TestSoundTouchVolume(_STBase):
    @pytest.mark.asyncio
    async def test_connect_adopts_speaker_volume_not_renderer(self):
        app.soundtouch = fake_st(standby=False, volume=42)
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=0) as rget, \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as rset:
            await app._sync_device_volume(restore=True)
        assert app.state.volume == 42
        rget.assert_not_called()
        rset.assert_not_called()

    @pytest.mark.asyncio
    async def test_connect_in_standby_keeps_saved_volume(self):
        app.soundtouch = fake_st(standby=True, volume=0)
        await app._sync_device_volume(restore=True)
        assert app.state.volume == 30

    @pytest.mark.asyncio
    async def test_keepalive_ignores_renderer_zero_in_standby(self):
        """The CY920 logic would adopt the renderer's 0 as a user change."""
        app.soundtouch = fake_st(standby=True)
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=0) as rget:
            await app._poll_device_volume()
        rget.assert_not_called()
        assert app.state.volume == 30

    @pytest.mark.asyncio
    async def test_keepalive_adopts_change_made_on_speaker(self):
        app.soundtouch = fake_st(standby=False, volume=55)
        await app._poll_device_volume()
        assert app.state.volume == 55
        assert app.STATE_FILE.exists()

    @pytest.mark.asyncio
    async def test_api_volume_sets_speaker_and_renderer(self):
        app.soundtouch = fake_st()
        with patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as rset:
            await app.api_volume(app.VolumeRequest(level=47))
        app.soundtouch.set_volume.assert_awaited_once_with(47)
        rset.assert_awaited_once_with(47)
        assert app.state.volume == 47


class TestSoundTouchPlay(_STBase):
    def setup_method(self):
        super().setup_method()
        app.state.queue.clear()
        app.state.queue.append(app.Track(id="a", title="A", artist="", source_type="youtube",
                                         source_url="u", local_path="/nonexistent.mp3"))
        app.state.current_index = 0

    def teardown_method(self):
        super().teardown_method()
        app.state.queue.clear()
        app.state.current_index = -1

    @pytest.mark.asyncio
    async def test_play_prepares_speaker_and_pushes_renderer_volume(self):
        app.soundtouch = fake_st(standby=True)
        order = []
        app.soundtouch.prepare = AsyncMock(side_effect=lambda v: order.append(("prepare", v)))
        with patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_set_uri", new_callable=AsyncMock,
                          side_effect=lambda t: order.append(("uri", t.id))), \
             patch.object(app, "dlna_play", new_callable=AsyncMock), \
             patch.object(app, "_preload_next", new_callable=AsyncMock), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as rset, \
             patch.object(app, "_delayed_push_volume", new_callable=MagicMock) as delayed, \
             patch("asyncio.create_task") as ct:
            await app._play_current()
        assert order == [("prepare", 30), ("uri", "a")]   # wake BEFORE the URI
        rset.assert_awaited_once_with(30)
        delayed.assert_called_once_with(3.0)
        ct.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_failure_still_plays(self):
        app.soundtouch = fake_st()
        app.soundtouch.prepare = AsyncMock(side_effect=OSError("8090 down"))
        with patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_set_uri", new_callable=AsyncMock) as seturi, \
             patch.object(app, "dlna_play", new_callable=AsyncMock), \
             patch.object(app, "_preload_next", new_callable=AsyncMock), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock), \
             patch.object(app, "_delayed_push_volume", new_callable=MagicMock), \
             patch("asyncio.create_task"):
            await app._play_current()
        seturi.assert_awaited_once()


# ---------------------------------------------------------------------------
# Who was playing: never auto-advance after someone else's stream
# ---------------------------------------------------------------------------

class TestPlaybackOwner:
    def test_foreign_uri(self):
        assert app._is_foreign_uri("http://192.168.1.101:8123/api/tts_proxy/x.mp3") is True
        assert app._is_foreign_uri("http://jazzblues.ice.infomaniak.ch/jazzblues-high.mp3") is True
        assert app._is_foreign_uri(f"{app.BASE_URL}/stream/abc12345") is False
        assert app._is_foreign_uri("") is False   # renderer reports nothing: assume ours


class _AdvanceBase(_STBase):
    """Run exactly one auto_advance_loop iteration after a PLAYING state."""
    def setup_method(self):
        super().setup_method()
        app._last_transport_state = "PLAYING"
        app._user_stop_ts = 0.0
        app.state.queue.clear()
        for i in "ab":
            app.state.queue.append(app.Track(id=i, title=i, artist="", source_type="youtube",
                                             source_url=i, local_path=f"/{i}.mp3"))
        app.state.current_index = 0

    def teardown_method(self):
        super().teardown_method()
        app._last_transport_state = "STOPPED"
        app.state.queue.clear()
        app.state.current_index = -1

    async def run_once(self, transport="STOPPED"):
        calls = {"n": 0}

        async def one_tick(_):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("stop loop")
        with patch.object(app.asyncio, "sleep", side_effect=one_tick), \
             patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_get_transport_state", new_callable=AsyncMock, return_value=transport), \
             patch.object(app, "_play_current", new_callable=AsyncMock) as play:
            with pytest.raises(RuntimeError):
                await app.auto_advance_loop()
        return play


class TestAutoAdvanceGuards(_AdvanceBase):
    @pytest.mark.asyncio
    async def test_own_track_end_still_advances(self):
        play = await self.run_once()
        play.assert_awaited_once()
        assert app.state.current_index == 1

    @pytest.mark.asyncio
    async def test_no_advance_after_foreign_stream(self):
        """HA preset / announcement on the same renderer ended: not our queue."""
        app._playing_foreign = True
        play = await self.run_once()
        play.assert_not_called()
        assert app.state.current_index == 0

    @pytest.mark.asyncio
    async def test_no_advance_when_soundtouch_went_to_standby(self):
        """'Music off' from HA must not wake the speaker with the next track."""
        app.soundtouch = fake_st(standby=True)
        play = await self.run_once()
        play.assert_not_called()

    @pytest.mark.asyncio
    async def test_soundtouch_awake_still_advances(self):
        app.soundtouch = fake_st(standby=False)
        play = await self.run_once()
        play.assert_awaited_once()


# ---------------------------------------------------------------------------
# Static devices (the SA-4 doesn't answer SSDP)
# ---------------------------------------------------------------------------

class TestStaticDevices:
    def teardown_method(self):
        app.discovered_devices.clear()

    @pytest.mark.asyncio
    async def test_registered_while_another_device_is_connected(self):
        url = "http://192.168.1.37:8091/XD/x.xml"
        dev = MagicMock(friendly_name="Bose_out", model_name="SoundTouch")
        dev.service.return_value = object()
        factory = MagicMock()
        factory.async_create_device = AsyncMock(return_value=dev)
        with patch.object(app, "STATIC_DEVICES", [url]), \
             patch.object(app, "UpnpFactory", return_value=factory), \
             patch.object(app, "av_transport", object()), \
             patch.object(app, "connect_to_device", new_callable=AsyncMock) as connect:
            await app._register_static_devices()
        assert app.discovered_devices["Bose_out"]["url"] == url
        connect.assert_not_called()   # already connected elsewhere: list only

    @pytest.mark.asyncio
    async def test_unreachable_static_device_is_skipped(self):
        factory = MagicMock()
        factory.async_create_device = AsyncMock(side_effect=OSError("down"))
        with patch.object(app, "STATIC_DEVICES", ["http://10.9.9.9/x.xml"]), \
             patch.object(app, "UpnpFactory", return_value=factory):
            await app._register_static_devices()
        assert app.discovered_devices == {}


# ---------------------------------------------------------------------------
# Cache cap
# ---------------------------------------------------------------------------

class TestCacheCap:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = app.CACHE_DIR
        app.CACHE_DIR = self.tmp
        app.state.queue.clear()

    def teardown_method(self):
        app.CACHE_DIR = self.orig
        app.state.queue.clear()

    def _mp3(self, name, mb, age):
        p = self.tmp / f"{name}.mp3"
        p.write_bytes(b"\0" * int(mb * 1024 * 1024))
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def test_evicts_oldest_but_never_queued_or_kept(self):
        old_queued = self._mp3("old_queued", 1, 400)
        oldest = self._mp3("oldest", 1, 300)
        middle = self._mp3("middle", 1, 200)
        fresh = self._mp3("fresh", 1, 100)
        app.state.queue.append(app.Track(id="q", title="", artist="", source_type="youtube",
                                         source_url="q", local_path=str(old_queued)))
        with patch.object(app, "CACHE_MAX_MB", 2):
            app._enforce_cache_cap(keep={str(fresh)})
        assert old_queued.exists() and fresh.exists()
        assert not oldest.exists() and not middle.exists()

    def test_under_cap_or_disabled_keeps_everything(self):
        a = self._mp3("a", 1, 10)
        with patch.object(app, "CACHE_MAX_MB", 5):
            app._enforce_cache_cap()
        with patch.object(app, "CACHE_MAX_MB", 0):
            app._enforce_cache_cap()
        assert a.exists()


# ---------------------------------------------------------------------------
# YouTube Music: tolerant parsing + enqueue modes
# ---------------------------------------------------------------------------

import ytmusic


class TestYtmParsing:
    def test_mixed_mood_page_keeps_known_items_only(self):
        """The real Blues page mixed videos/albums among playlists and crashed
        ytmusicapi's parser; ours keeps each recognised kind."""
        def two_row(title, nav):
            return {"musicTwoRowItemRenderer": {"title": {"runs": [{"text": title}]},
                    "subtitle": {"runs": [{"text": "sub"}]}, "navigationEndpoint": nav}}
        page = {"contents": [
            two_row("Blues Instrumentals", {"browseEndpoint": {"browseId": "VLRDCLAK5uy_x"}}),
            two_row("Rock Me Baby (live)", {"watchEndpoint": {"videoId": "vid12345678"}}),
            two_row("An Album", {"browseEndpoint": {"browseId": "MPREb_abc"}}),
            two_row("Mystery", {"browseEndpoint": {"browseId": "FEmusic_x"}}),
        ]}
        items = [ytmusic._item_from_two_row(r) for r in ytmusic._walk(page, "musicTwoRowItemRenderer")]
        kinds = [(i["kind"], i["id"]) if i else None for i in items]
        assert kinds == [("playlist", "RDCLAK5uy_x"), ("video", "vid12345678"), ("album", "MPREb_abc"), None]

    def test_secs(self):
        assert ytmusic._secs("3:45") == 225 and ytmusic._secs("1:02:03") == 3723
        assert ytmusic._secs(210) == 210 and ytmusic._secs("n/a") == 0


class _QueueBase:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_state_file = app.STATE_FILE
        app.STATE_FILE = Path(self.tmpdir) / "state.json"
        app.state.queue.clear()
        app.state.current_index = -1

    def teardown_method(self):
        app.STATE_FILE = self.orig_state_file
        app.state.queue.clear()
        app.state.current_index = -1


def _tracks(*ids):
    return [{"videoId": i, "title": f"T{i}", "artist": "A", "duration": 100} for i in ids]


class TestYtmEnqueue(_QueueBase):
    async def _enqueue(self, mode, kind="playlist", ids=("aaaaaaaaaaa", "bbbbbbbbbbb"), ready=True):
        with patch.object(app, "_ytm_resolve", new_callable=AsyncMock, return_value=_tracks(*ids)), \
             patch.object(app, "_download_playlist_tracks", new_callable=MagicMock) as dl, \
             patch("asyncio.create_task") as ct, \
             patch.object(app, "_device_ready", return_value=ready), \
             patch.object(app, "dlna_stop", new_callable=AsyncMock) as stop:
            r = await app.ytm_enqueue(app.YtmEnqueueRequest(kind=kind, id="x", mode=mode))
        return r, dl, stop

    def _seed(self):
        for t in ("one", "two"):
            app.state.queue.append(app.Track(id=t, title=t, artist="", source_type="youtube",
                                             source_url=f"https://www.youtube.com/watch?v={t:0<11}",
                                             local_path=f"/{t}.mp3"))
        app.state.current_index = 0

    @pytest.mark.asyncio
    async def test_end_appends_without_interrupting(self):
        self._seed()
        r, dl, stop = await self._enqueue("end")
        assert r["count"] == 2 and [t.id for t in app.state.queue][:2] == ["one", "two"]
        assert app.state.current_index == 0
        stop.assert_not_called()
        assert dl.call_args.kwargs["play_first"] is False

    @pytest.mark.asyncio
    async def test_next_inserts_after_current(self):
        self._seed()
        await self._enqueue("next")
        assert [t.title for t in app.state.queue] == ["one", "Taaaaaaaaaaa", "Tbbbbbbbbbbb", "two"]
        assert app.state.current_index == 0

    @pytest.mark.asyncio
    async def test_now_stops_and_plays_first_new_track(self):
        self._seed()
        r, dl, stop = await self._enqueue("now")
        assert app.state.current_index == 1
        assert app.state.queue[1].title == "Taaaaaaaaaaa"
        stop.assert_awaited_once()
        assert dl.call_args.kwargs["play_first"] is True
        assert time.monotonic() - app._user_stop_ts < 2   # auto-advance suppressed

    @pytest.mark.asyncio
    async def test_empty_queue_starts_playing(self):
        r, dl, stop = await self._enqueue("end")
        assert app.state.current_index == 0
        assert dl.call_args.kwargs["play_first"] is True
        stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicates_skipped(self):
        app.state.queue.append(app.Track(id="d", title="d", artist="", source_type="youtube",
                                         source_url="https://music.youtube.com/watch?v=aaaaaaaaaaa"))
        app.state.current_index = 0
        r, _, _ = await self._enqueue("end")
        assert r["count"] == 1 and r["skipped"] == 1

    @pytest.mark.asyncio
    async def test_ytm_failure_is_a_clean_502(self):
        def boom(*a):
            raise KeyError("navigationEndpoint")
        with pytest.raises(app.HTTPException) as ei:
            await app._ytm_call(boom)
        assert ei.value.status_code == 502


class TestNextRefreshesGaplessSuccessor(_QueueBase):
    """A playing, B already handed to the renderer as its gapless successor;
    'next' inserts X between them. Once X is downloaded the renderer must get
    X as its successor, or it plays A -> B and skips X."""

    def setup_method(self):
        super().setup_method()
        self.orig_cache = app.CACHE_DIR
        app.CACHE_DIR = Path(self.tmpdir)
        app.state.play_mode = "NORMAL"
        for t in ("A", "B"):
            app.state.queue.append(app.Track(id=t, title=t, artist="", source_type="youtube",
                                             source_url=f"https://www.youtube.com/watch?v={t * 11}",
                                             local_path=f"/{t}.mp3"))
        app.state.current_index = 0

    def teardown_method(self):
        app.CACHE_DIR = self.orig_cache
        super().teardown_method()

    @pytest.mark.asyncio
    async def test_inserted_next_track_becomes_the_gapless_successor(self):
        async def dl(url, tid):
            return {"local_path": f"/{tid}.mp3", "content_type": "audio/mpeg", "content_length": 1,
                    "title": "X", "artist": "", "duration": 200}
        with patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_set_next_uri", new_callable=AsyncMock) as set_next, \
             patch.object(app, "_ytm_resolve", new_callable=AsyncMock, return_value=_tracks("xxxxxxxxxxx")), \
             patch.object(app, "download_youtube", side_effect=dl), \
             patch("asyncio.create_task") as ct:
            await app._preload_next()
            assert set_next.call_args.args[0].id == "B"          # renderer has B queued
            await app.ytm_enqueue(app.YtmEnqueueRequest(kind="song", id="x", mode="next"))
            assert [t.title for t in app.state.queue] == ["A", "Txxxxxxxxxxx", "B"]
            await ct.call_args.args[0]                            # run the download batch
        assert set_next.call_args.args[0].title == "X"          # renderer now plays X after A

    @pytest.mark.asyncio
    async def test_end_mode_does_not_touch_the_successor(self):
        async def dl(url, tid):
            return {"local_path": f"/{tid}.mp3", "content_type": "audio/mpeg", "content_length": 1,
                    "title": "X", "artist": "", "duration": 200}
        with patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_set_next_uri", new_callable=AsyncMock) as set_next, \
             patch.object(app, "_ytm_resolve", new_callable=AsyncMock, return_value=_tracks("xxxxxxxxxxx")), \
             patch.object(app, "download_youtube", side_effect=dl), \
             patch("asyncio.create_task") as ct:
            await app.ytm_enqueue(app.YtmEnqueueRequest(kind="song", id="x", mode="end"))
            await ct.call_args.args[0]
        set_next.assert_not_called()                             # B stays the successor

    @pytest.mark.asyncio
    async def test_going_past_a_downloading_track_is_logged(self, caplog):
        app.state.queue.insert(1, app.Track(id="X", title="X", artist="", source_type="youtube",
                                            source_url="u-x"))   # not downloaded yet
        with patch.object(app, "dlna_get_position", new_callable=AsyncMock,
                          return_value={"track_uri": f"{app.BASE_URL}/stream/B"}), \
             patch.object(app, "_preload_next", new_callable=AsyncMock), \
             patch.object(app, "_uri_change_ts", 0.0), \
             caplog.at_level("INFO", logger="bulb_voice"):
            await app._sync_index_with_device()
        assert app.state.current_index == 2
        assert "Renderer went past X (still downloading)" in caplog.text


# ---------------------------------------------------------------------------
# Garage endpoints
# ---------------------------------------------------------------------------

class TestGarage(_STBase):
    def teardown_method(self):
        super().teardown_method()

    @pytest.mark.asyncio
    async def test_preset_wakes_then_sends_raw_station_url(self):
        app.soundtouch = fake_st(standby=True)
        order = []
        app.soundtouch.prepare = AsyncMock(side_effect=lambda v: order.append("prepare"))
        with patch.object(app, "_ensure_garage_device", new_callable=AsyncMock, return_value="Bose_out"), \
             patch.object(app, "dlna_set_raw_uri", new_callable=AsyncMock,
                          side_effect=lambda url, t, ct: order.append(url)), \
             patch.object(app, "dlna_play", new_callable=AsyncMock), \
             patch.object(app, "_push_volume", new_callable=AsyncMock) as push, \
             patch.object(app, "_delayed_push_volume", new_callable=MagicMock), \
             patch("asyncio.create_task"):
            r = await app.garage_preset(3)
        assert r["name"] == "Jazz Radio Blues"
        # raw URL (same one HA uses), not bulb_voice's /stream proxy
        assert order == ["prepare", "http://jazzblues.ice.infomaniak.ch/jazzblues-high.mp3"]
        push.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_preset_404(self):
        with pytest.raises(app.HTTPException) as ei:
            await app.garage_preset(9)
        assert ei.value.status_code == 404

    def test_all_presets_are_plain_http(self):
        """The SoundTouch renderer rejects https."""
        for p in app._garage_cfg()["presets"]:
            assert p["url"].startswith("http://"), p

    @pytest.mark.asyncio
    async def test_state_detects_preset_started_by_home_assistant(self):
        app.soundtouch = fake_st(standby=False, volume=33)
        app.soundtouch.now_playing = AsyncMock(return_value={"source": "UPNP", "play_status": "PLAY_STATE"})
        dev = MagicMock(friendly_name="Bose_out")
        with patch.object(app, "device", dev), patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_get_transport_state", new_callable=AsyncMock, return_value="PLAYING"), \
             patch.object(app, "dlna_get_position", new_callable=AsyncMock,
                          return_value={"track_uri": "http://live.magicfm.ro/magicfm.aacp"}):
            s = await app.garage_state()
        assert s["connected"] and s["preset"] == 4 and s["volume"] == 33 and s["standby"] is False

    def test_dial_stations_from_config(self):
        st = app._dial_stations(app._garage_cfg()["presets"])
        assert [s["n"] for s in st] == [1, 2, 3, 4, 5, 6]
        assert st[3] == {"n": 4, "name": "Magic FM", "freq": 89.5, "ps": "MAGIC FM"}
        assert all(87.5 <= s["freq"] <= 108 and len(s["ps"]) <= 8 for s in st)
        assert len({s["freq"] for s in st}) == len(st), "two presets on one frequency"

    def test_dial_stations_fill_missing_or_bad_values(self):
        presets = [{"name": "Radio Guerrilla", "url": "http://a/"},
                   {"name": "B", "url": "http://b/", "freq": 150},
                   {"name": "C", "url": "http://c/", "freq": True},
                   {"name": "D", "url": "http://d/", "freq": 99, "ps": "TOO LONG NAME"}]
        st = app._dial_stations(presets)
        assert st[0]["ps"] == "RADIO GU"
        assert [s["freq"] for s in st[:3]] == [90.9, 95.6, 100.4]   # spread across the band
        assert st[3]["freq"] == 99.0 and st[3]["ps"] == "TOO LONG"
        assert app._dial_stations([]) == []

    @pytest.mark.asyncio
    async def test_state_lists_dial_stations_even_when_disconnected(self):
        with patch.object(app, "device", None):
            s = await app.garage_state()
        assert not s["connected"]
        assert [x["name"] for x in s["stations"]] == s["presets"]

    @pytest.mark.asyncio
    async def test_power_puts_soundtouch_in_standby(self):
        app.soundtouch = fake_st()
        app.soundtouch.standby = AsyncMock()
        with patch.object(app, "_garage_connected", return_value=True):
            await app.garage_power()
        app.soundtouch.standby.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_power_never_stops_another_rooms_speaker(self):
        """/garage left open in one tab, Volumio selected from another client."""
        dev = MagicMock(friendly_name="Volumio-UPnP/AV")
        with patch.object(app, "device", dev), \
             patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_stop", new_callable=AsyncMock) as stop:
            with pytest.raises(app.HTTPException) as ei:
                await app.garage_power()
        assert ei.value.status_code == 409
        stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_power_on_garage_without_soundtouch_side_channel_stops_it(self):
        dev = MagicMock(friendly_name="Bose_out")
        with patch.object(app, "device", dev), \
             patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_stop", new_callable=AsyncMock) as stop:
            await app.garage_power()
        stop.assert_awaited_once()


class TestGarageSwitch:
    @pytest.mark.asyncio
    async def test_devices_names_the_garage_speaker(self):
        r = await app.api_devices()
        assert r["garage"] == "Bose_out"

    def test_main_app_switches_to_garage_page(self):
        js = (app.STATIC_DIR / "app.js").read_text()
        assert 'location.replace("/garage")' in js and 'location.href = "/garage"' in js
        assert '"#speakers"' in js
        assert 'href="/#speakers"' in (app.STATIC_DIR / "garage.html").read_text()


class TestGaragePage:
    def test_garage_page_and_assets_exist(self):
        html = (app.STATIC_DIR / "garage.html").read_text()
        for asset in ("/static/garage.css", "/static/garage.js"):
            assert asset in html
            assert (app.STATIC_DIR / asset.split("/")[-1]).exists()

    def test_dial_elements_used_by_js_exist(self):
        import re
        html = (app.STATIC_DIR / "garage.html").read_text()
        js = (app.STATIC_DIR / "garage.js").read_text()
        ids = set(re.findall(r'\$\("([\w-]+)"\)', js))
        missing = [i for i in ids if f'id="{i}"' not in html]
        assert not missing, missing

    def test_no_innerhtml_with_server_data(self):
        """YouTube titles are untrusted: the only innerHTML is the static SVG defs."""
        import re
        js = (app.STATIC_DIR / "garage.js").read_text()
        assert len(re.findall(r"\.innerHTML\s*=", js)) == 1
        assert "insertAdjacentHTML" not in js and "outerHTML" not in js
