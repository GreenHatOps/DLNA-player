"""Unit tests for Bulb Voice — pure logic, no DLNA device required."""
import json
import time
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

# Import the module under test
import app


class TestQueueMarkup:
    def test_decorative_rod_removed_without_removing_queue_controls(self):
        class Elements(HTMLParser):
            def __init__(self):
                super().__init__()
                self.attrs = []

            def handle_starttag(self, tag, attrs):
                self.attrs.append(dict(attrs))

        page = Elements()
        page.feed((app.STATIC_DIR / "index.html").read_text())
        ids = [attrs["id"] for attrs in page.attrs if "id" in attrs]
        for required in ("queue-list", "queue-stage", "queue-spacer", "queue-pos",
                         "queue-scroll", "queue-scroll-thumb"):
            assert ids.count(required) == 1
        assert all("queue-rod" not in attrs.get("class", "").split()
                   for attrs in page.attrs)


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------

class TestParseDuration:
    def test_normal(self):
        assert app._parse_duration("0:03:45") == 225

    def test_hours(self):
        assert app._parse_duration("1:02:03") == 3723

    def test_fractional_seconds(self):
        assert app._parse_duration("0:01:30.5") == 90

    def test_not_implemented(self):
        assert app._parse_duration("NOT_IMPLEMENTED") == 0

    def test_empty(self):
        assert app._parse_duration("") == 0

    def test_none(self):
        assert app._parse_duration(None) == 0

    def test_malformed(self):
        assert app._parse_duration("bad") == 0

    def test_two_parts(self):
        assert app._parse_duration("03:45") == 0


# ---------------------------------------------------------------------------
# _xml_escape
# ---------------------------------------------------------------------------

class TestXmlEscape:
    def test_ampersand(self):
        assert app._xml_escape("Tom & Jerry") == "Tom &amp; Jerry"

    def test_angle_brackets(self):
        assert app._xml_escape("<script>") == "&lt;script&gt;"

    def test_quotes(self):
        assert app._xml_escape('say "hi"') == "say &quot;hi&quot;"

    def test_combined(self):
        assert app._xml_escape('a & b < c > d "e"') == 'a &amp; b &lt; c &gt; d &quot;e&quot;'

    def test_clean_string(self):
        assert app._xml_escape("Hello World") == "Hello World"

    def test_empty(self):
        assert app._xml_escape("") == ""


# ---------------------------------------------------------------------------
# _detect_type
# ---------------------------------------------------------------------------

class TestDetectType:
    def test_youtube_watch(self):
        assert app._detect_type("https://www.youtube.com/watch?v=abc123") == "youtube"

    def test_youtu_be(self):
        assert app._detect_type("https://youtu.be/abc123") == "youtube"

    def test_music_youtube(self):
        assert app._detect_type("https://music.youtube.com/watch?v=abc") == "youtube"

    def test_mp3_url(self):
        assert app._detect_type("https://example.com/song.mp3") == "url"

    def test_flac_url(self):
        assert app._detect_type("https://example.com/song.flac") == "url"

    def test_radio_stream(self):
        assert app._detect_type("https://stream.radio.com/live") == "radio"

    def test_random_url(self):
        assert app._detect_type("https://example.com/page") == "radio"


# ---------------------------------------------------------------------------
# _is_playlist_url
# ---------------------------------------------------------------------------

class TestIsPlaylistUrl:
    def test_playlist_with_list_param(self):
        assert app._is_playlist_url("https://youtube.com/watch?v=x&list=PLabc") is True

    def test_playlist_url(self):
        assert app._is_playlist_url("https://youtube.com/playlist?list=PLabc") is True

    def test_channel_at(self):
        assert app._is_playlist_url("https://youtube.com/@LofiGirl") is True

    def test_channel_path(self):
        assert app._is_playlist_url("https://youtube.com/channel/UCabc") is True

    def test_single_video(self):
        assert app._is_playlist_url("https://youtube.com/watch?v=abc123") is False

    def test_youtu_be(self):
        assert app._is_playlist_url("https://youtu.be/abc123") is False


# ---------------------------------------------------------------------------
# _dlna_profile
# ---------------------------------------------------------------------------

