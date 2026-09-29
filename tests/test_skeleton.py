import json
import tempfile
from pathlib import Path

from usagetrim.core.skeleton import (
    extract_symbol_or_range,
    skeletonize_code_ast,
    skeletonize_json,
    skeletonize_notebook,
    skeletonize_python,
)

SAMPLE_PYTHON = """\"\"\"Module docstring for auth service.\"\"\"

import os
from typing import Optional

API_KEY = "secret"

class AuthService:
    \"\"\"Handles user authentication and JWT generation.\"\"\"
    realm: str = "default"

    def __init__(self, secret: str):
        self.secret = secret
        self.cache = {}
        # 50 lines of complex setup
        for i in range(50):
            self.cache[i] = i * 2

    def verify_token(self, token: str, expiry: Optional[int] = None) -> bool:
        \"\"\"Verify token validity.\"\"\"
        if not token:
            return False
        # 20 lines of signature math
        return True

def standalone_helper(x: int) -> int:
    \"\"\"Helper calculation.\"\"\"
    return x * 42
"""


def test_skeletonize_python():
    skeleton = skeletonize_python(SAMPLE_PYTHON)
    assert "Module docstring for auth service." in skeleton
    assert "class AuthService:" in skeleton
    assert "Handles user authentication" in skeleton
    assert "def verify_token" in skeleton
    assert "expiry: Optional[int]" in skeleton
    assert "-> bool:" in skeleton
    assert "def standalone_helper(x: int) -> int:" in skeleton
    # Implementation body should be replaced with ...
    assert "self.cache = {}" not in skeleton
    assert "return x * 42" not in skeleton
    assert "..." in skeleton


def test_skeletonize_json():
    huge_json = '{"name": "app", "dependencies": ["a", "b", "c", "d", "e", "f", "g", "h"]}'
    res = skeletonize_json(huge_json, max_array_items=2)
    assert '"name": "app"' in res
    assert '"a"' in res
    assert '"b"' in res
    assert "more items omitted by usagetrim" in res


def test_extract_symbol_and_range():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(SAMPLE_PYTHON)
        tmp_path = f.name

    try:
        # Extract lines 8-12 (which contains class AuthService)
        range_res = extract_symbol_or_range(tmp_path, lines_range="8-12")
        assert "class AuthService:" in range_res
        assert "lines 8-12" in range_res

        # Extract symbol
        sym_res = extract_symbol_or_range(tmp_path, symbol="verify_token")
        assert "def verify_token" in sym_res
        assert "return True" in sym_res

        # Skeleton mode
        skel_res = extract_symbol_or_range(tmp_path, skeleton=True)
        assert "class AuthService:" in skel_res
        assert "..." in skel_res
    finally:
        Path(tmp_path).unlink()


def test_skeletonize_typescript():
    ts_code = """
export interface User {
    id: number;
    name: string;
}

export class UserService {
    private db: Database;

    constructor(db: Database) {
        this.db = db;
        console.log("initialized");
    }

    public async getUser(id: number): Promise<User> {
        const user = await this.db.find(id);
        if (!user) throw new Error("not found");
        return user;
    }
}
"""
    skeleton = skeletonize_code_ast(ts_code, ".ts")
    assert "export interface User" in skeleton
    assert "id: number" in skeleton
    assert "export class UserService" in skeleton
    assert "public async getUser(id: number): Promise<User>" in skeleton
    assert "const user = await this.db.find(id)" not in skeleton
    assert "..." in skeleton


def test_skeletonize_go():
    go_code = """package main

type Service struct {
    port int
}

func (s *Service) Start() error {
    log.Println("Starting service on port", s.port)
    return http.ListenAndServe(fmt.Sprintf(":%d", s.port), nil)
}
"""
    skeleton = skeletonize_code_ast(go_code, ".go")
    assert "package main" in skeleton
    assert "type Service struct" in skeleton
    assert "func (s *Service) Start() error" in skeleton
    assert "http.ListenAndServe" not in skeleton
    assert "..." in skeleton


