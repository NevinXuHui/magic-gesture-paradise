"""Shared native CPU model for PC and SmartApp, returning the existing JS contract."""
from pathlib import Path
import threading
import time
import cv2
import mediapipe as mp
import numpy as np


def default_model():
    backend = Path(__file__).resolve().parent
    packaged = backend / 'models/gesture_recognizer.task'
    return packaged if packaged.is_file() else backend.parent / 'public/models/gesture_recognizer.task'


def serialize_result(result):
    def points(hands):
        return [[dict(x=p.x, y=p.y, z=p.z) for p in hand] for hand in hands]
    def categories(hands):
        return [[dict(categoryName=c.category_name, score=c.score) for c in hand] for hand in hands]
    return dict(landmarks=points(result.hand_landmarks),
                worldLandmarks=points(result.hand_world_landmarks),
                gestures=categories(result.gestures), handedness=categories(result.handedness))


class Inference:
    def __init__(self, model_path=None):
        options = mp.tasks.vision.GestureRecognizerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path or default_model()), delegate=mp.tasks.BaseOptions.Delegate.CPU),
            running_mode=mp.tasks.vision.RunningMode.VIDEO, num_hands=2,
            min_hand_detection_confidence=.5, min_hand_presence_confidence=.5,
            min_tracking_confidence=.5,
            canned_gesture_classifier_options=mp.tasks.components.processors.ClassifierOptions(score_threshold=0.0, max_results=-1))
        self.model = mp.tasks.vision.GestureRecognizer.create_from_options(options)
        self.lock = threading.Lock()
        self.timestamp = 0

    def close(self):
        with self.lock:
            self.model.close()

    def recognize(self, body):
        frame = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError('无法解码摄像头 JPEG 帧')
        if max(frame.shape[:2]) > 1280:
            raise ValueError('图像尺寸过大，最长边限制 1280 像素')
        return self.recognize_image(frame)

    def recognize_image(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        # Drop concurrent work instead of building up delayed frames.
        if not self.lock.acquire(blocking=False):
            return None
        try:
            self.timestamp = max(self.timestamp + 1, time.monotonic_ns() // 1_000_000)
            started = time.perf_counter()
            result = self.model.recognize_for_video(image, self.timestamp)
            return dict(result=serialize_result(result), ms=(time.perf_counter()-started)*1000)
        finally:
            self.lock.release()