class TestDlnaProfile:
    def test_mp3(self):
        assert app._dlna_profile("audio/mpeg") == "DLNA.ORG_PN=MP3"

    def test_mp4(self):
        assert app._dlna_profile("audio/mp4") == "DLNA.ORG_PN=AAC_ISO"

    def test_flac(self):
        assert app._dlna_profile("audio/flac") == "*"

    def test_unknown(self):
        assert app._dlna_profile("audio/ogg") == "*"


# ---------------------------------------------------------------------------
# _track_dict
# ---------------------------------------------------------------------------

class TestTrackDict:
    def test_ready_with_local_path(self):
        t = app.Track(id="1", title="T", artist="A", source_type="youtube",
                      source_url="u", local_path="/tmp/x.mp3")
        d = app._track_dict(t)
        assert d["ready"] is True

    def test_ready_with_direct_url(self):
        t = app.Track(id="1", title="T", artist="A", source_type="radio",
                      source_url="u", direct_url="http://stream")
        d = app._track_dict(t)
        assert d["ready"] is True

    def test_not_ready_youtube_no_path(self):
        t = app.Track(id="1", title="T", artist="A", source_type="youtube",
                      source_url="u")
        d = app._track_dict(t)
        assert d["ready"] is False

    def test_ready_non_youtube(self):
        t = app.Track(id="1", title="T", artist="A", source_type="radio",
                      source_url="u")
        d = app._track_dict(t)
        assert d["ready"] is True

    def test_fields(self):
        t = app.Track(id="x", title="Song", artist="Band", source_type="youtube",
                      source_url="u", duration=120, local_path="/tmp/x.mp3")
        d = app._track_dict(t)
        assert d == {"id": "x", "title": "Song", "artist": "Band",
                     "source_type": "youtube", "duration": 120, "ready": True}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = Path(self.tmpdir) / "state.json"
        self.original_state_file = app.STATE_FILE
        app.STATE_FILE = self.state_file
        # Reset state
        app.state.queue.clear()
        app.state.current_index = -1
        app.state.volume = 30

    def teardown_method(self):
        app.STATE_FILE = self.original_state_file
        app.state.queue.clear()
        app.state.current_index = -1
        app.state.volume = 30

    def test_save_and_load(self):
        t = app.Track(id="a", title="Song A", artist="Art", source_type="youtube",
                      source_url="http://yt/a", local_path="/tmp/a.mp3")
        app.state.queue.append(t)
        app.state.current_index = 0
        app.state.volume = 55
        app._save_state()

        # Reset and reload
        app.state.queue.clear()
        app.state.current_index = -1
        app.state.volume = 30

        with patch.object(Path, "exists", return_value=True):
            app._load_state()

        assert len(app.state.queue) == 1
        assert app.state.queue[0].title == "Song A"
        assert app.state.current_index == 0
        assert app.state.volume == 55

    def test_load_skips_missing_files(self):
        data = {
            "current_index": 0, "volume": 30,
            "queue": [{"id": "b", "title": "B", "artist": "X",
                       "source_type": "youtube", "source_url": "u",
                       "local_path": "/nonexistent/b.mp3", "duration": 0,
                       "direct_url": "", "content_type": "audio/mpeg",
                       "content_length": 0}]
        }
        self.state_file.write_text(json.dumps(data))
        app._load_state()
        assert len(app.state.queue) == 0  # skipped because file doesn't exist

    def test_load_corrupt_json(self):
        self.state_file.write_text("not json{{{")
        app._load_state()  # should not crash
        assert len(app.state.queue) == 0

    def test_load_clamps_index(self):
        data = {"current_index": 99, "volume": 30, "queue": []}
        self.state_file.write_text(json.dumps(data))
        app._load_state()
        assert app.state.current_index == -1

    def test_load_nonexistent_file(self):
        app._load_state()  # file doesn't exist, should be no-op
        assert len(app.state.queue) == 0


# ---------------------------------------------------------------------------
# Queue management (unit-level, no HTTP)
# ---------------------------------------------------------------------------

class TestQueueLogic:
    def setup_method(self):
        app.state.queue.clear()
        app.state.current_index = -1

    def teardown_method(self):
        app.state.queue.clear()
        app.state.current_index = -1

    def test_device_ready_when_none(self):
        assert app._device_ready() is False

    def test_device_ready_when_set(self):
        app.av_transport = "mock"
        app.rendering_control = "mock"
        assert app._device_ready() is True
        app.av_transport = None
        app.rendering_control = None


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

