"""Logs are one JSON object per line, with the service and level, and never hold a credential."""

import io
import json
import logging
import sys
import threading
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from visionquery.runtime import configure_logging

URL = "rtsp://admin:hunter2@10.0.0.7:554/stream1"
SCRUBBED = "rtsp://***@10.0.0.7:554/stream1"
SECRETS = ("hunter2", "admin:")


pytestmark = pytest.mark.usefixtures("restore_logging")


@pytest.fixture
def output() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("recorder", "INFO", stream=stream)
    return stream


def _lines(output: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _assert_clean(output: io.StringIO) -> None:
    text = output.getvalue()
    for secret in SECRETS:
        assert secret not in text, text


def test_each_record_is_one_json_line_with_service_and_level(output: io.StringIO) -> None:
    logging.getLogger("visionquery.recorder.segments").info("sealed %d segments", 3)
    logging.getLogger("visionquery.recorder").warning("camera %s is late", "gate-2")
    first, second = _lines(output)
    assert first["service"] == "recorder"
    assert first["level"] == "INFO"
    assert first["logger"] == "visionquery.recorder.segments"
    assert first["message"] == "sealed 3 segments"
    assert second["level"] == "WARNING"
    assert second["message"] == "camera gate-2 is late"


def test_each_record_carries_its_time_in_utc(output: io.StringIO) -> None:
    logging.getLogger("x").info("now")
    (line,) = _lines(output)
    time = datetime.fromisoformat(line["time"])
    assert time.utcoffset() == timedelta(0)


def test_records_below_the_level_are_dropped() -> None:
    stream = io.StringIO()
    configure_logging("recorder", "WARNING", stream=stream)
    logging.getLogger("x").info("quiet")
    logging.getLogger("x").error("loud")
    assert [line["message"] for line in _lines(stream)] == ["loud"]


def test_configuring_twice_does_not_write_twice() -> None:
    first, second = io.StringIO(), io.StringIO()
    configure_logging("recorder", "INFO", stream=first)
    configure_logging("recorder", "INFO", stream=second)
    logging.getLogger("x").info("once")
    assert first.getvalue() == ""
    assert len(_lines(second)) == 1


def test_structured_fields_become_json_fields(output: io.StringIO) -> None:
    logging.getLogger("x").info(
        "sealed", extra={"camera": "gate-2", "segment": 41, "lag": 0.25, "path": Path("/f/41.mp4")}
    )
    (line,) = _lines(output)
    assert line["camera"] == "gate-2"
    assert line["segment"] == 41
    assert line["lag"] == 0.25
    assert line["path"] == "/f/41.mp4"


def test_structured_fields_cannot_hide_the_service_or_level(output: io.StringIO) -> None:
    logging.getLogger("x").info("hi", extra={"service": "someone-else", "level": "DEBUG"})
    (line,) = _lines(output)
    assert line["service"] == "recorder"
    assert line["level"] == "INFO"


def test_credentials_in_the_message_are_scrubbed(output: io.StringIO) -> None:
    log = logging.getLogger("x")
    log.info("opening %s", URL)
    log.info(f"opening {URL}")
    log.info(URL)
    log.info("%(url)s", {"url": URL})
    assert [line["message"] for line in _lines(output)] == [
        f"opening {SCRUBBED}",
        f"opening {SCRUBBED}",
        SCRUBBED,
        SCRUBBED,
    ]
    _assert_clean(output)


def test_credentials_in_exception_text_are_scrubbed(output: io.StringIO) -> None:
    try:
        try:
            raise OSError(f"cannot reach {URL}")
        except OSError as cause:
            error = ConnectionError(f"giving up on {URL}")
            error.add_note(f"last tried {URL}")
            raise error from cause
    except ConnectionError:
        logging.getLogger("x").exception("camera lost")
    (line,) = _lines(output)
    assert line["message"] == "camera lost"
    assert f"giving up on {SCRUBBED}" in line["exception"]
    assert f"cannot reach {SCRUBBED}" in line["exception"]
    assert f"last tried {SCRUBBED}" in line["exception"]
    _assert_clean(output)


def test_credentials_in_structured_fields_are_scrubbed(output: io.StringIO) -> None:
    class Camera:
        def __str__(self) -> str:
            return f"Camera({URL})"

    logging.getLogger("x").info(
        "added",
        extra={
            "url": URL,
            "cameras": [{"url": URL, "fallback": (URL,)}],
            "by_url": {URL: 1},
            "camera": Camera(),
            "error": ValueError(URL),
        },
    )
    (line,) = _lines(output)
    assert line["url"] == SCRUBBED
    assert line["cameras"] == [{"url": SCRUBBED, "fallback": [SCRUBBED]}]
    assert line["by_url"] == {SCRUBBED: 1}
    assert line["camera"] == f"Camera({SCRUBBED})"
    assert line["error"] == SCRUBBED
    _assert_clean(output)


def test_credentials_in_stack_info_are_scrubbed(output: io.StringIO) -> None:
    # The stack shows each frame's source line, and this one holds a credential.
    logging.getLogger("x").info("here", stack_info=True)  # rtsp://admin:hunter2@h/
    (line,) = _lines(output)
    assert "rtsp://***@h/" in line["stack"]
    _assert_clean(output)


def test_a_handler_the_runtime_did_not_install_never_sees_a_credential(
    output: io.StringIO,
) -> None:
    # A library that installs its own handler (as uvicorn does) and stops propagation
    # still logs through records the runtime made.
    foreign = io.StringIO()
    handler = logging.StreamHandler(foreign)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    library = logging.getLogger("some.library")
    library.addHandler(handler)
    library.propagate = False
    try:
        try:
            raise ConnectionError(URL)
        except ConnectionError:
            library.exception("failed %s", URL)
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert f"failed {SCRUBBED}" in foreign.getvalue()
    assert f"ConnectionError: {SCRUBBED}" in foreign.getvalue()
    for secret in SECRETS:
        assert secret not in foreign.getvalue()


def test_a_record_whose_arguments_do_not_fit_is_still_logged_and_scrubbed(
    output: io.StringIO,
) -> None:
    logging.getLogger("x").info("%s and %s", URL)
    (line,) = _lines(output)
    assert SCRUBBED in line["message"]
    _assert_clean(output)


def test_an_uncaught_exception_is_logged_as_json_and_scrubbed(output: io.StringIO) -> None:
    try:
        raise RuntimeError(f"stream died: {URL}")
    except RuntimeError as error:
        sys.excepthook(type(error), error, error.__traceback__)
    (line,) = _lines(output)
    assert line["level"] == "CRITICAL"
    assert f"RuntimeError: stream died: {SCRUBBED}" in line["exception"]
    _assert_clean(output)


def test_an_uncaught_exception_in_a_thread_is_logged_as_json_and_scrubbed(
    output: io.StringIO,
) -> None:
    def die() -> None:
        raise RuntimeError(f"stream died: {URL}")

    thread = threading.Thread(target=die, name="camera-gate-2")
    thread.start()
    thread.join()
    (line,) = _lines(output)
    assert line["level"] == "CRITICAL"
    assert "camera-gate-2" in line["message"]
    assert f"RuntimeError: stream died: {SCRUBBED}" in line["exception"]
    _assert_clean(output)


def test_warnings_are_logged_as_json_and_scrubbed(output: io.StringIO) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn(f"slow camera {URL}", stacklevel=1)
    (line,) = _lines(output)
    assert line["level"] == "WARNING"
    assert line["logger"] == "py.warnings"
    _assert_clean(output)


def test_a_lone_surrogate_still_comes_out_as_valid_json(output: io.StringIO) -> None:
    logging.getLogger("x").info("bad \udcff byte", extra={"field": "\ud800"})
    text = output.getvalue()
    text.encode("utf-8")
    (line,) = _lines(output)
    assert line["message"] == "bad \udcff byte"
    assert line["field"] == "\ud800"


class _Unprintable:
    def __repr__(self) -> str:
        raise RuntimeError("no repr")

    def __str__(self) -> str:
        raise RuntimeError("no str")


def test_an_argument_that_cannot_be_printed_never_raises_into_the_caller(
    output: io.StringIO,
) -> None:
    logging.getLogger("x").info("value is %s", _Unprintable())
    logging.getLogger("x").info(_Unprintable())
    logging.getLogger("x").info("a %s b %s", _Unprintable(), URL)
    first, second, third = _lines(output)
    for line in (first, second, third):
        assert line["level"] == "INFO"
        assert "message" in line
    _assert_clean(output)


def test_a_structured_field_that_cannot_be_printed_never_raises_into_the_caller(
    output: io.StringIO,
) -> None:
    logging.getLogger("x").info("hello", extra={"thing": _Unprintable()})
    (line,) = _lines(output)
    assert line["message"] == "hello"


class _AccessFormatter(logging.Formatter):
    """Stands in for uvicorn's `AccessFormatter`, which unpacks `record.args` in `format`."""

    def format(self, record: logging.LogRecord) -> str:
        client, method, path, http_version, status = cast(
            tuple[str, str, str, str, int], record.args
        )
        return f'{client} - "{method} {path} HTTP/{http_version}" {status}'


def _foreign_access_log(
    formatter: logging.Formatter,
) -> tuple[io.StringIO, logging.Logger, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    library = logging.getLogger("uvicorn.access")
    library.addHandler(handler)
    library.propagate = False
    return stream, library, handler


def test_a_record_with_nothing_to_scrub_reaches_foreign_formatters_untouched(
    output: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    stream, library, handler = _foreign_access_log(_AccessFormatter())
    try:
        library.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/metrics", "1.1", 200)
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert stream.getvalue() == '127.0.0.1:5000 - "GET /metrics HTTP/1.1" 200\n'
    assert capsys.readouterr().err == ""


def test_the_runtime_keeps_the_arguments_of_a_record_it_did_not_change(
    output: io.StringIO,
) -> None:
    record = logging.getLogger("x").makeRecord("x", logging.INFO, "f", 1, "%s %d", ("a", 1), None)
    assert record.args == ("a", 1)
    assert record.msg == "%s %d"


def test_an_unprintable_argument_keeps_the_message_text(output: io.StringIO) -> None:
    logging.getLogger("x").info("camera %s lost", _Unprintable())
    logging.getLogger("x").info("camera %s lost via %s", _Unprintable(), URL)
    first, second = _lines(output)
    assert first["message"].startswith("camera %s lost")
    assert second["message"].startswith("camera %s lost via %s")
    _assert_clean(output)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "camera_password",
        "cameraPassword",
        "CameraPassword",
        "PASSWORD",
        "accessToken",
        "access_token",
        "api_key",
        "apiKey",
        "x-api-key",
        "private_key",
        "passphrase",
        "client_secret",
        "Authorization",
    ],
)
def test_a_value_under_a_credential_named_key_is_masked(output: io.StringIO, key: str) -> None:
    logging.getLogger("x").info("added", extra={key: "hunter2"})
    (line,) = _lines(output)
    assert line[key] == "***"
    _assert_clean(output)


def test_credential_named_keys_are_masked_at_any_depth(output: io.StringIO) -> None:
    logging.getLogger("x").info(
        "added",
        extra={
            "camera": {"name": "gate-2", "auth": {"password": "hunter2"}},
            "cameras": [{"id": 1, "cameraPassword": "hunter2"}, ({"token": "hunter2"},)],
            "settings": {"secret": {"a": "hunter2", "b": ["hunter2"]}},
        },
    )
    (line,) = _lines(output)
    assert line["camera"] == {"name": "gate-2", "auth": {"password": "***"}}
    assert line["cameras"] == [{"id": 1, "cameraPassword": "***"}, [{"token": "***"}]]
    assert line["settings"] == {"secret": "***"}
    _assert_clean(output)


def test_keys_that_only_look_like_credentials_are_left_alone(output: io.StringIO) -> None:
    logging.getLogger("x").info(
        "counted",
        extra={"token_count": 5, "max_tokens": 7, "tokenizer": "bpe", "secretary": "bob"},
    )
    (line,) = _lines(output)
    assert line["token_count"] == 5
    assert line["max_tokens"] == 7
    assert line["tokenizer"] == "bpe"
    assert line["secretary"] == "bob"


def _foreign_logger(name: str, fmt: str) -> tuple[io.StringIO, logging.Logger, logging.Handler]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(fmt))
    library = logging.getLogger(name)
    library.addHandler(handler)
    library.propagate = False
    return stream, library, handler


