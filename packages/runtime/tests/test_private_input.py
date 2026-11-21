"""A URL with a credential handed to ffmpeg in a file only its owner can read (ADR 0019)."""

import os
import stat
from pathlib import Path

import pytest
from visionquery.runtime.private_input import FFMPEG_PROTOCOLS, private_ffmpeg_input

URL = "rtsp://reader:Sup3r-S3cret@mediamtx:8554/dock"


def test_the_arguments_name_a_file_and_never_the_url(tmp_path: Path) -> None:
    with private_ffmpeg_input(URL, {"rtsp_transport": "tcp"}, directory=tmp_path) as argv:
        joined = " ".join(argv)

        assert argv[:4] == ["-f", "concat", "-safe", "0"]
        assert argv[argv.index("-protocol_whitelist") + 1] == FFMPEG_PROTOCOLS
        assert "Sup3r-S3cret" not in joined
        assert "reader" not in joined
        assert "mediamtx" not in joined
        assert "rtsp_transport" not in joined


def test_the_file_holds_the_url_and_the_options_for_ffmpeg_to_read(tmp_path: Path) -> None:
    with private_ffmpeg_input(
        URL, {"rtsp_transport": "tcp", "timeout": "10000000"}, directory=tmp_path
    ) as argv:
        listed = Path(argv[argv.index("-i") + 1]).read_text(encoding="utf-8")

    assert listed.splitlines() == [
        "ffconcat version 1.0",
        f"file '{URL}'",
        "option rtsp_transport tcp",
        "option timeout 10000000",
    ]


def test_only_its_owner_can_read_the_file_or_look_in_its_directory(tmp_path: Path) -> None:
    with private_ffmpeg_input(URL, directory=tmp_path) as argv:
        path = Path(argv[argv.index("-i") + 1])

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert path.parent.parent == tmp_path


def test_nothing_is_left_when_the_block_ends_normally_or_not(tmp_path: Path) -> None:
    with private_ffmpeg_input(URL, directory=tmp_path):
        pass
    with pytest.raises(RuntimeError), private_ffmpeg_input(URL, directory=tmp_path):
        raise RuntimeError("ffmpeg could not start")

    assert list(tmp_path.iterdir()) == []


def test_every_call_has_a_file_of_its_own(tmp_path: Path) -> None:
    with (
        private_ffmpeg_input(URL, directory=tmp_path) as one,
        private_ffmpeg_input(URL, directory=tmp_path) as two,
    ):
        assert one[one.index("-i") + 1] != two[two.index("-i") + 1]


def test_a_quote_in_the_url_cannot_end_its_token_early(tmp_path: Path) -> None:
    with private_ffmpeg_input("rtsp://u:p'w@h/x'y", directory=tmp_path) as argv:
        listed = Path(argv[argv.index("-i") + 1]).read_text(encoding="utf-8")

    assert listed.splitlines()[1] == "file 'rtsp://u:p'\\''w@h/x'\\''y'"


@pytest.mark.parametrize("text", ["rtsp://h/x\nfile '/etc/passwd'", "rtsp://h/x\r", "rtsp://h/\0"])
def test_a_url_that_could_add_a_line_is_refused_without_saying_it(
    tmp_path: Path, text: str
) -> None:
    with (
        pytest.raises(ValueError, match="control characters") as refused,
        private_ffmpeg_input(text, directory=tmp_path),
    ):
        pass

    assert "passwd" not in str(refused.value)
    assert list(tmp_path.iterdir()) == [], "refused before anything is written"


@pytest.mark.parametrize(
    ("name", "value"),
    [("rtsp_transport", "tcp\noption x y"), ("a b", "c"), ("timeout", "1 2"), ("x", "'y'")],
)
def test_an_option_that_would_need_quoting_is_refused(
    tmp_path: Path, name: str, value: str
) -> None:
    with (
        pytest.raises(ValueError, match="cannot be given"),
        private_ffmpeg_input(URL, {name: value}, directory=tmp_path),
    ):
        pass

    assert list(tmp_path.iterdir()) == []


def test_the_default_directory_is_the_system_temporary_one() -> None:
    with private_ffmpeg_input(URL) as argv:
        path = Path(argv[argv.index("-i") + 1])

        assert path.parent.parent == Path(os.environ.get("TMPDIR", "/tmp"))
