"""Guards the one Dockerfile-shape assumption CDK synth can't see: which
stage `docker build` picks when a Lambda's `DockerImageCode.from_image_asset`
call omits `target=`.

Phase 7 appended a `lambda-stream` stage (`FROM lambda AS lambda-stream`)
after the `lambda` stage. Docker's default target with no `--target` flag is
the LAST stage in the file -- which silently became `lambda-stream` instead
of `lambda`. Every function that relied on that default (ApiFunction,
ExtractFunction, SynthesizeFunction, StitchFunction) built the wrong image
(uvicorn serving `src.chat_app:app`, no Lambda Runtime Interface Client)
until every call site was given an explicit `target="lambda"`. Caught only
by a real deploy: every route, including `/health`, returned a bare 500.

This is deliberately independent of `infra/tests/test_synth.py`'s CDK
assertions -- the synthesized CloudFormation template does not encode which
Docker stage an asset was built from, only its content hash.
"""

from __future__ import annotations

import os
import subprocess

import pytest

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))


@pytest.mark.docker
def test_default_build_target_runs_the_lambda_ric_handler() -> None:
    tag = "bookloud-test-default-target"
    subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=_BACKEND_DIR,
        check=True,
        capture_output=True,
    )
    try:
        result = subprocess.run(
            ["docker", "inspect", tag, "--format", "{{json .Config.Cmd}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        cmd = result.stdout.strip()
        assert cmd == '["lambda_function.handler"]', (
            f"docker build with no --target produced Cmd={cmd!r}, expected the "
            "`lambda` stage's RIC handler -- every Lambda that omits target= "
            "(ApiFunction, ExtractFunction, SynthesizeFunction, StitchFunction) "
            "depends on this default."
        )
    finally:
        subprocess.run(["docker", "rmi", tag], capture_output=True)