def test_skeletonize_rust():
    rs_code = """pub struct Client {
    pub timeout_ms: u64,
}

impl Client {
    pub fn new(timeout: u64) -> Self {
        println!("creating new client");
        Self { timeout_ms: timeout }
    }
}
"""
    skeleton = skeletonize_code_ast(rs_code, ".rs")
    assert "pub struct Client" in skeleton
    assert "impl Client" in skeleton
    assert "pub fn new(timeout: u64) -> Self" in skeleton
    assert "creating new client" not in skeleton
    assert "..." in skeleton


def _tiny_notebook_fixture() -> dict:
    """Minimal .ipynb with a base64 PNG, long stdout, and preserved sources."""
    # Fake base64 PNG payload (not a real image; long enough to trip the stripper).
    fake_png = "iVBORw0KGgo" + ("A" * 4000) + "=="
    long_stdout = "\n".join(f"row_{i} value={i * 3}" for i in range(20))
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "widgets": {"state": {"w1": {"value": "huge-widget-state-" + ("x" * 200)}}},
        },
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {"tags": ["intro"]},
                "source": ["# Training notebook\n", "Keep this markdown.\n"],
            },
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {"scrolled": True},
                "source": ["import matplotlib.pyplot as plt\n", "print('hello')\n"],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": [line + "\n" for line in long_stdout.splitlines()],
                    },
                    {
                        "output_type": "display_data",
                        "metadata": {},
                        "data": {
                            "image/png": fake_png,
                            "text/plain": ["<Figure size 640x480>"],
                        },
                    },
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 2,
                "metadata": {},
                "source": ["df.head(100)\n"],
                "outputs": [],
            },
        ],
    }


def test_skeletonize_notebook_strips_outputs_and_keeps_source():
    raw = json.dumps(_tiny_notebook_fixture())
    stripped = skeletonize_notebook(raw)
    parsed = json.loads(stripped)

    assert parsed["nbformat"] == 4
    assert len(parsed["cells"]) == 3

    md = parsed["cells"][0]
    assert md["cell_type"] == "markdown"
    assert "Training notebook" in "".join(md["source"])
    assert md["metadata"].get("tags") == ["intro"]

    code = parsed["cells"][1]
    source_text = "".join(code["source"])
    assert "import matplotlib.pyplot as plt" in source_text
    assert "print('hello')" in source_text
    assert code["execution_count"] is None

    # Base64 image gone; text/plain kept or folded.
    display = next(o for o in code["outputs"] if o.get("output_type") == "display_data")
    png = "".join(display["data"]["image/png"])
    assert "stripped by usagetrim" in png
    assert "iVBORw0KGgo" not in png

    stream = next(o for o in code["outputs"] if o.get("output_type") == "stream")
    stream_text = "".join(stream["text"])
    assert "row_0" in stream_text
    assert "output lines folded" in stream_text
    assert "row_19" not in stream_text

    assert "widgets" not in parsed.get("metadata", {})
    assert "df.head(100)" in "".join(parsed["cells"][2]["source"])
    # Compact dumps so indentation does not mask the binary strip.
    compact_raw = len(json.dumps(json.loads(raw), separators=(",", ":")))
    compact_out = len(json.dumps(parsed, separators=(",", ":")))
    assert compact_out < compact_raw * 0.2


def test_extract_ipynb_auto_strips(tmp_path):
    nb_path = tmp_path / "tiny.ipynb"
    nb_path.write_text(json.dumps(_tiny_notebook_fixture()), encoding="utf-8")
    result = extract_symbol_or_range(nb_path)
    parsed = json.loads(result)
    assert "import matplotlib.pyplot as plt" in "".join(parsed["cells"][1]["source"])
    png = "".join(
        next(o for o in parsed["cells"][1]["outputs"] if o["output_type"] == "display_data")[
            "data"
        ]["image/png"]
    )
    assert "stripped by usagetrim" in png