def test_extra_fields_never_reach_a_foreign_formatter_with_a_credential(
    output: io.StringIO,
) -> None:
    stream, library, handler = _foreign_logger("some.library", "%(message)s %(url)s %(password)s")
    try:
        library.info("opening", extra={"url": URL, "password": "hunter2"})
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert stream.getvalue() == f"opening {SCRUBBED} ***\n"


def test_extra_fields_of_a_logger_created_before_configuring_are_scrubbed() -> None:
    early = logging.getLogger("created.before")
    stream, library, handler = _foreign_logger("created.before", "%(url)s")
    configure_logging("recorder", "INFO", stream=io.StringIO())
    try:
        early.info("x", extra={"url": URL})
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert stream.getvalue() == f"{SCRUBBED}\n"


def test_extra_fields_keep_their_type_when_there_is_nothing_to_scrub(
    output: io.StringIO,
) -> None:
    stream, library, handler = _foreign_logger("some.library", "%(count)d %(ratio).1f")
    try:
        library.info("x", extra={"count": 3, "ratio": 0.5})
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert stream.getvalue() == "3 0.5\n"


def test_configuring_twice_wraps_make_record_once() -> None:
    standard = logging.Logger.makeRecord
    configure_logging("a", "INFO", stream=io.StringIO())
    wrapped = logging.Logger.makeRecord
    configure_logging("a", "INFO", stream=io.StringIO())
    assert wrapped is not standard
    assert logging.Logger.makeRecord is wrapped
    stream, library, handler = _foreign_logger("some.library", "%(url)s")
    try:
        library.info("x", extra={"url": URL})
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert stream.getvalue() == f"{SCRUBBED}\n"