class TestNormalizeYtUrl:
    def test_standard_url(self):
        assert app._normalize_yt_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert app._normalize_yt_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_music_url(self):
        assert app._normalize_yt_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x") == "dQw4w9WgXcQ"

    def test_non_youtube(self):
        url = "https://radio.com/stream"
        assert app._normalize_yt_url(url) == url


class TestIsDuplicate:
    def setup_method(self):
        app.state.queue.clear()

    def teardown_method(self):
        app.state.queue.clear()

    def test_exact_match(self):
        app.state.queue.append(app.Track(
            id="1", title="T", artist="A", source_type="youtube",
            source_url="https://www.youtube.com/watch?v=abc12345678"))
        assert app._is_duplicate("https://www.youtube.com/watch?v=abc12345678") is True

    def test_different_format_same_video(self):
        app.state.queue.append(app.Track(
            id="1", title="T", artist="A", source_type="youtube",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        assert app._is_duplicate("https://youtu.be/dQw4w9WgXcQ") is True

    def test_not_duplicate(self):
        app.state.queue.append(app.Track(
            id="1", title="T", artist="A", source_type="youtube",
            source_url="https://www.youtube.com/watch?v=abc12345678"))
        assert app._is_duplicate("https://www.youtube.com/watch?v=xyz98765432") is False

    def test_empty_queue(self):
        assert app._is_duplicate("https://www.youtube.com/watch?v=abc12345678") is False

    def test_radio_exact_match(self):
        app.state.queue.append(app.Track(
            id="1", title="T", artist="A", source_type="radio",
            source_url="http://stream.radio.com/live"))
        assert app._is_duplicate("http://stream.radio.com/live") is True


# ---------------------------------------------------------------------------
# DownloadProgress
# ---------------------------------------------------------------------------

class TestDownloadProgress:
    def test_default_values(self):
        dp = app.DownloadProgress()
        assert dp.total == 0
        assert dp.done == 0
        assert dp.current_title == ""
        assert dp.track_ids == []
        assert dp.started_at == 0.0

    def test_with_started_at(self):
        now = time.time()
        dp = app.DownloadProgress(total=5, done=2, started_at=now,
                                  track_ids=["a", "b", "c", "d", "e"])
        assert dp.total == 5
        assert dp.done == 2
        assert dp.started_at == now
        assert len(dp.track_ids) == 5

    def test_elapsed_calculation(self):
        dp = app.DownloadProgress(total=3, done=1, started_at=time.time() - 10)
        elapsed = time.time() - dp.started_at
        assert 9.5 < elapsed < 11


# ---------------------------------------------------------------------------
# _nudge_device
# ---------------------------------------------------------------------------

class TestNudgeDevice:
    @pytest.mark.asyncio
    async def test_nudge_does_not_raise_on_failure(self):
        """_nudge_device should silently swallow errors."""
        await app._nudge_device("http://192.168.1.254:9999/nonexistent")
        # No exception = pass

    @pytest.mark.asyncio
    async def test_nudge_with_empty_url(self):
        await app._nudge_device("")
        # No exception = pass


# ---------------------------------------------------------------------------
# State persistence with new fields
# ---------------------------------------------------------------------------

class TestStatePersistenceNewFields:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_file = Path(self.tmpdir) / "state.json"
        self.original_state_file = app.STATE_FILE
        app.STATE_FILE = self.state_file
        app.state.queue.clear()
        app.state.current_index = -1
        app.state.volume = 30
        app.state.last_device = ""
        app.state.last_device_url = ""

    def teardown_method(self):
        app.STATE_FILE = self.original_state_file
        app.state.queue.clear()
        app.state.current_index = -1
        app.state.volume = 30
        app.state.last_device = ""
        app.state.last_device_url = ""

    def test_save_and_load_last_device(self):
        app.state.last_device = "Office-Bulb"
        app.state.last_device_url = "http://192.168.1.49:8080/description.xml"
        app._save_state()

        app.state.last_device = ""
        app.state.last_device_url = ""
        app._load_state()

        assert app.state.last_device == "Office-Bulb"
        assert app.state.last_device_url == "http://192.168.1.49:8080/description.xml"

    def test_load_old_state_without_last_device(self):
        """State files from before last_device was added should load gracefully."""
        data = {"current_index": -1, "volume": 30, "queue": []}
        self.state_file.write_text(json.dumps(data))
        app._load_state()
        assert app.state.last_device == ""
        assert app.state.last_device_url == ""


# ---------------------------------------------------------------------------
# _track_id_from_stream_uri (gapless index sync)
# ---------------------------------------------------------------------------

class TestTrackIdFromStreamUri:
    def test_normal(self):
        assert app._track_id_from_stream_uri("http://192.168.1.5:8000/stream/abc12345") == "abc12345"

    def test_trailing_slash(self):
        assert app._track_id_from_stream_uri("http://192.168.1.5:8000/stream/abc12345/") == "abc12345"

    def test_query_string(self):
        assert app._track_id_from_stream_uri("http://192.168.1.5:8000/stream/abc12345?foo=1") == "abc12345"

    def test_empty(self):
        assert app._track_id_from_stream_uri("") is None

    def test_not_implemented(self):
        assert app._track_id_from_stream_uri("NOT_IMPLEMENTED") is None

    def test_foreign_uri(self):
        assert app._track_id_from_stream_uri("http://radio.example.com/live.mp3") is None

    def test_marker_but_no_id(self):
        assert app._track_id_from_stream_uri("http://192.168.1.5:8000/stream/") is None


# ---------------------------------------------------------------------------
# _mark_user_stop (auto-advance suppression)
# ---------------------------------------------------------------------------

class TestMarkUserStop:
    def teardown_method(self):
        app._user_stop_ts = 0.0

    def test_mark_sets_recent_timestamp(self):
        import time as _time
        app._mark_user_stop()
        assert _time.monotonic() - app._user_stop_ts < 1.0

    def test_default_is_outside_window(self):
        import time as _time
        app._user_stop_ts = 0.0
        assert _time.monotonic() - app._user_stop_ts >= app.USER_STOP_WINDOW


# ---------------------------------------------------------------------------
# _drop_track_at (queue removal index adjustment)
# ---------------------------------------------------------------------------

class TestDropTrackAt:
    def _mk(self, tid):
        return app.Track(id=tid, title=tid, artist="", source_type="youtube",
                         source_url=f"https://youtu.be/{tid}")

    def setup_method(self):
        app.state.queue = [self._mk("aa"), self._mk("bb"), self._mk("cc")]
        app.state.current_index = 1

    def teardown_method(self):
        app.state.queue.clear()
        app.state.current_index = -1

    def test_remove_before_current_shifts_index(self):
        app._drop_track_at(0)
        assert [t.id for t in app.state.queue] == ["bb", "cc"]
        assert app.state.current_index == 0  # still points at "bb"

    def test_remove_after_current_keeps_index(self):
        app._drop_track_at(2)
        assert [t.id for t in app.state.queue] == ["aa", "bb"]
        assert app.state.current_index == 1

    def test_remove_current_mid_queue_points_to_next(self):
        app._drop_track_at(1)
        assert [t.id for t in app.state.queue] == ["aa", "cc"]
        assert app.state.current_index == 1  # now points at "cc"

    def test_remove_current_last_resets_index(self):
        app.state.current_index = 2
        app._drop_track_at(2)
        assert app.state.current_index == -1

    def test_remove_only_track_resets_index(self):
        app.state.queue = [self._mk("solo")]
        app.state.current_index = 0
        app._drop_track_at(0)
        assert app.state.queue == []
        assert app.state.current_index == -1

    def test_deletes_cached_file(self, tmp_path):
        f = tmp_path / "aa.mp3"
        f.write_bytes(b"x")
        app.state.queue[0].local_path = str(f)
        app._drop_track_at(0)
        assert not f.exists()


# ---------------------------------------------------------------------------
# _get_next_index / _get_prev_index (play modes, manual vs auto)
# ---------------------------------------------------------------------------

class TestNextPrevIndex:
    def _mk(self, tid):
        return app.Track(id=tid, title=tid, artist="", source_type="youtube",
                         source_url=f"https://youtu.be/{tid}")

    def setup_method(self):
        app.state.queue = [self._mk("aa"), self._mk("bb"), self._mk("cc")]
        app.state.current_index = 1
        app.state.play_mode = "NORMAL"

    def teardown_method(self):
        app.state.queue.clear()
        app.state.current_index = -1
        app.state.play_mode = "NORMAL"

    # next
    def test_normal_next(self):
        assert app._get_next_index() == 2

    def test_normal_end_of_queue(self):
        app.state.current_index = 2
        assert app._get_next_index() is None

    def test_repeat_all_wraps(self):
        app.state.play_mode = "REPEAT_ALL"
        app.state.current_index = 2
        assert app._get_next_index() == 0

    def test_repeat_one_auto_repeats(self):
        app.state.play_mode = "REPEAT_ONE"
        assert app._get_next_index() == 1

    def test_repeat_one_manual_advances(self):
        app.state.play_mode = "REPEAT_ONE"
        assert app._get_next_index(manual=True) == 2

    def test_shuffle_excludes_current(self):
        app.state.play_mode = "SHUFFLE"
        for _ in range(20):
            assert app._get_next_index() in (0, 2)

    def test_shuffle_single_track(self):
        app.state.play_mode = "SHUFFLE"
        app.state.queue = [self._mk("solo")]
        app.state.current_index = 0
        assert app._get_next_index() is None

    def test_empty_queue(self):
        app.state.queue = []
        assert app._get_next_index() is None

    def test_manual_next_with_nothing_playing_starts_first(self):
        app.state.current_index = -1
        assert app._get_next_index(manual=True) == 0

    # prev
    def test_normal_prev(self):
        assert app._get_prev_index() == 0

    def test_normal_prev_at_start(self):
        app.state.current_index = 0
        assert app._get_prev_index() is None

    def test_repeat_all_prev_wraps(self):
        app.state.play_mode = "REPEAT_ALL"
        app.state.current_index = 0
        assert app._get_prev_index() == 2

    def test_shuffle_prev_is_sequential(self):
        app.state.play_mode = "SHUFFLE"
        assert app._get_prev_index() == 0

    def test_prev_with_nothing_playing(self):
        app.state.current_index = -1
        assert app._get_prev_index() is None


# ---------------------------------------------------------------------------
# Volume sync (restore on reconnect, adopt on new device, keepalive drift)
# ---------------------------------------------------------------------------

class TestIsLastDevice:
    def setup_method(self):
        app.state.last_device = ""
        app.state.last_device_url = ""

    teardown_method = setup_method

    def test_nothing_saved(self):
        assert app._is_last_device("Bulb", "http://192.168.1.49:49152/desc.xml") is False

    def test_name_match(self):
        app.state.last_device = "Bulb"
        assert app._is_last_device("Bulb", "http://10.0.0.9/x.xml") is True

    def test_exact_url_match(self):
        app.state.last_device_url = "http://192.168.1.49:49152/desc.xml"
        assert app._is_last_device("unknown", "http://192.168.1.49:49152/desc.xml") is True

    def test_ip_match_after_name_reset_and_new_path(self):
        app.state.last_device = "Bulb"
        app.state.last_device_url = "http://192.168.1.49:49152/desc.xml"
        assert app._is_last_device("unknown", "http://192.168.1.49:8080/upnp/dev.xml") is True

    def test_different_device(self):
        app.state.last_device = "Bulb"
        app.state.last_device_url = "http://192.168.1.49:49152/desc.xml"
        assert app._is_last_device("LG TV", "http://192.168.1.77:1234/desc.xml") is False

    def test_unparseable_url(self):
        app.state.last_device_url = "not a url"
        assert app._is_last_device("x", "also not") is False


class _VolumeTestBase:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.original_state_file = app.STATE_FILE
        app.STATE_FILE = Path(self.tmpdir) / "state.json"
        app.state.volume = 30
        app._volume_push_pending = False
        app._volume_set_ts = 0.0

    def teardown_method(self):
        app.STATE_FILE = self.original_state_file
        app.state.volume = 30
        app._volume_push_pending = False
        app._volume_set_ts = 0.0


class TestSyncDeviceVolume(_VolumeTestBase):
    @pytest.mark.asyncio
    async def test_equal_does_nothing(self):
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=30), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._sync_device_volume(restore=True)
        set_vol.assert_not_called()
        assert app.state.volume == 30

    @pytest.mark.asyncio
    async def test_restore_pushes_saved_volume(self):
        """Device came back at 100 after a reboot: push our saved 30."""
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=100), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._sync_device_volume(restore=True)
        set_vol.assert_awaited_once_with(30)
        assert app.state.volume == 30
        assert app._volume_push_pending is False

    @pytest.mark.asyncio
    async def test_restore_push_failure_sets_pending(self):
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=100), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock,
                          side_effect=RuntimeError("712")):
            await app._sync_device_volume(restore=True)
        assert app.state.volume == 30
        assert app._volume_push_pending is True

    @pytest.mark.asyncio
    async def test_adopt_takes_device_volume(self):
        """User picked a different device: its current level wins."""
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=12), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._sync_device_volume(restore=False)
        set_vol.assert_not_called()
        assert app.state.volume == 12

    @pytest.mark.asyncio
    async def test_get_failure_leaves_state(self):
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock,
                          side_effect=RuntimeError("no GetVolume")), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._sync_device_volume(restore=True)
        set_vol.assert_not_called()
        assert app.state.volume == 30
        assert app._volume_push_pending is False


