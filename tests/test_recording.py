import numpy as np
import pytest


def test_contact_sheet_layout():
    pytest.importorskip("cv2")
    from jarvis.hands.recording import _sheet

    frames = [(float(i), np.full((360, 640, 3), i * 20, np.uint8)) for i in range(10)]
    sheet = _sheet(frames, k=6)
    assert sheet.shape[1] == 640 * 3 and sheet.shape[0] == 360 * 2       # 6 frames -> 3 x 2 grid
    assert _sheet(frames[:2], k=6).shape[0] == 360                       # fewer frames than k
