import os

import numpy as np
import pytest

from exact.core.png_io import save_png
from exact.core.synthetic import payload_image


def pytest_addoption(parser):
    parser.addoption("--runslow", action="store_true", default=False, help="run full-size image tests")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow"):
        return
    skip = pytest.mark.skip(reason="needs --runslow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def payload_rgba() -> np.ndarray:
    return payload_image(97, 61, "RGBA", seed=1)


@pytest.fixture
def payload_rgb() -> np.ndarray:
    return payload_image(83, 47, "RGB", seed=2)


@pytest.fixture
def rgba_png(tmp_path, payload_rgba) -> str:
    path = os.path.join(tmp_path, "payload.png")
    save_png(path, payload_rgba)
    return path


@pytest.fixture
def rgb_png(tmp_path, payload_rgb) -> str:
    path = os.path.join(tmp_path, "payload-rgb.png")
    save_png(path, payload_rgb)
    return path