class TestPollDeviceVolume(_VolumeTestBase):
    @pytest.mark.asyncio
    async def test_external_change_is_adopted_and_saved(self):
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=45), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._poll_device_volume()
        set_vol.assert_not_called()
        assert app.state.volume == 45
        assert json.loads(app.STATE_FILE.read_text())["volume"] == 45

    @pytest.mark.asyncio
    async def test_reset_to_max_is_pushed_back(self):
        """Device jumped to 100 (CY920 reboot signature): restore, don't adopt."""
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=100), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._poll_device_volume()
        set_vol.assert_awaited_once_with(30)
        assert app.state.volume == 30
        assert not app.STATE_FILE.exists()

    @pytest.mark.asyncio
    async def test_recovering_ping_treats_mismatch_as_reset(self):
        """Previous keepalive ping failed -> any mismatch is a reboot reset."""
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=60), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._poll_device_volume(recovering=True)
        set_vol.assert_awaited_once_with(30)
        assert app.state.volume == 30

    @pytest.mark.asyncio
    async def test_skipped_within_grace_after_local_set(self):
        app._volume_set_ts = time.monotonic()
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock, return_value=45) as get_vol:
            await app._poll_device_volume()
        get_vol.assert_not_called()
        assert app.state.volume == 30

    @pytest.mark.asyncio
    async def test_set_landing_during_get_is_not_clobbered(self):
        """A SetVolume that lands while GetVolume is in flight must win."""
        async def stale_get(*a, **k):
            app._volume_set_ts = time.monotonic()  # simulate /api/volume racing us
            app.state.volume = 60
            return 30
        with patch.object(app, "dlna_get_volume", side_effect=stale_get):
            await app._poll_device_volume()
        assert app.state.volume == 60

    @pytest.mark.asyncio
    async def test_pending_push_is_retried(self):
        app._volume_push_pending = True
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock) as get_vol, \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._poll_device_volume()
        get_vol.assert_not_called()
        set_vol.assert_awaited_once_with(30)
        assert app._volume_push_pending is False

    @pytest.mark.asyncio
    async def test_get_failure_is_silent(self):
        with patch.object(app, "dlna_get_volume", new_callable=AsyncMock,
                          side_effect=RuntimeError("boom")):
            await app._poll_device_volume()
        assert app.state.volume == 30


class TestPlayCurrentVolumeRetry(_VolumeTestBase):
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
    async def test_pending_volume_pushed_after_play(self):
        app._volume_push_pending = True
        with patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_set_uri", new_callable=AsyncMock), \
             patch.object(app, "dlna_play", new_callable=AsyncMock), \
             patch.object(app, "_preload_next", new_callable=AsyncMock), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._play_current()
        set_vol.assert_awaited_once_with(30)
        assert app._volume_push_pending is False

    @pytest.mark.asyncio
    async def test_no_push_when_not_pending(self):
        with patch.object(app, "_device_ready", return_value=True), \
             patch.object(app, "dlna_set_uri", new_callable=AsyncMock), \
             patch.object(app, "dlna_play", new_callable=AsyncMock), \
             patch.object(app, "_preload_next", new_callable=AsyncMock), \
             patch.object(app, "dlna_set_volume", new_callable=AsyncMock) as set_vol:
            await app._play_current()
        set_vol.assert_not_called()
