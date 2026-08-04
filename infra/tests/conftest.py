from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "docker: requires a working Docker daemon (DockerImageCode.from_image_asset)",
    )