def test_a_credential_argument_is_scrubbed_and_the_arguments_keep_their_shape(
    output: io.StringIO, capsys: pytest.CaptureFixture[str]
) -> None:
    stream, library, handler = _foreign_access_log(_AccessFormatter())
    try:
        library.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", URL, "1.1", 200)
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert stream.getvalue() == f'127.0.0.1:5000 - "GET {SCRUBBED} HTTP/1.1" 200\n'
    assert capsys.readouterr().err == ""


def test_mapping_arguments_are_scrubbed_per_key(output: io.StringIO) -> None:
    stream, library, handler = _foreign_logger("some.library", "%(message)s")
    try:
        library.info("%(url)s %(n)d", {"url": URL, "n": 2})
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert stream.getvalue() == f"{SCRUBBED} 2\n"


def test_a_credential_split_between_message_and_argument_is_never_leaked(
    output: io.StringIO,
) -> None:
    stream, library, handler = _foreign_logger("some.library", "%(message)s")
    try:
        library.info("login password=%s now", "hunter2")
        library.info("rtsp://admin:%s@10.0.0.7/s", "hunter2")
    finally:
        library.removeHandler(handler)
        library.propagate = True
    assert "hunter2" not in stream.getvalue()
    assert len(stream.getvalue().splitlines()) == 2
