import logging
import threading
import time
from collections.abc import Sequence

import cv2
from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose

from reachy_lookat.gstreamer_nvidia_patch import apply_nvidia_unixfd_patch

# Apply the NVIDIA Jetson GStreamer plugin patch before any SDK initialization.
# Without it, the camera IPC socket is never created on Jetson and no frames flow
# (no-op off-Jetson). See reachy_chat/main.py for the same pattern.
apply_nvidia_unixfd_patch()

logger = logging.getLogger(__name__)

# Warn-once bookkeeping so a persistent failure doesn't spam the 50 Hz loop.
_warned_keys: set[str] = set()


def _warn_once(key: str, message: str, exc: Exception | None = None) -> None:
    if key in _warned_keys:
        return
    _warned_keys.add(key)
    if exc is not None:
        logger.warning("%s: %s", message, exc)
    else:
        logger.warning(message)


CONTROL_PERIOD_SECONDS = 0.02
SMOOTHING_ALPHA = 0.4
TRACKING_RANGE_DEGREES = 30.0
RETURN_HOME_DURATION_SECONDS = 2.5
SLEEP_CUE_SETTLE_SECONDS = 1.0
YAW_LIMITS = (-180.0, 180.0)
PITCH_LIMITS = (-40.0, 40.0)

Face = tuple[int, int, int, int]


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def select_largest_face(faces: Sequence[Face]) -> Face | None:
    if len(faces) == 0:
        return None
    return max(faces, key=lambda face: face[2] * face[3])


def compute_target_from_face(
    face: Face,
    frame_shape: Sequence[int],
) -> tuple[float, float]:
    height, width = int(frame_shape[0]), int(frame_shape[1])
    if height <= 0 or width <= 0:
        raise ValueError("Frame dimensions must be positive.")

    x, y, face_width, face_height = face
    center_x = x + face_width / 2.0
    center_y = y + face_height / 2.0

    normalized_x = 2.0 * center_x / width - 1.0
    normalized_y = 2.0 * center_y / height - 1.0

    yaw = clamp(
        -normalized_x * TRACKING_RANGE_DEGREES,
        *YAW_LIMITS,
    )
    pitch = clamp(
        normalized_y * TRACKING_RANGE_DEGREES,
        *PITCH_LIMITS,
    )
    return yaw, pitch


def smooth_target(
    current_yaw: float,
    current_pitch: float,
    target_yaw: float,
    target_pitch: float,
    alpha: float = SMOOTHING_ALPHA,
) -> tuple[float, float]:
    alpha = clamp(alpha, 0.0, 1.0)
    yaw = current_yaw + alpha * (target_yaw - current_yaw)
    pitch = current_pitch + alpha * (target_pitch - current_pitch)
    return (
        clamp(yaw, *YAW_LIMITS),
        clamp(pitch, *PITCH_LIMITS),
    )


class ReachyLookat(ReachyMiniApp):
    custom_app_url: str | None = None
    request_media_backend: str | None = None

    @staticmethod
    def try_get_frame(reachy_mini: ReachyMini):
        try:
            frame = reachy_mini.media.get_frame()
        except Exception as exc:
            _warn_once(
                "get_frame",
                "lookat: camera get_frame() failed (is the daemon running with a "
                "camera, and on Jetson is the unixfd patch applied?)",
                exc,
            )
            return None

        shape = getattr(frame, "shape", None)
        if (
            frame is None
            or shape is None
            or len(shape) != 3
            or shape[0] <= 0
            or shape[1] <= 0
            or shape[2] < 3
        ):
            return None
        return frame

    @staticmethod
    def detect_faces(detector: cv2.CascadeClassifier, frame) -> Sequence[Face]:
        try:
            grayscale = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            return detector.detectMultiScale(
                grayscale,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(30, 30),
            )
        except Exception as exc:
            _warn_once("detect", "lookat: face detection failed", exc)
            return ()

    @staticmethod
    def create_face_detector() -> cv2.CascadeClassifier | None:
        try:
            detector = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
            if detector.empty():
                return None
            return detector
        except Exception:
            return None

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        detector = self.create_face_detector()
        if detector is None:
            logger.warning(
                "lookat: face detector failed to load; head will stay centered"
            )
        smoothed_yaw = 0.0
        smoothed_pitch = 0.0
        next_tick = time.monotonic()
        seen_frame = False

        try:
            while not stop_event.is_set():
                frame = self.try_get_frame(reachy_mini)
                if frame is not None and not seen_frame:
                    seen_frame = True
                    logger.info(
                        "lookat: receiving camera frames (%dx%d)",
                        frame.shape[1],
                        frame.shape[0],
                    )
                if frame is not None and detector is not None:
                    face = select_largest_face(self.detect_faces(detector, frame))
                    if face is not None:
                        target_yaw, target_pitch = compute_target_from_face(
                            face,
                            frame.shape,
                        )
                        smoothed_yaw, smoothed_pitch = smooth_target(
                            smoothed_yaw,
                            smoothed_pitch,
                            target_yaw,
                            target_pitch,
                        )

                pose = create_head_pose(
                    yaw=clamp(smoothed_yaw, *YAW_LIMITS),
                    pitch=clamp(smoothed_pitch, *PITCH_LIMITS),
                    degrees=True,
                )
                try:
                    reachy_mini.set_target(head=pose, body_yaw=0.0)
                except Exception as exc:
                    _warn_once("set_target", "lookat: set_target failed", exc)

                next_tick += CONTROL_PERIOD_SECONDS
                stop_event.wait(max(0.0, next_tick - time.monotonic()))
        finally:
            self._return_to_neutral(reachy_mini)

    @staticmethod
    def _return_to_neutral(reachy_mini: ReachyMini) -> None:
        """Recenter the head on exit so the robot isn't left stuck mid-motion (#9)."""
        try:
            reachy_mini.goto_target(
                head=create_head_pose(degrees=True),
                body_yaw=0.0,
                duration=RETURN_HOME_DURATION_SECONDS,
            )
        except (AttributeError, TypeError) as exc:
            logger.warning(
                "lookat: graceful return-home unavailable; snapping home: %s", exc
            )
            try:
                reachy_mini.set_target(
                    head=create_head_pose(degrees=True),
                    body_yaw=0.0,
                )
            except (AttributeError, TypeError) as set_exc:
                logger.warning("lookat: failed to snap home on exit: %s", set_exc)
            except Exception as set_exc:
                logger.debug("lookat: failed to snap home on exit: %s", set_exc)
        except Exception as exc:
            logger.debug("lookat: failed to return home on exit: %s", exc)

        try:
            reachy_mini.media.play_sound("go_sleep.wav")
            time.sleep(SLEEP_CUE_SETTLE_SECONDS)
        except AttributeError as exc:
            logger.warning("lookat: sleep cue unavailable on exit: %s", exc)
        except Exception as exc:
            logger.debug("lookat: sleep cue failed on exit: %s", exc)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = ReachyLookat()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()
