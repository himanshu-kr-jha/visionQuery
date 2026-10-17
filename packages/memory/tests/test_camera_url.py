"""A pulled camera's URL: accepted only when strictly valid, and shown only without credentials.

No database is needed: these are the rules a URL meets before the registry stores it.
"""

import copy
import pickle
import traceback

import pytest
from pydantic import TypeAdapter
from visionquery.memory.cameras import CameraUrl, InvalidCameraError

SECRET = "s3cr3t-Pa55"


@pytest.mark.parametrize(
    ("url", "redacted"),
    [
        ("rtsp://10.0.0.7", "rtsp://10.0.0.7"),
        ("rtsp://gate-2.local", "rtsp://gate-2.local"),
        # The path and the query are secret, like the password: UniFi Protect's token is a path.
        ("rtsp://10.0.0.7/stream1", "rtsp://10.0.0.7/***"),
        ("rtsp://10.0.0.7:554/Streaming/Channels/101", "rtsp://10.0.0.7:554/***"),
        (f"rtsp://admin:{SECRET}@10.0.0.7:554/stream1", "rtsp://***@10.0.0.7:554/***"),
        ("rtsp://admin@cam-7.example.internal/live", "rtsp://***@cam-7.example.internal/***"),
        (f"rtsps://viewer:{SECRET}@[fd00::7]:322/main", "rtsps://***@[fd00::7]:322/***"),
        ("rtsp://10.0.0.7/cam/realmonitor?channel=1&subtype=0", "rtsp://10.0.0.7/***"),
        ("rtsps://192.168.1.1:7441/Xk9aQ2bLm4ZtTq1R?enableSrtp", "rtsps://192.168.1.1:7441/***"),
        ("rtsp://10.0.0.7?x", "rtsp://10.0.0.7/***"),
        # A password may hold any character, percent-encoded where the URL needs it.
        ("rtsp://admin:p%40ss%2Fw%3Ard!$@10.0.0.7/s", "rtsp://***@10.0.0.7/***"),
        ("rtsp://admin:a:b@10.0.0.7/s", "rtsp://***@10.0.0.7/***"),
        # A password without a user, and underscores in host names.
        (f"rtsp://:{SECRET}@10.0.0.7/s", "rtsp://***@10.0.0.7/***"),
        ("rtsp://cam_1.lan/s", "rtsp://cam_1.lan/***"),
        # The scheme and the host are case-insensitive, and are kept in lower case.
        ("RTSP://Admin:PW@CAM.Example/Live", "rtsp://***@cam.example/***"),
    ],
)  # fmt: skip
def test_a_valid_url_is_kept_and_shown_without_its_secrets(url: str, redacted: str) -> None:
    camera_url = CameraUrl(url)
    assert camera_url.redacted == redacted
    assert str(camera_url) == redacted
    assert repr(camera_url) == f"CameraUrl({redacted!r})"


@pytest.mark.parametrize(
    "url",
    [
        # Credentials in the path or the query are accepted, and are as secret as a password.
        "rtsp://192.168.1.10:554/user=admin&password=x&channel=1&stream=0.sdp?",
        "rtsps://192.168.1.1:7441/Xk9aQ2bLm4ZtTq1R?enableSrtp",
        "rtsp://10.0.0.7/s?sig=abc",
        "rtsp://10.0.0.7/s?token=abc",
        "rtsp://10.0.0.7/s?key=abc",
        "rtsp://10.0.0.7/s?auth=0",
        "rtsp://10.0.0.7/s?user=admin&password=abc",
        "rtsp://10.0.0.7/password=abc/s",
    ],
)
def test_a_url_with_secrets_in_its_path_or_query_is_accepted_and_hides_them(url: str) -> None:
    camera_url = CameraUrl(url)
    assert camera_url.get_secret_value() == url
    assert camera_url.redacted == f"{url.split('://')[0]}://{url.split('://')[1].split('/')[0]}/***"
    for shown in (str(camera_url), repr(camera_url), f"{[camera_url]}"):
        assert "abc" not in shown
        assert "password" not in shown
        assert "Xk9a" not in shown
        assert "admin" not in shown


def test_the_full_url_is_given_only_when_asked_for() -> None:
    url = CameraUrl(f"rtsp://admin:{SECRET}@10.0.0.7/stream1")
    assert url.get_secret_value() == f"rtsp://admin:{SECRET}@10.0.0.7/stream1"
    assert url.has_credentials
    assert not CameraUrl("rtsp://10.0.0.7/stream1").has_credentials
    shown = [str(url), repr(url), f"{url}", f"{url!r}", f"{[url]}", f"{ {'url': url} }"]
    assert all(SECRET not in text for text in shown)


