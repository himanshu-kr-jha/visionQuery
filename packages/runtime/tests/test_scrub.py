"""`scrub` removes the credentials a URL can carry from any text, wherever the URL sits in it."""

import contextlib
import json
import subprocess
import sys

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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password=hunter2", "password=***"),
        ("password: hunter2", "password: ***"),
        ("Password = hunter2 next", "Password = *** next"),
        ("pwd=abc", "pwd=***"),
        ("passwd=abc", "passwd=***"),
        ("secret=abc", "secret=***"),
        ("client_secret=abc", "client_secret=***"),
        ("token=abc", "token=***"),
        ("access_token=abc", "access_token=***"),
        ("api_key=abc", "api_key=***"),
        ("api-key: abc", "api-key: ***"),
        ("x-api-key: abc123", "x-api-key: ***"),
        ('{"password": "hunter2"}', '{"password": "***"}'),
        ("{'password': 'hunter2'}", "{'password': '***'}"),
        ('{"password":"hunter2","user":"a"}', '{"password":"***","user":"a"}'),
        ("Authorization: Basic YWRtaW46aHVudGVyMg==", "Authorization: Basic ***"),
        ("Authorization: Bearer abc.def-ghi", "Authorization: Bearer ***"),
        ('{"Authorization": "Bearer abc"}', '{"Authorization": "Bearer ***"}'),
        ("connect failed (password=hunter2) retrying", "connect failed (password=***) retrying"),
    ],
)
def test_plain_text_credentials_are_masked(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cameraPassword=hunter2", "cameraPassword=***"),
        ("camera_password=hunter2", "camera_password=***"),
        ("accessToken=hunter2", "accessToken=***"),
        ("csrftoken: hunter2", "csrftoken: ***"),
        ("private_key=hunter2", "private_key=***"),
        ("privateKey=hunter2", "privateKey=***"),
        ("passphrase=hunter2", "passphrase=***"),
        ("apiKey=hunter2", "apiKey=***"),
        ('{"cameraPassword": "hunter2"}', '{"cameraPassword": "***"}'),
    ],
)
def test_credential_words_are_masked_as_the_end_of_a_longer_name(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password="hunter2 two"', 'password="***"'),
        ("password='hunter2 two'", "password='***'"),
        ('{"password": "hunter2 two, and more"}', '{"password": "***"}'),
        ("{'password': 'hunter2 two'}", "{'password': '***'}"),
        ('password="hunter2 two" user=bob', 'password="***" user=bob'),
        ('{"password": "hun\\"ter2 two"}', '{"password": "***"}'),
        ('password="unterminated hunter2', 'password="***'),
    ],
)
def test_a_quoted_value_runs_to_its_closing_quote(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "token count=5",
        "password reset requested for user 7",
        "max_tokens=5",
        "token_count=5",
        "tokenizer=bpe",
        "passthrough=1",
        "the token was refreshed",
        "secretary=bob",
        "passwords: none",
        "mypassword",
    ],
)
def test_ordinary_words_are_left_alone(text: str) -> None:
    assert scrub(text) == text


#: Texts that would make a careless pattern quadratic: (a prefix, a unit repeated to size).
_ADVERSARIAL = {
    "letters": ("", "a"),
    "scheme_then_letters": ("rtsp://", "a"),
    "many_separators": ("", "a://"),
    "bare_separators": ("", "://"),
    "many_ats": ("rtsp://", "@"),
    "ats_after_words": ("", "a@"),
    "many_equals": ("", "="),
    "keyword_equals": ("", "password="),
    "keyword_spaces": ("password", " "),
    "keyword_then_quotes": ("", 'password="'),
    "keyword_then_escapes": ("", 'password="\\'),
    "suffix_words": ("", "camera_token_password"),
    "query_marks": ("", "?key"),
    "query_equals": ("", "?key="),
    "authorization": ("", "Authorization: "),
    "authorization_spaces": ("Authorization:", " "),
    "scheme_chars": ("", "a.b-c+d"),
}

_SMALL, _LARGE = 20_000, 200_000

#: Times `scrub` on a small and a large size of each adversarial text, best of three each,
#: writing one line per text as soon as it is timed, so a text that hangs is known by name.
_TIMING = f"""
import json, sys, time
from visionquery.runtime import scrub

def best(prefix, unit, size):
    text = prefix + unit * (size // len(unit))
    times = []
    for _ in range(3):
        started = time.perf_counter()
        scrub(text)
        times.append(time.perf_counter() - started)
    return min(times)

for name, (prefix, unit) in json.loads(sys.argv[1]).items():
    timed = [best(prefix, unit, {_SMALL}), best(prefix, unit, {_LARGE})]
    print(json.dumps([name, timed]), flush=True)
"""

#: Linear work grows 10x from the small size to the large and quadratic work 100x; this
#: sits between.
_GROWTH_LIMIT = 40
#: Below this a small run is too quick to compare against.
_FLOOR_SECONDS = 0.001
#: A quadratic scrub of the large size would run for many minutes; it must fail, not hang.
#: The texts that were timed before it are still judged.
_HARD_TIMEOUT_SECONDS = 60


@pytest.fixture(scope="module")
def timings(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[float, float]]:
    """How long each adversarial text took at each size, timed in one child process.

    A child process, so that a quadratic scrub can be stopped; one, so that the interpreter
    starts once rather than once per text. A text missing from the result did not finish.
    """
    output = tmp_path_factory.mktemp("scrub") / "timings.jsonl"
    with output.open("w") as stream, contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", _TIMING, json.dumps(_ADVERSARIAL)],
            stdout=stream,
            timeout=_HARD_TIMEOUT_SECONDS,
            check=True,
        )
    timed: dict[str, tuple[float, float]] = {}
    for line in output.read_text().splitlines():
        name, (small, large) = json.loads(line)
        timed[name] = (small, large)
    return timed


@pytest.mark.parametrize("name", list(_ADVERSARIAL))
def test_scrubbing_adversarial_text_grows_linearly_with_its_size(
    timings: dict[str, tuple[float, float]], name: str
) -> None:
    if name not in timings:
        pytest.fail(f"scrubbing {name} did not finish within {_HARD_TIMEOUT_SECONDS} s")
    small, large = timings[name]
    assert large < max(small, _FLOOR_SECONDS) * _GROWTH_LIMIT, (small, large)


def test_credentials_are_still_removed_inside_large_inputs() -> None:
    filler = "lorem ipsum " * 50_000
    text = f"{filler}rtsp://admin:hunter2@cam/x password=hunter2 {filler}"
    result = scrub(text)
    assert "hunter2" not in result
    assert "rtsp://***@cam/x password=***" in result
