"""Verify the deliberately limited SDK environment with a real model invocation."""
import importlib.metadata
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXCLUDED = ('jax', 'jaxlib', 'scipy', 'ml-dtypes', 'sounddevice', 'sentencepiece')
for name in EXCLUDED:
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        continue
    raise SystemExit(f'{name} 仍安装在该环境中，请使用全新的 .venv-lean，不要复用完整环境。')

import cv2
import numpy as np
from backend.inference import Inference
engine = Inference()
try:
    result = engine.recognize_image(np.zeros((480, 640, 3), dtype=np.uint8))
    assert result is not None and result['result']['landmarks'] == []
    print(f'精简环境实际模型推理通过：{result["ms"]:.1f} ms（空画面）')
finally:
    engine.close()
print('已确认未安装：' + ', '.join(EXCLUDED))
print('这是仅用于手势推理的环境；pip check 会报告 MediaPipe 全功能声明中的缺失依赖，详见 README。')
