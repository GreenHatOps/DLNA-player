"""Unit tests for Bulb Voice — pure logic, no DLNA device required."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

# Import the module under test
import app


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
