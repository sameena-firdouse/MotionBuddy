import numpy as np
import cv2
import mediapipe as mp
import math
import pyttsx3
import threading
import queue
import time
import pythoncom
import json
from pathlib import Path
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

MODEL_PATH = "pose_landmarker_lite.task"
REFERENCE_PATH = "reference_squat.json"

with open(REFERENCE_PATH, "r", encoding="utf-8") as file:
    reference_data = json.load(file)

reference_frames = [
    item for item in reference_data["frames"]
    if item.get("knee_angle") is not None
]

if not reference_frames:
    raise RuntimeError(
        "No reference knee angles found in reference_squat.json"
    )

reference_angles = [
    float(item["knee_angle"])
    for item in reference_frames
]

print(
    f"Loaded {len(reference_angles)} reference pose measurements."
)
# ---------------- VOICE SYSTEM ----------------

speech_queue = queue.Queue()


def speech_worker():
    pythoncom.CoInitialize()
    engine = None

    try:
        engine = pyttsx3.init("sapi5")
        engine.setProperty("rate", 155)
        engine.setProperty("volume", 1.0)

        while True:
            message = speech_queue.get()

            try:
                if message is None:
                    break

                print("Speaking:", message)
                engine.say(message)
                engine.runAndWait()

            except Exception as error:
                print("Speech error:", error)

            finally:
                speech_queue.task_done()

    except Exception as error:
        print("Voice engine error:", error)

    finally:
        if engine is not None:
            engine.stop()
        pythoncom.CoUninitialize()


threading.Thread(
    target=speech_worker,
    daemon=True
).start()


def speak(message):
    # Prevent the same message from piling up in the queue.
    if message not in list(speech_queue.queue):
        speech_queue.put(message)


# ---------------- ANGLE CALCULATIONS ----------------

def calculate_angle(a, b, c):
    angle = math.degrees(
        math.atan2(c[1] - b[1], c[0] - b[0])
        - math.atan2(a[1] - b[1], a[0] - b[0])
    )

    angle = abs(angle)

    if angle > 180:
        angle = 360 - angle

    return angle


# Estimate forward torso lean relative to vertical.
# This works best when the camera sees you from the side.
def calculate_torso_lean(shoulder, hip):
    dx = shoulder[0] - hip[0]
    dy = hip[1] - shoulder[1]

    # 0 degrees means approximately upright.
    return abs(math.degrees(math.atan2(dx, dy)))
def compare_knee_angles(user_angle, reference_angle):
    difference = user_angle - reference_angle

    if abs(difference) <= 15:
        return "Good! Close to reference", difference

    if difference > 15:
        return "Bend your knees a little more", difference

    return "Try a shallower squat", difference


def select_reference_cycle(angles):
    """Extract one stand -> squat -> stand cycle from reference angles."""
    values = np.asarray(angles, dtype=float)
    if len(values) < 3:
        return values.tolist()

    bottom_idx = int(np.argmin(values))
    if bottom_idx == 0 or bottom_idx == len(values) - 1:
        return values.tolist()

    start_idx = int(np.argmax(values[:bottom_idx + 1]))
    end_idx = bottom_idx + int(np.argmax(values[bottom_idx:]))

    if end_idx <= start_idx:
        return values.tolist()

    cycle = values[start_idx:end_idx + 1]
    return cycle.tolist() if len(cycle) >= 3 else values.tolist()


def resample_sequence(values, target_length=40):
    """Resample sequences to the same length so reps of different speeds compare."""
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return values
    old_positions = np.linspace(0.0, 1.0, num=len(values))
    new_positions = np.linspace(0.0, 1.0, num=target_length)
    return np.interp(new_positions, old_positions, values)


def compare_repetition(user_angles, reference_cycle):
    """Return a simple knee-angle trajectory similarity score and feedback."""
    if len(user_angles) < 5 or len(reference_cycle) < 3:
        return None, "Not enough movement data; try one slower, complete squat."

    user_curve = resample_sequence(user_angles)
    reference_curve = resample_sequence(reference_cycle)
    mean_error = float(np.mean(np.abs(user_curve - reference_curve)))

    # A transparent angle-based score: 0-degree average error = 100;
    # 45 degrees or more average error = 0. This is a prototype score,
    # not a medical or full-body form assessment.
    similarity = max(0.0, min(100.0, 100.0 * (1.0 - mean_error / 45.0)))

    user_depth = float(np.min(user_angles))
    reference_depth = float(np.min(reference_cycle))
    depth_difference = user_depth - reference_depth

    if depth_difference > 12:
        feedback = "Try bending your knees a little more to match the reference depth."
    elif depth_difference < -12:
        feedback = "You went deeper than the reference. Keep the movement controlled."
    elif mean_error > 18:
        feedback = "Your movement timing or knee-angle path differs from the reference. Try a slower, controlled rep."
    else:
        feedback = "Good work. Your squat knee-angle pattern is close to the reference."

    return similarity, feedback


