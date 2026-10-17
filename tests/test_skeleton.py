import json
import tempfile
from pathlib import Path

from usagetrim.core.skeleton import (
    extract_symbol_or_range,
    extract_types_stub,
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


def test_extract_types_stub_python():
    from usagetrim.metrics.tokenizer import count_tokens

    body_chunks = "\n".join(
        f"        self.transactions.append({{'id': {i}, 'status': 'pending', 'payload': 'chunk_{i}' * 50}})"
        for i in range(120)
    )
    total_chunks = "\n".join(
        f"        # compliance calculation turn {i}\n        subtotal += {i} * 0.45"
        for i in range(120)
    )
    pdf_chunks = "\n".join(
        f"    buffer.extend(b'PDF_RAW_STREAM_DATA_CHUNK_{i}\\n')" for i in range(120)
    )
    py_code = f"""\"\"\"Extensive billing calculation engine module.\"\"\"
from typing import Dict, List, Optional, Union, TypeAlias
from dataclasses import dataclass

DEFAULT_TAX_RATE = 0.23
CurrencyCode: TypeAlias = str

@dataclass
class InvoiceItem:
    \"\"\"Represents a billable line item.\"\"\"
    sku: str
    quantity: int
    unit_price: float = 0.0

class BillingService:
    \"\"\"Main business logic for enterprise invoicing.\"\"\"
    tax_rate: float = DEFAULT_TAX_RATE

    def __init__(self, currency: CurrencyCode):
        \"\"\"Initialize billing service with local currency.\"\"\"
        self.currency = currency
        self.transactions = []
{body_chunks}

    def calculate_total(self, items: List[InvoiceItem], discount: Optional[float] = None) -> float:
        \"\"\"Perform complex discount calculation and tax evaluation.\"\"\"
        subtotal = 0.0
{total_chunks}
        return round(subtotal, 2)

def generate_invoice_pdf(service: BillingService, items: List[InvoiceItem]) -> bytes:
    \"\"\"Render PDF report with embedded fonts and styling.\"\"\"
    buffer = bytearray()
{pdf_chunks}
    return bytes(buffer)
"""
    stub = extract_types_stub(py_code, ".py")
    assert "Extensive billing calculation engine module." not in stub
    assert "class InvoiceItem:" in stub
    assert "sku: str" in stub
    assert "unit_price: float = 0.0" in stub
    assert "class BillingService:" in stub
    assert "def calculate_total(self, items: List[InvoiceItem], discount: Optional[float]" in stub
    assert ") -> float: ..." in stub
    assert (
        "def generate_invoice_pdf(service: BillingService, items: List[InvoiceItem]) -> bytes: ..."
        in stub
    )
    assert "PDF_RAW_STREAM_DATA" not in stub
    assert "compliance calculation turn" not in stub

    # Acceptance criteria: > 80% token savings
    raw_tokens = count_tokens(py_code).avg
    stub_tokens = count_tokens(stub).avg
    savings = (raw_tokens - stub_tokens) / raw_tokens
    assert savings > 0.80, f"Expected >80% reduction, got {savings:.2%}"


def test_extract_types_stub_typescript():
    from usagetrim.metrics.tokenizer import count_tokens

    conn_chunks = "\n".join(
        f"        console.log('Setting up client connection pool {i}', 'x'.repeat(40));"
        for i in range(100)
    )
    req_chunks = "\n".join(
        f"        console.log('Sending network payload chunk {i}', 'data'.repeat(20));"
        for i in range(100)
    )
    audit_chunks = "\n".join(
        f"        console.log('Audit creation trail step {i}', 'token'.repeat(15));"
        for i in range(80)
    )
    ts_code = f"""import {{ Client, Options }} from '@org/client';

const INTERNAL_SECRET_SALT = "x983274982374";
let cachedCount = 0;
function internalHelper(): number {{
    let acc = 0;
    for (let i = 0; i < 200; i++) {{ acc += i; }}
    return acc;
}}

export const API_VERSION = "2026-10-17";
export type UserId = string | number;

export enum Priority {{
    Low = 1,
    Medium = 2,
    High = 3,
}}

export interface TaskRecord {{
    id: UserId;
    title: string;
    priority: Priority;
    tags?: string[];
}}

export class TaskManager {{
    private client: Client;

    constructor(client: Client) {{
        this.client = client;
{conn_chunks}
    }}

    public async assignTask(taskId: UserId, assignee: string): Promise<boolean> {{
{req_chunks}
        return true;
    }}
}}

export function createTask(title: string, priority: Priority = Priority.Low): TaskRecord {{
{audit_chunks}
    return {{ id: "1", title, priority }};
}}
"""
    stub = extract_types_stub(ts_code, ".ts")
    assert "export interface TaskRecord" in stub
    assert "id: UserId" in stub
    assert "export type UserId" in stub
    assert "export enum Priority" in stub
    assert "export class TaskManager" in stub
    assert "public async assignTask(taskId: UserId, assignee: string): Promise<boolean>" in stub
    assert (
        "export function createTask(title: string, priority: Priority = Priority.Low): TaskRecord"
        in stub
    )
    # Internal variables and loop bodies stripped
    assert "INTERNAL_SECRET_SALT" not in stub
    assert "internalHelper" not in stub
    assert "Setting up client connection pool" not in stub
    assert "Audit creation trail step" not in stub

    # Acceptance criteria: > 80% token savings
    raw_tokens = count_tokens(ts_code).avg
    stub_tokens = count_tokens(stub).avg
    savings = (raw_tokens - stub_tokens) / raw_tokens
    assert savings > 0.80, f"Expected >80% reduction, got {savings:.2%}"


def test_extract_types_stub_go():
    from usagetrim.metrics.tokenizer import count_tokens

    warm_chunks = "\n".join(
        f'        fmt.Println("Warming up internal connection queues {i}", "data_chunk_payload_stream")'
        for i in range(120)
    )
    ledger_chunks = "\n".join(
        f'        fmt.Println("Processing settlement ledger chunk {i}", "audit_record_trace_line")'
        for i in range(120)
    )
    go_code = f"""package payment

import (
    "context"
    "fmt"
    "time"
)

const (
    DefaultTimeout = 30 * time.Second
    MaxRetryCount  = 5
)

var internalPoolMetrics = make(map[string]int64)

type Transaction struct {{
    ID        string    `json:"id"`
    Amount    int64     `json:"amount"`
    Timestamp time.Time `json:"timestamp"`
}}

type Processor interface {{
    Process(ctx context.Context, tx *Transaction) error
    Cancel(ctx context.Context, txID string) error
}}

type Engine struct {{
    timeout time.Duration
}}

func NewEngine(timeout time.Duration) *Engine {{
{warm_chunks}
    return &Engine{{timeout: timeout}}
}}

func (e *Engine) Process(ctx context.Context, tx *Transaction) error {{
{ledger_chunks}
    return nil
}}
"""
    stub = extract_types_stub(go_code, ".go")
    assert "package payment" in stub
    assert "type Transaction struct" in stub
    assert "ID        string" in stub
    assert "type Processor interface" in stub
    assert "Process(ctx context.Context, tx *Transaction) error" in stub
    assert "type Engine struct" in stub
    assert "func NewEngine(timeout time.Duration) *Engine" in stub
    assert "func (e *Engine) Process(ctx context.Context, tx *Transaction) error" in stub
    # Loop bodies and internal metrics stripped
    assert "internalPoolMetrics" not in stub
    assert "Warming up internal connection queues" not in stub
    assert "Processing settlement ledger chunk" not in stub

    # Acceptance criteria: > 80% token savings
    raw_tokens = count_tokens(go_code).avg
    stub_tokens = count_tokens(stub).avg
    savings = (raw_tokens - stub_tokens) / raw_tokens
    assert savings > 0.80, f"Expected >80% reduction, got {savings:.2%}"


def test_extract_types_stub_rust():
    from usagetrim.metrics.tokenizer import count_tokens

    alloc_chunks = "\n".join(
        f'        println!("allocating memory pool partition segment {i}", "extended_buffer_line_text");'
        for i in range(120)
    )
    spawn_chunks = "\n".join(
        f'        println!("Spawning background actor thread worker {i}", "worker_thread_diagnostic");'
        for i in range(120)
    )
    rs_code = f"""use std::collections::HashMap;
use std::sync::Arc;

pub const MAX_CONCURRENCY: usize = 64;
static GLOBAL_ATOMIC_COUNTER: usize = 0;

pub struct ClusterConfig {{
    pub name: String,
    pub nodes: Vec<String>,
}}

pub trait Dispatcher {{
    fn dispatch(&self, message: &str) -> Result<usize, String>;
}}

pub enum ClusterState {{
    Leader,
    Follower,
    Candidate,
}}

impl ClusterConfig {{
    pub fn new(name: &str) -> Self {{
{alloc_chunks}
        Self {{
            name: name.to_string(),
            nodes: Vec::new(),
        }}
    }}
}}

pub fn spawn_cluster(cfg: ClusterConfig) -> bool {{
{spawn_chunks}
    true
}}
"""
    stub = extract_types_stub(rs_code, ".rs")
    assert "pub struct ClusterConfig" in stub
    assert "pub trait Dispatcher" in stub
    assert "pub enum ClusterState" in stub
    assert "impl ClusterConfig" in stub
    assert "pub fn new(name: &str) -> Self" in stub
    assert "pub fn spawn_cluster(cfg: ClusterConfig) -> bool" in stub
    # Implementation loops and bodies stripped
    assert "GLOBAL_ATOMIC_COUNTER" not in stub
    assert "Spawning background actor thread worker" not in stub

    # Acceptance criteria: > 80% token savings
    raw_tokens = count_tokens(rs_code).avg
    stub_tokens = count_tokens(stub).avg
    savings = (raw_tokens - stub_tokens) / raw_tokens
    assert savings > 0.80, f"Expected >80% reduction, got {savings:.2%}"


def test_extract_symbol_or_range_types_only(tmp_path):
    py_file = tmp_path / "models.py"
    py_file.write_text(
        "class Model:\n    x: int = 1\n    def run(self):\n        for i in range(100): pass\n"
    )
    res = extract_symbol_or_range(py_file, types_only=True)
    assert "class Model:" in res
    assert "x: int = 1" in res
    assert "def run(self): ..." in res
    assert "for i in range" not in res


def test_mcp_usagetrim_read_types_only(tmp_path):
    from usagetrim.mcp.server import handle_usagetrim_read

    ts_file = tmp_path / "api.ts"
    ts_file.write_text(
        "export interface User { id: string; }\nexport function getUser(): User {\n  const x = 1;\n  return { id: '1' };\n}\n"
    )
    res = handle_usagetrim_read({"path": str(ts_file), "types_only": True})
    assert "export interface User" in res
    assert "export function getUser(): User" in res
    assert "const x = 1" not in res
