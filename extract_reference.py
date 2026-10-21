
import cv2
import json
import math
import mediapipe as mp

from mediapipe.tasks import python
from mediapipe.tasks.python import vision

VIDEO_PATH = "reference_squat.mp4"
MODEL_PATH = "pose_landmarker_lite.task"
OUTPUT_PATH = "reference_squat.json"

# Sample one frame approximately every 5 frames.
FRAME_STEP = 5


def calculate_angle(a, b, c):
    """Calculate the angle ABC in degrees."""
    ba = (a[0] - b[0], a[1] - b[1])
    bc = (c[0] - b[0], c[1] - b[1])

    dot = ba[0] * bc[0] + ba[1] * bc[1]
    mag_ba = math.hypot(*ba)
    mag_bc = math.hypot(*bc)

    if mag_ba == 0 or mag_bc == 0:
        return None

    cosine = dot / (mag_ba * mag_bc)
    cosine = max(-1.0, min(1.0, cosine))

    return round(math.degrees(math.acos(cosine)), 2)


def get_point(landmarks, index):
    point = landmarks[index]
    return (point.x, point.y)


def main():
    options = vision.PoseLandmarkerOptions(
        base_options=python.BaseOptions(
            model_asset_path=MODEL_PATH
        ),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1
    )

    cap = cv2.VideoCapture(VIDEO_PATH)

    if not cap.isOpened():
        raise RuntimeError(
            f"Cannot open {VIDEO_PATH}. Check the filename and location."
        )

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0:
        fps = 30.0

    reference_frames = []
    frame_index = 0

    try:
        with vision.PoseLandmarker.create_from_options(options) as landmarker:
            while True:
                success, frame = cap.read()

                if not success:
                    break

                if frame_index % FRAME_STEP == 0:
                    timestamp_ms = round(frame_index * 1000 / fps)

                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    image = mp.Image(
                        image_format=mp.ImageFormat.SRGB,
                        data=rgb
                    )

                    result = landmarker.detect_for_video(
                        image, timestamp_ms
                    )

                    if result.pose_landmarks:
                        landmarks = result.pose_landmarks[0]

                        # MediaPipe body landmark indices:
                        # Right hip=24, knee=26, ankle=28
                        # Left hip=23, knee=25, ankle=27
                        right_knee = calculate_angle(
                            get_point(landmarks, 24),
                            get_point(landmarks, 26),
                            get_point(landmarks, 28)
                        )

                        left_knee = calculate_angle(
                            get_point(landmarks, 23),
                            get_point(landmarks, 25),
                            get_point(landmarks, 27)
                        )

                        knee_angles = [
                            a for a in [right_knee, left_knee]
                            if a is not None
                        ]

                        if knee_angles:
                            average_knee = round(
                                sum(knee_angles) / len(knee_angles), 2
                            )

                            reference_frames.append({
                                "timestamp_ms": timestamp_ms,
                                "knee_angle": average_knee
                            })

                            print(
                                f"Frame {frame_index}: "
                                f"knee angle = {average_knee} degrees"
                            )

                frame_index += 1

    finally:
        cap.release()

    if not reference_frames:
        raise RuntimeError(
            "No poses detected. Check the video and lighting."
        )

    output = {
        "exercise": "squat",
        "video": VIDEO_PATH,
        "fps": fps,
        "frame_step": FRAME_STEP,
        "frames": reference_frames
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as file:
        json.dump(output, file, indent=2)

    print("\nReference extraction complete!")
    print(f"Frames with detected poses: {len(reference_frames)}")
    print(f"Saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