def test_urls_are_equal_when_their_full_text_is() -> None:
    a = CameraUrl("rtsp://admin:one@10.0.0.7/s")
    assert a == CameraUrl("RTSP://admin:one@10.0.0.7/s")
    assert a != CameraUrl("rtsp://admin:two@10.0.0.7/s")
    assert a != CameraUrl("rtsp://admin:one@10.0.0.7/t")
    assert len({a, CameraUrl("rtsp://admin:one@10.0.0.7/s")}) == 1


def test_a_url_cannot_be_pickled_because_that_would_carry_the_full_url() -> None:
    url = CameraUrl(f"rtsp://admin:{SECRET}@10.0.0.7/s?token=abc")
    with pytest.raises(TypeError) as raised:
        pickle.dumps(url)
    assert SECRET not in str(raised.value)
    assert "abc" not in str(raised.value)
    with pytest.raises(TypeError):
        copy.copy(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "10.0.0.7/stream1",  # no scheme: nothing could scrub its credentials
        f"admin:{SECRET}@10.0.0.7/stream1",
        "http://10.0.0.7/stream1",
        "rtmp://10.0.0.7/live",
        "rtsp+http://10.0.0.7/s",
        "rtsp:/10.0.0.7/s",
        "rtsp://",
        "rtsp:///stream1",
        "rtsp://10.0.0.7:0/s",
        "rtsp://10.0.0.7:65536/s",
        "rtsp://10.0.0.7:port/s",
        "rtsp://10.0.0.7:/s",
        "rtsp://999.0.0.7/s",
        "rtsp://10.0.0/s",
        "rtsp://-cam.example/s",
        "rtsp://cam..example/s",
        "rtsp://_/s",  # an underscore only inside a label
        "rtsp://_cam/s",
        "rtsp://cam_/s",
        "rtsp://cam._x.lan/s",
        "rtsp://[fd00::7/s",
        "rtsp://[not-an-address]/s",
        "rtsp://[fe80::1%25eth0]/s",
        "rtsp://kamera-ü.example/s",  # write it as its xn-- name
        "rtsp://10.0.0.7/stream 1",
        "rtsp://10.0.0.7/stream\t1",
        "rtsp://10.0.0.7/stream1\n",
        " rtsp://10.0.0.7/stream1",
        "rtsp://10.0.0.7/stream1#frag",
        "rtsp://10.0.0.7/a%zz",
        "rtsp://10.0.0.7/café",
        # An unescaped @, / or # in a password: it must be percent-encoded.
        f"rtsp://admin:{SECRET}@x@10.0.0.7/s",
        f"rtsp://admin:{SECRET}/x@10.0.0.7/s",
        f"rtsp://admin:{SECRET}#x@10.0.0.7/s",
        f"rtsp://admin:{SECRET}?x@10.0.0.7/s",
        f"rtsp://admin:{SECRET} x@10.0.0.7/s",
        "rtsp://@10.0.0.7/s",
        "rtsp://10.0.0.7/s@t",
        "rtsp://10.0.0.7/" + "s" * 2048,
    ],
)
def test_an_invalid_url_is_refused_without_quoting_it(url: str) -> None:
    with pytest.raises(InvalidCameraError) as raised:
        CameraUrl(url)
    error = raised.value
    assert isinstance(error, ValueError)
    # Nothing it was raised from, not even as context, for a traceback to print.
    assert error.__cause__ is None
    assert error.__context__ is None
    text = "".join(traceback.format_exception(error))
    assert SECRET not in text
    assert "10.0.0.7" not in text


def test_a_url_that_is_not_text_is_refused() -> None:
    with pytest.raises(InvalidCameraError):
        CameraUrl(b"rtsp://10.0.0.7/s")  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]


def test_serialised_as_the_api_will_serialise_it_a_url_is_its_redacted_form() -> None:
    url = CameraUrl(f"rtsp://admin:{SECRET}@10.0.0.7/s")
    adapter = TypeAdapter(CameraUrl)
    assert adapter.dump_python(url) == "rtsp://***@10.0.0.7/***"
    assert adapter.dump_python(url, mode="json") == "rtsp://***@10.0.0.7/***"
    assert adapter.dump_json(url) == b'"rtsp://***@10.0.0.7/***"'
    assert adapter.json_schema(mode="serialization") == {"type": "string"}
