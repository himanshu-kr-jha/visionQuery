"""Every camera in the registry, recorded by one process, each on its own.

`Fleet.reconcile` is given the cameras not removed: a camera that appears is started and one
that is gone is stopped (its Segment finished `stopped`), and no other camera notices either.
Each camera's Footage goes in a directory named for its id, which is never reused, under the
Footage directory, so a removed camera's Footage stays apart and still says which camera it was.

**Every camera is a task supervised alone.** A camera whose stream goes away is not a failure:
the camera's own loop reconnects with backoff (`camera.record_camera`). A camera whose recording
fails outright (its directory cannot be written, say) is logged as an ERROR with the traceback
and started again after a wait that doubles to a minute, and every other camera keeps
recording. What one process saves is the interpreter and its libraries, loaded once rather than
once per camera; what it gives up is that the process dying stops every camera at once, and
each then recovers what it was writing when the process is started again.

Adapted from phyOS's Photon fleet (`adapters.photon.fleet`), reading the camera registry
instead of a directory of cameras.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from visionquery.memory.cameras import Camera
from visionquery.recorder.camera import Backoff, Timing, record_camera
from visionquery.recorder.ffmpeg import Source

__all__ = ["Fleet"]

_log = logging.getLogger(__name__)

#: The first wait before a camera whose recording failed is started again, doubling to the last.
_FIRST_WAIT = 1.0
_LAST_WAIT = 60.0
#: A camera that recorded this long before failing starts again from the first wait.
_STEADY = 600.0


@dataclass
class _Recorded:
    camera: Camera
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class Fleet:
    """The cameras being recorded into `footage`, each read from `source(camera)`."""

    def __init__(self, footage: Path, source: Callable[[Camera], Source], timing: Timing) -> None:
        self._footage = footage
        self._source = source
        self._timing = timing
        self._cameras: dict[int, _Recorded] = {}

    @property
    def recording(self) -> set[int]:
        """The ids of the cameras being recorded, and not being stopped."""
        return {
            camera_id for camera_id, recorded in self._cameras.items() if not recorded.stop.is_set()
        }

    def reconcile(self, cameras: Sequence[Camera]) -> None:
        """Record exactly `cameras`: start those not recorded, stop those not among them."""
        wanted = {camera.id: camera for camera in cameras if camera.removed_at is None}
        for camera_id, recorded in list(self._cameras.items()):
            if recorded.task is not None and recorded.task.done():
                del self._cameras[camera_id]
            elif not recorded.stop.is_set() and (
                camera_id not in wanted or wanted[camera_id].path != recorded.camera.path
            ):
                _log.info(
                    "%s (%s): no longer to be recorded; stopping",
                    recorded.camera.name,
                    recorded.camera.path,
                    extra={"camera": recorded.camera.name, "camera_id": camera_id},
                )
                recorded.stop.set()
        for camera_id, camera in wanted.items():
            if camera_id not in self._cameras:
                recorded = _Recorded(camera)
                recorded.task = asyncio.create_task(
                    self._supervise(recorded), name=f"camera {camera_id}"
                )
                self._cameras[camera_id] = recorded

    async def stop(self) -> None:
        """Stop every camera and wait for each to finish the Segment it is writing."""
        for recorded in self._cameras.values():
            recorded.stop.set()
        tasks = [recorded.task for recorded in self._cameras.values() if recorded.task]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cameras.clear()

    async def _supervise(self, recorded: _Recorded) -> None:
        camera = recorded.camera
        backoff = Backoff(_FIRST_WAIT, _LAST_WAIT)
        while not recorded.stop.is_set():
            started = time.monotonic()
            try:
                await record_camera(
                    camera,
                    self._source(camera),
                    self._footage / str(camera.id),
                    self._timing,
                    recorded.stop,
                )
            except Exception:
                if time.monotonic() - started >= _STEADY:
                    backoff.reset()
                wait = backoff.next()
                _log.exception(
                    "%s (%s): recording failed; starting it again in %.0f s",
                    camera.name,
                    camera.path,
                    wait,
                    extra={"camera": camera.name, "camera_id": camera.id, "path": camera.path},
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(recorded.stop.wait(), timeout=wait)