def get_movement_stage(knee_angle, previous_angle, squat_started):
    if not squat_started:
        return "Standing / ready"
    if knee_angle < 115:
        return "Bottom of squat"
    if previous_angle is not None and knee_angle < previous_angle - 1.5:
        return "Descending"
    if previous_angle is not None and knee_angle > previous_angle + 1.5:
        return "Rising"
    return "Squat in progress"


reference_cycle = select_reference_cycle(reference_angles)
print(f"Using {len(reference_cycle)} angles for one reference squat cycle.")

# ---------------- POSE DETECTION ----------------

options = vision.PoseLandmarkerOptions(
    base_options=python.BaseOptions(
        model_asset_path=MODEL_PATH
    ),
    running_mode=vision.RunningMode.VIDEO,
    num_poses=1
)

with vision.PoseLandmarker.create_from_options(options) as landmarker:
    camera = cv2.VideoCapture(0)

    if not camera.isOpened():
        print("Camera could not be opened.")
        raise SystemExit
    reference_camera = cv2.VideoCapture("reference_squat.mp4")

    if not reference_camera.isOpened():
        camera.release()
        print("Reference video could not be opened.")
        raise SystemExit
    print("MotionBuddy Posture Coach is running!")
    print("Press Q in the camera window to exit.")

    timestamp_ms = 0
    squat_count = 0
    squat_started = False
    user_rep_angles = []
    last_knee_angle = None
    last_similarity = None
    last_rep_feedback = "Complete a squat to see your similarity score."
    movement_stage = "Standing / ready"

    current_message = ""
    candidate_message = ""
    candidate_since = time.time()
    last_spoken_message = ""

    # Prevent posture warnings from being spoken repeatedly.
    last_posture_warning_time = 0
    warning_cooldown = 4

    speak("Welcome to MotionBuddy. Let's start exercising.")

    try:
        while True:
            success, frame = camera.read()

            if not success:
                break

            # Read the reference exercise video
            reference_success, reference_frame = reference_camera.read()

            # Restart the reference video when it reaches the end
            if not reference_success:
                reference_camera.set(cv2.CAP_PROP_POS_FRAMES, 0)
                reference_success, reference_frame = reference_camera.read()

            if not reference_success:
                print("Could not read the reference video.")
                break

            frame = cv2.flip(frame, 1)
            height, width = frame.shape[:2]

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(
                image_format=mp.ImageFormat.SRGB,
                data=rgb
            )

            timestamp_ms += 33

            result = landmarker.detect_for_video(
                mp_image, timestamp_ms
            )

            message = "Move into camera view"
            knee_angle = None
            torso_lean = None
            reference_angle = None
            comparison_message = "Complete a squat for a movement score"
            angle_difference = None
            if result.pose_landmarks:
                landmarks = result.pose_landmarks[0]

                # Right-side landmarks:
                # shoulder 12, hip 24, knee 26, ankle 28
                shoulder = landmarks[12]
                hip = landmarks[24]
                knee = landmarks[26]
                ankle = landmarks[28]

                visibility = min(
                    shoulder.visibility,
                    hip.visibility,
                    knee.visibility,
                    ankle.visibility
                )

                if visibility > 0.7:
                    shoulder_point = (shoulder.x, shoulder.y)
                    hip_point = (hip.x, hip.y)
                    knee_point = (knee.x, knee.y)
                    ankle_point = (ankle.x, ankle.y)

                    knee_angle = calculate_angle(
                        hip_point,
                        knee_point,
                        ankle_point
                    )
                    torso_lean = calculate_torso_lean(
                        shoulder_point,
                        hip_point
                    )

                    movement_stage = get_movement_stage(
                        knee_angle, last_knee_angle, squat_started
                    )

                    # Capture the whole repetition from the first bend until
                    # the user returns to standing, not just the bottom frames.
                    if knee_angle <= 160 and not squat_started:
                        squat_started = True
                        user_rep_angles = [knee_angle]
                    elif squat_started and knee_angle <= 165:
                        user_rep_angles.append(knee_angle)

                    if knee_angle > 160:
                        message = "Stand straight"

                        if squat_started:
                            # Include the final standing angle before scoring.
                            user_rep_angles.append(knee_angle)
                            squat_count += 1
                            squat_started = False

                            last_similarity, last_rep_feedback = compare_repetition(
                                user_rep_angles, reference_cycle
                            )
                            user_rep_angles = []

                            speak(
                                f"Squat {squat_count} completed. "
                                + (
                                    f"Similarity {int(last_similarity)} percent. "
                                    if last_similarity is not None else ""
                                )
                                + last_rep_feedback
                            )
                            print(
                                f"Rep {squat_count}: similarity="
                                f"{int(last_similarity) if last_similarity is not None else 'N/A'}%, "
                                f"feedback={last_rep_feedback}"
                            )

                    elif knee_angle < 110:
                        message = "Good! Squat position"

                    elif squat_started:
                        message = "Keep going, then stand up"

                    else:
                        message = "Bend your knees to squat"

                    last_knee_angle = knee_angle

                    # Basic forward-lean warning.
                    # Tune this threshold after testing your camera view.
                    if squat_started and torso_lean > 45:
                        message = "Keep your upper body more upright"

                        now_warning = time.time()

                        if (
                            now_warning - last_posture_warning_time
                            >= warning_cooldown
                        ):
                            speak(
                                "Check your posture. "
                                "Keep your upper body controlled."
                            )
                            last_posture_warning_time = now_warning
                    cv2.putText(
                        frame,
                        f"Stage: {movement_stage}",
                        (20, 190),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (255, 255, 0),
                        2
                    )

                    score_text = (
                        f"Last squat similarity: {last_similarity:.0f}%"
                        if last_similarity is not None
                        else "Similarity: complete one squat"
                    )
                    cv2.putText(
                        frame,
                        score_text,
                        (20, 225),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,
                        (0, 255, 0),
                        2
                    )
                    cv2.putText(
                        frame,
                        f"Knee angle: {int(knee_angle)} deg",
                        (20, 75),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.65,
                        (255, 255, 0),
                        2
                    )

                    cv2.putText(
                        frame,
                        f"Torso lean: {int(torso_lean)} deg",
                        (20, 150),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.65,
                        (255, 255, 0),
                        2
                    )

                else:
                    message = "Move into camera view"

                # Draw visible body landmarks
                for landmark in landmarks:
                    if landmark.visibility > 0.5:
                        x = int(landmark.x * width)
                        y = int(landmark.y * height)

                        cv2.circle(
                            frame,
                            (x, y),
                            4,
                            (0, 0, 255),
                            -1
                        )

            # Wait until an instruction remains stable.
            now = time.time()

            if message != candidate_message:
                candidate_message = message
                candidate_since = now

            elif (
                message != current_message
                and now - candidate_since >= 0.6
            ):
                current_message = message

                print("Coach says:", current_message)

                if current_message != last_spoken_message:
                    speak(current_message)
                    last_spoken_message = current_message

            display_message = current_message or message

            cv2.putText(
                frame,
                display_message,
                (20, 35),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2
            )

            cv2.putText(
                frame,
                f"Squats: {squat_count}",
                (20, 115),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2
            )

            feedback_display = last_rep_feedback
            # Keep the feedback readable by limiting the displayed length.
            cv2.putText(
                frame,
                feedback_display[:52],
                (20, 260),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (0, 255, 255),
                1
            )

            # Make both panels the same height
            panel_height = 360

            # Resize webcam panel
            user_width = int(width * panel_height / height)
            user_display = cv2.resize(
                frame, (user_width, panel_height)
            )

            # Resize reference video while preserving its aspect ratio
            ref_height, ref_width = reference_frame.shape[:2]
            ref_display_width = int(
                ref_width * panel_height / ref_height
            )

            reference_display = cv2.resize(
                reference_frame,
                (ref_display_width, panel_height)
            )

            # Label each panel
            cv2.putText(
                reference_display,
                "REFERENCE EXERCISE",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 255),
                2
            )

            cv2.putText(
                user_display,
                "YOUR MOVEMENT",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 255),
                2
            )

            # Place the reference on the left and webcam on the right
            combined_frame = np.hstack((
                reference_display,
                user_display
            ))

            cv2.imshow(
                "MotionBuddy - Follow and Exercise",
                combined_frame
            )

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    finally:
        camera.release()
        reference_camera.release()
        cv2.destroyAllWindows()
        speech_queue.put(None)
