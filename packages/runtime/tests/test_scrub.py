"""`scrub` removes the credentials a URL can carry from any text, wherever the URL sits in it."""

import pytest
from visionquery.runtime import scrub


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("rtsp://admin:hunter2@10.0.0.7:554/stream1", "rtsp://***@10.0.0.7:554/stream1"),
        ("rtsps://admin:hunter2@cam.local/live", "rtsps://***@cam.local/live"),
        ("http://admin:hunter2@cam.local/snapshot.jpg", "http://***@cam.local/snapshot.jpg"),
        ("https://admin:hunter2@cam.local/onvif", "https://***@cam.local/onvif"),
        ("RTSP://Admin:Hunter2@CAM.local/x", "RTSP://***@CAM.local/x"),
        # A username alone is half a credential.
        ("rtsp://admin@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://:hunter2@cam.local/x", "rtsp://***@cam.local/x"),
        # Cameras' passwords are typed by people, unencoded, with whatever characters they like.
        ("rtsp://admin:p@ss@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://admin:pa:ss@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://admin:pa/ss@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://admin:pa?ss#@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://admin:pa'ss\"@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://admin:p%40ss@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://admin:pässwört@cam.local/x", "rtsp://***@cam.local/x"),
        ("rtsp://admin:hunter2@[fe80::1]:554/x", "rtsp://***@[fe80::1]:554/x"),
        # Some cameras take credentials as query parameters instead.
        (
            "http://cam.local/cgi?user=admin&password=hunter2&channel=1",
            "http://cam.local/cgi?user=***&password=***&channel=1",
        ),
        ("http://cam.local/cgi?usr=admin&pwd=hunter2", "http://cam.local/cgi?usr=***&pwd=***"),
        ("http://cam.local/cgi?PassWord=hunter2#top", "http://cam.local/cgi?PassWord=***#top"),
        ("http://cam.local/x?token=abc;key=def", "http://cam.local/x?token=***;key=***"),
    ],
)
def test_credentials_in_a_url_are_removed(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "ffmpeg: rtsp://admin:hunter2@10.0.0.7/s1: 401 Unauthorized",
            "ffmpeg: rtsp://***@10.0.0.7/s1: 401 Unauthorized",
        ),
        (
            "ConnectionError('rtsp://admin:hunter2@10.0.0.7/s1')",
            "ConnectionError('rtsp://***@10.0.0.7/s1')",
        ),
        (
            '{"url": "rtsp://admin:hunter2@10.0.0.7/s1"}',
            '{"url": "rtsp://***@10.0.0.7/s1"}',
        ),
        ("<rtsp://admin:hunter2@10.0.0.7/s1>", "<rtsp://***@10.0.0.7/s1>"),
        ("url=rtsp://admin:hunter2@10.0.0.7/s1", "url=rtsp://***@10.0.0.7/s1"),
        (
            "a rtsp://u1:p1@h1/s and http://u2:p2@h2/s\nthen rtsps://u3:p3@h3/s",
            "a rtsp://***@h1/s and http://***@h2/s\nthen rtsps://***@h3/s",
        ),
    ],
)
def test_every_url_in_running_text_is_scrubbed(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "rtsp://10.0.0.7:554/stream1",
        "http://cam.local/cgi?channel=1&subtype=0",
        "[rtsp @ 0x55d0c8a0] method DESCRIBE failed: 401 Unauthorized",
        "mail himanshu@example.com about camera 3",
        "ratio 3:4 at 12:00",
        "",
    ],
)
def test_text_without_credentials_is_left_alone(text: str) -> None:
    assert scrub(text) == text


def test_two_urls_run_together_lose_more_than_their_credentials_but_never_leak() -> None:
    # Within one run of non-space text, everything up to the last `@` is treated as userinfo:
    # a path that happens to hold an `@` is lost, a password that holds one is never kept.
    scrubbed = scrub("rtsp://u1:secret1@h1/s,rtsp://u2:secret2@h2/s")
    assert "secret1" not in scrubbed
    assert "secret2" not in scrubbed
