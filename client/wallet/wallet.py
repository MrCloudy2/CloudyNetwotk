"""
=============================================================================
  Blockchain Wallet — PySide6 Desktop App
  Connects to server.py at http://127.0.0.1:8765
  Requires: pip install PySide6 ecdsa requests
=============================================================================
"""

import sys
import json
import struct
import time
import hashlib
import threading
import os
import requests
import ecdsa

import wire

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QTabWidget,
    QFrame, QScrollArea, QSizePolicy, QMessageBox, QGridLayout,
    QSplitter, QListWidget, QListWidgetItem, QGroupBox, QSpacerItem,
    QDialog, QDialogButtonBox, QFormLayout, QProgressBar, QStackedWidget,
    QToolButton, QCheckBox
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QSize, QPropertyAnimation,
    QEasingCurve, QRect, QPoint
)
from PySide6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPixmap, QPainter, QBrush,
    QLinearGradient, QFontDatabase, QPen, QClipboard, QAction
)
from PySide6.QtWidgets import QButtonGroup, QRadioButton

# GPU mining support (optional — wallet works fine without pyopencl/pycuda)
try:
    from gpu_miner import GpuMiner, detect_gpu
    _GPU_NAME     = detect_gpu()   # None if no GPU or no backend installed
    GPU_AVAILABLE = _GPU_NAME is not None
except Exception:
    # Catch all exceptions — pycuda/pyopencl can raise non-ImportError on
    # Windows when drivers are present but not fully initialised at import time.
    GpuMiner      = None
    GPU_AVAILABLE = False
    _GPU_NAME     = None

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
SERVER_URL = "http://cloudy.freemyip.com:8765"
BLOCK_REWARD = 1
MINING_CHECK_INTERVAL = 500_000   # kept for About tab display only; CPU miner now uses time-based 10 s poll

# Speed presets ───────────────────────────────────────────────────────────────
# CPU: sleep injected per nonce in the inner Python loop
CPU_SLEEP_NORMAL = 0.0001    # 100 µs/nonce  → frees ≈50 % CPU headroom
CPU_SLEEP_HIGH   = 0.0       # no sleep      → max hashrate

# GPU: sleep between 1 M-nonce batches  (uses stop-flag wait, so it's responsive)
GPU_SLEEP_NORMAL = 0.08      # 80 ms rest between batches → GPU ~< 50 % load
GPU_SLEEP_HIGH   = 0.0       # no rest       → max hashrate
GPU_BATCH_SIZE   = 1_000_000 # nonces per GPU dispatch

# ─────────────────────────────────────────────────────────────────────────────
#  COLOUR PALETTE  (dark industrial / terminal aesthetic)
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "bg":        "#0d0f14",
    "bg2":       "#13161e",
    "bg3":       "#1a1e28",
    "border":    "#252a38",
    "border2":   "#313849",
    "accent":    "#4fffb0",      # neon mint
    "accent2":   "#00c8ff",      # cyan
    "accent3":   "#ff6b6b",      # warning red
    "accent4":   "#f7c948",      # gold
    "text":      "#e0e6f0",
    "text2":     "#7a8499",
    "text3":     "#4a5168",
    "success":   "#3ddc84",
    "error":     "#ff5f57",
    "warning":   "#ffbd2e",
}

STYLESHEET = f"""
/* ── Root ────────────────────────────────────────────────────────── */
QMainWindow, QDialog {{
    background: {C["bg"]};
}}
QWidget {{
    background: transparent;
    color: {C["text"]};
    font-family: "JetBrains Mono", "Fira Code", "Courier New", monospace;
    font-size: 12px;
}}

/* ── Tab widget ──────────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {C["border"]};
    background: {C["bg2"]};
    border-radius: 8px;
}}
QTabWidget > QWidget {{
    background: {C["bg2"]};
    color: {C["text"]};
}}
QTabBar {{
    background: transparent;
    color: {C["text2"]};
}}
QTabBar::tab {{
    background: {C["bg3"]};
    color: {C["text2"]};
    padding: 10px 22px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QTabBar::tab:selected {{
    background: {C["bg2"]};
    color: {C["accent"]};
    border-bottom: 2px solid {C["accent"]};
}}
QTabBar::tab:hover:!selected {{
    background: {C["border"]};
    color: {C["text"]};
}}
QTabBar::tab:!selected {{
    color: {C["text2"]};
    background: {C["bg3"]};
}}

/* ── Buttons ─────────────────────────────────────────────────────── */
QPushButton {{
    background: {C["bg3"]};
    color: {C["text"]};
    border: 1px solid {C["border2"]};
    border-radius: 6px;
    padding: 8px 18px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton:hover {{
    background: {C["border"]};
    border-color: {C["accent"]};
    color: {C["accent"]};
}}
QPushButton:pressed {{
    background: {C["bg"]};
}}
QPushButton#primary {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #1a7a52, stop:1 #155f40);
    color: {C["accent"]};
    border: 1px solid {C["accent"]};
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #23a36c, stop:1 #1a7a52);
}}
QPushButton#danger {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #7a2020, stop:1 #5f1515);
    color: {C["error"]};
    border: 1px solid {C["error"]};
}}
QPushButton#danger:hover {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #a32323, stop:1 #7a2020);
}}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {C["border2"]};
    color: {C["text2"]};
    padding: 5px 10px;
    font-size: 11px;
}}
QPushButton#ghost:hover {{
    border-color: {C["accent2"]};
    color: {C["accent2"]};
}}

/* ── Inputs ──────────────────────────────────────────────────────── */
QLineEdit, QTextEdit {{
    background: {C["bg"]};
    color: {C["text"]};
    border: 1px solid {C["border"]};
    border-radius: 6px;
    padding: 8px 12px;
    selection-background-color: {C["accent"]};
    selection-color: {C["bg"]};
}}
QLineEdit:focus, QTextEdit:focus {{
    border-color: {C["accent"]};
}}
QLineEdit::placeholder {{
    color: {C["text3"]};
}}

/* ── Labels ──────────────────────────────────────────────────────── */
QLabel#heading {{
    color: {C["text"]};
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#subheading {{
    color: {C["text2"]};
    font-size: 11px;
    letter-spacing: 0.5px;
}}
QLabel#accent {{
    color: {C["accent"]};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#mono {{
    color: {C["text2"]};
    font-size: 10px;
    font-family: "JetBrains Mono", "Fira Code", "Courier New", monospace;
}}
QLabel#tag {{
    background: {C["bg3"]};
    color: {C["text2"]};
    border: 1px solid {C["border"]};
    border-radius: 3px;
    padding: 2px 6px;
    font-size: 10px;
}}

/* ── Cards / Frames ──────────────────────────────────────────────── */
QFrame#card {{
    background: {C["bg3"]};
    border: 1px solid {C["border"]};
    border-radius: 8px;
}}
QFrame#card_accent {{
    background: {C["bg3"]};
    border: 1px solid {C["accent"]};
    border-left: 3px solid {C["accent"]};
    border-radius: 8px;
}}
QFrame#separator {{
    background: {C["border"]};
    max-height: 1px;
    min-height: 1px;
}}

/* ── List widgets ────────────────────────────────────────────────── */
QListWidget {{
    background: {C["bg"]};
    border: 1px solid {C["border"]};
    border-radius: 6px;
    outline: 0;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-bottom: 1px solid {C["border"]};
    color: {C["text2"]};
    font-size: 11px;
}}
QListWidget::item:selected {{
    background: {C["bg3"]};
    color: {C["accent"]};
}}
QListWidget::item:hover {{
    background: {C["bg3"]};
}}

/* ── Progress bar ────────────────────────────────────────────────── */
QProgressBar {{
    background: {C["bg"]};
    border: 1px solid {C["border"]};
    border-radius: 4px;
    height: 6px;
    text-align: center;
    font-size: 0px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 {C["accent"]}, stop:1 {C["accent2"]});
    border-radius: 4px;
}}

/* ── Scroll bars ─────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {C["bg"]};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {C["border2"]};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: {C["text3"]};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {C["bg"]};
    height: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {C["border2"]};
    border-radius: 4px;
    min-width: 20px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── Group box ───────────────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid {C["border"]};
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px 8px 8px 8px;
    font-size: 11px;
    font-weight: 600;
    color: {C["text2"]};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {C["text2"]};
}}

/* ── Tooltips ────────────────────────────────────────────────────── */
QToolTip {{
    background: {C["bg3"]};
    color: {C["text"]};
    border: 1px solid {C["border2"]};
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 11px;
}}

/* ── Checkbox ────────────────────────────────────────────────────── */
QCheckBox {{
    color: {C["text2"]};
    font-size: 11px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {C["border2"]};
    border-radius: 3px;
    background: {C["bg"]};
}}

QCheckBox::indicator:checked {{
    background: {C["accent"]};
    border-color: {C["accent"]};
}}

/* ── Radio buttons ──────────────────────────────────────────── */
/* Fully explicit so Windows native style never overrides our dark theme  */
QRadioButton {{
    color: {C["text2"]};
    font-size: 11px;
    spacing: 8px;
    background: transparent;
}}
QRadioButton:hover {{
    color: {C["text"]};
}}
QRadioButton:disabled {{
    color: {C["text3"]};
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 7px;
    border: 2px solid {C["border2"]};
    background: {C["bg"]};
}}
QRadioButton::indicator:hover {{
    border-color: {C["accent"]};
}}
QRadioButton::indicator:checked {{
    background: {C["accent"]};
    border: 3px solid {C["bg"]};
    outline: 2px solid {C["accent"]};
}}
QRadioButton::indicator:disabled {{
    border-color: {C["text3"]};
    background: {C["bg3"]};
}}

/* ── Context menus ──────────────────────────────────────────── */
QMenu {{
    background: {C["bg3"]};
    color: {C["text"]};
    border: 1px solid {C["border2"]};
    border-radius: 6px;
    padding: 4px 0px;
    font-size: 11px;
}}
QMenu::item {{
    padding: 6px 20px 6px 12px;
    background: transparent;
    color: {C["text"]};
}}
QMenu::item:selected {{
    background: {C["border"]};
    color: {C["accent"]};
}}
QMenu::item:disabled {{
    color: {C["text3"]};
}}
QMenu::separator {{
    height: 1px;
    background: {C["border"]};
    margin: 3px 8px;
}}
"""

# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_address(public_key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()

def truncate(s: str, head: int = 8, tail: int = 8) -> str:
    if len(s) <= head + tail + 3:
        return s
    return f"{s[:head]}…{s[-tail:]}"

# Maximum possible SHA-256 target (all bits set = easiest mining)
#_MAX_TARGET = (1 << 256) - 1
MAX_256 = 2**256  # Total number of possible SHA-256 hashes

def format_difficulty(target) -> str:
    """
    Convert a raw difficulty target integer to the expected average
    number of hash attempts required to find a valid solution.
    """
    try:
        tgt = int(target)
    except (TypeError, ValueError):
        return "?"

    if tgt < 0:
        # Re-interpret negative 256-bit overflow as unsigned
        tgt = tgt & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF

    # If the target is zero, it's mathematically impossible to find a block.
    # Attempts approach infinity.
    if tgt == 0:
        return "∞"

    # Purely probabilistic average attempts: 2^256 / target
    attempts = MAX_256 / tgt

    # Format the float into a clean k, M, G string
    if attempts >= 1_000_000_000_000:
        return f"{attempts/1_000_000_000_000:.2f}T"
    if attempts >= 1_000_000_000:
        return f"{attempts/1_000_000_000:.2f}G"
    if attempts >= 1_000_000:
        return f"{attempts/1_000_000:.2f}M"
    if attempts >= 1_000:
        return f"{attempts/1_000:.2f}k"

    return f"{attempts:.2f}"

_BINARY_HEADERS = {"Content-Type": "application/octet-stream"}

# ---------------------------------------------------------------------------
#  Client-side rate limiter
#
#  Server enforces:  30 req/min global,  10 req/min for /add_transaction
#  We stay safely under those limits client-side so we never get 429'd.
#
#  Strategy:
#    • Global token bucket: 28 tokens/min (≈ 1 token per 2.14 s) with a
#      burst allowance of 6 tokens.  Keeps total traffic well under 30/min
#      even when multiple background workers fire simultaneously.
#    • Per-endpoint minimum gap enforces sensible polling intervals for the
#      high-frequency calls (blockchain poll, mining job fetch).
#    • /add_transaction gets its own stricter bucket (8/min, burst 2).
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Thread-safe token-bucket rate limiter with optional per-path minimum gap.

    tokens_per_min : average requests per minute allowed
    burst          : maximum tokens that can accumulate (short burst headroom)
    min_gaps       : dict mapping URL path prefix → minimum seconds between calls
    """

    def __init__(self, tokens_per_min: float, burst: int,
                 min_gaps: dict | None = None):
        self._rate      = tokens_per_min / 60.0   # tokens per second
        self._burst     = float(burst)
        self._tokens    = float(burst)
        self._last_refill = time.monotonic()
        self._min_gaps  = min_gaps or {}          # path prefix → min seconds
        self._last_call: dict[str, float] = {}    # path prefix → last call time
        self._lock      = threading.Lock()

    def _refill(self, now: float) -> None:
        """Add tokens proportional to elapsed time (call with lock held)."""
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def acquire(self, path: str, block: bool = True) -> bool:
        """
        Consume one token for the given path.
        If block=True, sleeps until a token is available.
        Returns True when the request may proceed.
        """
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)

                # Per-path minimum gap enforcement
                for prefix, gap in self._min_gaps.items():
                    if path.startswith(prefix):
                        last = self._last_call.get(prefix, 0.0)
                        wait_gap = (last + gap) - now
                        if wait_gap > 0:
                            if not block:
                                return False
                            # Release lock and sleep outside it
                            break
                else:
                    # All gap checks passed — try to consume a token
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        for prefix in self._min_gaps:
                            if path.startswith(prefix):
                                self._last_call[prefix] = now
                        return True
                    elif not block:
                        return False
                    # Need to wait for a token; compute how long
                    wait_token = (1.0 - self._tokens) / self._rate
                    time.sleep(max(0.01, wait_token))
                    continue

            # Gap sleep (released lock already via break)
            time.sleep(max(0.01, wait_gap))


# Global limiter: 28/min burst-6 — safely under the server's 30/min
# Per-path minimum gaps:
#   /blockchain  — poll no more than once every 8 s  (~7/min max)
#   /utxos       — no more than once every 6 s
#   /get_mining_job — no more than once every 4 s (miner fetches new job)
_api_limiter = _RateLimiter(
    tokens_per_min = 28,
    burst          = 6,
    min_gaps       = {
        # /get_mining_job: 6 s minimum — miner fetches a new job after each
        # solve; 6 s gives ~10/min max, well under the 30/min global limit.
        "/get_mining_job": 6.0,
        # /blockchain: only hit by the miner's external-solve poll (every 5 s
        # in gpu_miner every 10 s, in cpu miner every 10 s) and never
        # by background UI timers anymore. 8 s floor keeps two concurrent
        # miners safe.
        "/blockchain":     8.0,
        # /utxos: health-check timer fires every 30 s; no need to enforce a
        # tighter gap here — the timer itself is the throttle.
        "/utxos":          6.0,
    },
)

# Stricter limiter for /add_transaction (server allows only 10/min)
_tx_limiter = _RateLimiter(tokens_per_min=8, burst=2)


def api_get_raw(path: str, timeout: int = 5) -> requests.Response:
    _api_limiter.acquire(path)
    return requests.get(f"{SERVER_URL}{path}", timeout=timeout)


def api_post_binary(path: str, data: bytes, timeout: int = 5) -> requests.Response:
    limiter = _tx_limiter if path.startswith("/add_transaction") else _api_limiter
    limiter.acquire(path)
    return requests.post(
        f"{SERVER_URL}{path}", data=data, headers=_BINARY_HEADERS, timeout=timeout
    )

def divider() -> QFrame:
    f = QFrame()
    f.setObjectName("separator")
    f.setFrameShape(QFrame.HLine)
    return f

def card(parent=None, accent=False) -> QFrame:
    f = QFrame(parent)
    f.setObjectName("card_accent" if accent else "card")
    return f

def label(text: str, obj: str = "", parent=None) -> QLabel:
    lbl = QLabel(text, parent)
    if obj:
        lbl.setObjectName(obj)
    return lbl

def bold_label(text: str, color: str = C["text"]) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{color}; font-weight:700; font-size:13px;")
    return lbl

# ─────────────────────────────────────────────────────────────────────────────
#  WORKER THREADS
# ─────────────────────────────────────────────────────────────────────────────
class MiningWorker(QThread):
    """Mines blocks in a background thread."""
    log_signal      = Signal(str)
    stats_signal    = Signal(dict)      # {"hashrate": float, "nonce": int, "block": int}
    found_signal    = Signal(int, str)  # (block_index, block_hash)

    def __init__(self, address: str, throttle_sleep: float = 0.0):
        super().__init__()
        self._address        = address
        self._throttle_sleep = throttle_sleep   # seconds to sleep per nonce (0 = full speed)
        self._stop_flag      = threading.Event()
        self._blocks_found   = 0
        self._total_hashes   = 0

    def stop(self):
        self._stop_flag.set()

    def run(self):
        self._stop_flag.clear()
        self.log_signal.emit("⛏  Mining started…")

        # Local aliases
        sha256         = hashlib.sha256
        pack_into      = struct.pack_into
        stop_flag      = self._stop_flag
        throttle_sleep = self._throttle_sleep
        retry_delay    = 4      # doubles on repeated server failures, capped at 60 s
        POLL_INTERVAL  = 10.0   # seconds between /blockchain external-solve checks
        STATS_INTERVAL = 2.0    # seconds between UI stats updates (no network call)

        while not stop_flag.is_set():
            # ── Fetch binary mining job ──────────────────────────────────────
            try:
                resp = api_get_raw(
                    f"/get_mining_job?miner_address={self._address}"
                )
                if resp.status_code != 200:
                    self.log_signal.emit(
                        f"✗ Server error {resp.status_code}: "
                        f"{wire.decode_error(resp.content)}"
                    )
                    stop_flag.wait(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                    continue
                job = wire.decode_mining_job(resp.content)
                retry_delay = 4
            except Exception as e:
                self.log_signal.emit(f"✗ Server unreachable: {e}")
                stop_flag.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue

            job_id_raw   = job["job_id_raw"]
            block_index  = job["block_index"]
            target       = job["target"]
            header       = bytearray(job["header_bytes"])
            target_bytes = target.to_bytes(32, "big")

            self.log_signal.emit(
                f"▶ Job #{block_index}"
                f"  target={target_bytes.hex()[:16]}…"
                f"  txs={job['tx_count']}"
            )

            nonce        = 0
            t0           = time.time()
            poll_t       = t0
            stats_t      = t0
            total_hashes = self._total_hashes

            while not stop_flag.is_set():
                # ── Hot loop ─────────────────────────────────────────────────
                pack_into(">Q", header, 104, nonce)
                digest = sha256(sha256(header).digest()).digest()

                total_hashes += 1

                if digest <= target_bytes:
                    # ── Block solved ─────────────────────────────────────────
                    self._total_hashes  = total_hashes
                    self._blocks_found += 1
                    elapsed = time.time() - t0
                    hr      = nonce / max(elapsed, 0.001)
                    bh      = digest.hex()

                    self.log_signal.emit(
                        f"✦ SOLVED block {block_index}!"
                        f"  nonce={nonce:,}  hash={bh[:20]}…"
                    )
                    try:
                        body = wire.encode_submit_block(job_id_raw, bytes(header))
                        res  = api_post_binary("/submit_block", body, timeout=10)
                        if res.status_code == 201:
                            info = wire.decode_submit_block(res.content)
                            self.log_signal.emit(
                                f"  ✓ Accepted: block #{info['block_index']}"
                                f"  hash={info['block_hash'][:20]}…"
                            )
                        else:
                            self.log_signal.emit(
                                f"  ✗ Rejected: {wire.decode_error(res.content)}"
                            )
                    except Exception as e:
                        self.log_signal.emit(f"  Submit error: {e}")

                    self.found_signal.emit(block_index, bh)
                    self.stats_signal.emit({
                        "hashrate":     hr,
                        "nonce":        nonce,
                        "block":        block_index,
                        "blocks_found": self._blocks_found,
                    })
                    time.sleep(0.5)
                    break

                nonce += 1
                now    = time.time()

                # ── Stats every 2 s (no network call) ────────────────────────
                if now - stats_t >= STATS_INTERVAL:
                    stats_t = now
                    elapsed = now - t0
                    hr      = nonce / max(elapsed, 0.001)
                    self.stats_signal.emit({
                        "hashrate":     hr,
                        "nonce":        nonce,
                        "block":        block_index,
                        "blocks_found": self._blocks_found,
                    })

                # ── External-solve check every 10 s (/blockchain) ────────────
                if now - poll_t >= POLL_INTERVAL:
                    poll_t = now
                    try:
                        r = api_get_raw("/blockchain", timeout=4)
                        if r.status_code == 200:
                            chain, _ = wire.decode_blockchain(r.content)
                            if len(chain) > block_index:
                                self.log_signal.emit(
                                    f"⚡ Block {block_index} mined by network — new job"
                                )
                                break
                    except Exception:
                        pass

                if throttle_sleep > 0:
                    time.sleep(throttle_sleep)

            self._total_hashes = total_hashes

        self.log_signal.emit("■  Mining stopped.")



class HealthCheckWorker(QThread):
    """Non-blocking server ping — emits True if node is reachable."""
    result_signal = Signal(bool)

    def run(self):
        try:
            r = requests.get(f"{SERVER_URL}/utxos", timeout=3)
            self.result_signal.emit(r.status_code == 200)
        except Exception:
            self.result_signal.emit(False)


class BalanceWorker(QThread):
    """Fetches UTXOs and calculates balance for a given address."""
    result_signal = Signal(float, list)   # (balance, [utxo_list])
    error_signal  = Signal(str)

    def __init__(self, address: str):
        super().__init__()
        self._address = address

    def run(self):
        try:
            resp = api_get_raw("/utxos")
            if resp.status_code != 200:
                self.error_signal.emit(wire.decode_error(resp.content))
                return
            utxos = wire.decode_utxos(resp.content)
            my_utxos = []
            total = 0.0
            for key, out in utxos.items():
                if out.get("address") == self._address:
                    my_utxos.append({"key": key, **out})
                    total += out.get("amount", 0)
            self.result_signal.emit(total, my_utxos)
        except Exception as e:
            self.error_signal.emit(str(e))


class GpuMiningWorker(QThread):
    """
    Wraps GpuMiner (PyOpenCL) in a QThread and bridges its callbacks
    to Qt signals so the UI can consume them safely.
    """
    log_signal   = Signal(str)
    stats_signal = Signal(dict)      # {"hashrate", "nonce", "block", "blocks_found", "eta"}
    found_signal = Signal(int, str)  # (block_index, block_hash)

    def __init__(self, address: str, inter_batch_sleep: float = 0.0):
        super().__init__()
        self._address           = address
        self._inter_batch_sleep = inter_batch_sleep
        self._miner             = None

    def stop(self):
        if self._miner is not None:
            self._miner.stop()

    def run(self):
        if GpuMiner is None:
            self.log_signal.emit(
                "✗ gpu_miner.py not found or failed to load — "
                "place it next to wallet.py and check pycuda/pyopencl install"
            )
            return
        # GPU_AVAILABLE being False means detect_gpu() found nothing at import
        # time, but GpuMiner itself will try both backends and report clearly.
        # We let it run and fail gracefully rather than blocking here.

        self._miner = GpuMiner(
            address           = self._address,
            server_url        = SERVER_URL,
            batch_size        = 0,   # 0 = auto-calibrate batch size
            inter_batch_sleep = self._inter_batch_sleep,
            on_log            = self.log_signal.emit,
            on_stats          = self.stats_signal.emit,
            on_found          = self.found_signal.emit,
        )
        self._miner.run()
        self.log_signal.emit("■  GPU mining stopped.")


class ExplorerWorker(QThread):
    """Fetches blockchain data for the explorer tab."""
    result_signal = Signal(dict)
    error_signal  = Signal(str)

    def run(self):
        try:
            r_chain = api_get_raw("/blockchain")
            r_utxos = api_get_raw("/utxos")
            if r_chain.status_code != 200:
                self.error_signal.emit(wire.decode_error(r_chain.content))
                return
            if r_utxos.status_code != 200:
                self.error_signal.emit(wire.decode_error(r_utxos.content))
                return
            chain, mempool = wire.decode_blockchain(r_chain.content)
            utxos          = wire.decode_utxos(r_utxos.content)

            # Convert 256-bit difficulty_target ints to hex strings
            # so they survive the PySide6 signal boundary
            for blk in chain:
                tgt = blk.get("difficulty_target", 0)
                blk["difficulty_target"] = hex(tgt) if isinstance(tgt, int) else tgt

            self.result_signal.emit({"chain": chain, "mempool": mempool, "utxos": utxos})
        except Exception as e:
            self.error_signal.emit(str(e))


class LeaderboardWorker(QThread):
    """
    Scans the full chain + UTXO set to build two ranked lists:
      • miners_ranked  — addresses sorted by number of blocks mined (coinbase count)
      • balances_ranked — addresses sorted by total UTXO balance (descending)
    This can be slow on long chains, so it only runs on-demand when the
    Leaderboard tab is opened.
    """
    result_signal = Signal(list, list)   # (miners_ranked, balances_ranked)
    error_signal  = Signal(str)

    def run(self):
        try:
            r_chain = api_get_raw("/blockchain", timeout=20)
            r_utxos = api_get_raw("/utxos",      timeout=10)
            if r_chain.status_code != 200:
                self.error_signal.emit(wire.decode_error(r_chain.content))
                return
            if r_utxos.status_code != 200:
                self.error_signal.emit(wire.decode_error(r_utxos.content))
                return
            chain, _ = wire.decode_blockchain(r_chain.content)
            utxos    = wire.decode_utxos(r_utxos.content)

            # ── Blocks mined: count coinbase outputs per address ──────
            blocks_mined: dict[str, int] = {}
            for block in chain:
                if block.get("index", 0) == 0:
                    continue   # skip genesis block
                for tx in block.get("transactions", []):
                    if str(tx.get("tx_id", "")).startswith("coinbase_"):
                        for out in tx.get("outputs", []):
                            addr = out.get("address", "")
                            if addr:
                                blocks_mined[addr] = blocks_mined.get(addr, 0) + 1

            miners_ranked = sorted(
                [{"address": a, "blocks": b} for a, b in blocks_mined.items()],
                key=lambda x: x["blocks"],
                reverse=True,
            )

            # ── Balance: sum all UTXOs per address ────────────────────
            balances: dict[str, int] = {}
            for _key, out in utxos.items():
                addr = out.get("address", "")
                if addr:
                    balances[addr] = balances.get(addr, 0) + out.get("amount", 0)

            balances_ranked = sorted(
                [{"address": a, "balance": b} for a, b in balances.items()],
                key=lambda x: x["balance"],
                reverse=True,
            )

            self.result_signal.emit(miners_ranked, balances_ranked)

        except Exception as e:
            self.error_signal.emit(str(e))


# ─────────────────────────────────────────────────────────────────────────────
#  WALLET STATE  (keypair + persistence)
# ─────────────────────────────────────────────────────────────────────────────
WALLET_FILE = "wallet.json"

class WalletState:
    def __init__(self):
        self.private_key_hex = ""
        self.public_key_hex  = ""
        self.address         = ""
        self._sk             = None   # cached ecdsa.SigningKey
        self._load()

    def _load(self):
        if os.path.exists(WALLET_FILE):
            try:
                with open(WALLET_FILE) as f:
                    data = json.load(f)
                self.private_key_hex = data["private_key"]
                self.public_key_hex  = data["public_key"]
                self.address         = data["address"]
                # Pre-build the signing key object so sign_tx() is instant
                self._sk = ecdsa.SigningKey.from_string(
                    bytes.fromhex(self.private_key_hex), curve=ecdsa.SECP256k1
                )
                return
            except Exception:
                pass

    def generate(self):
        self._sk             = ecdsa.SigningKey.generate(curve=ecdsa.SECP256k1)
        vk                   = self._sk.get_verifying_key()
        self.private_key_hex = self._sk.to_string().hex()
        self.public_key_hex  = vk.to_string().hex()
        self.address         = get_address(self.public_key_hex)
        self._save()

    def load_from_private(self, priv_hex: str):
        self._sk             = ecdsa.SigningKey.from_string(bytes.fromhex(priv_hex), curve=ecdsa.SECP256k1)
        vk                   = self._sk.get_verifying_key()
        self.private_key_hex = priv_hex
        self.public_key_hex  = vk.to_string().hex()
        self.address         = get_address(self.public_key_hex)
        self._save()

    def _save(self):
        with open(WALLET_FILE, "w") as f:
            json.dump({
                "private_key": self.private_key_hex,
                "public_key":  self.public_key_hex,
                "address":     self.address,
            }, f, indent=2)

    @property
    def loaded(self) -> bool:
        return bool(self.address)

    def sign_tx(self, raw_inputs: list, outputs: list) -> list:
        # Use cached SigningKey — no hex decode on every send
        sanitized = [
            {"tx_id": i["tx_id"], "out_idx": i["out_idx"], "public_key": i["public_key"]}
            for i in raw_inputs
        ]
        msg     = json.dumps({"inputs": sanitized, "outputs": outputs}, sort_keys=True).encode()
        sig_hex = self._sk.sign(msg).hex()
        signed  = []
        for inp in raw_inputs:
            s = inp.copy()
            s["signature"] = sig_hex
            signed.append(s)
        return signed


# ─────────────────────────────────────────────────────────────────────────────
#  REUSABLE UI COMPONENTS
# ─────────────────────────────────────────────────────────────────────────────
class StatusDot(QLabel):
    """Animated status indicator dot."""
    def __init__(self, parent=None):
        super().__init__("●", parent)
        self.setStyleSheet(f"color: {C['text3']}; font-size: 10px;")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._blink)
        self._on = True
        self._active = False

    def set_active(self, active: bool):
        self._active = active
        if active:
            self._timer.start(600)
            self.setStyleSheet(f"color: {C['accent']}; font-size: 10px;")
        else:
            self._timer.stop()
            self.setStyleSheet(f"color: {C['text3']}; font-size: 10px;")
            self.setText("●")

    def _blink(self):
        self._on = not self._on
        color = C["accent"] if self._on else C["bg3"]
        self.setStyleSheet(f"color: {color}; font-size: 10px;")


class CopyableField(QFrame):
    """Read-only field with a copy button."""
    def __init__(self, value: str = "", label_text: str = "", redact: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._value  = value
        self._redact = redact

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        if label_text:
            top = QHBoxLayout()
            lbl = QLabel(label_text)
            lbl.setStyleSheet(f"color:{C['text3']}; font-size:10px; font-weight:600;")
            top.addWidget(lbl)
            top.addStretch()

            if redact:
                self._eye = QPushButton("SHOW")
                self._eye.setObjectName("ghost")
                self._eye.setFixedHeight(20)
                self._eye.clicked.connect(self._toggle_redact)
                top.addWidget(self._eye)

            copy_btn = QPushButton("COPY")
            copy_btn.setObjectName("ghost")
            copy_btn.setFixedHeight(20)
            copy_btn.clicked.connect(self._copy)
            top.addWidget(copy_btn)
            lay.addLayout(top)

        self._display = QLabel()
        self._display.setObjectName("mono")
        self._display.setWordWrap(True)
        self._display.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._display)
        self._refresh_display()

    def setValue(self, val: str):
        self._value = val
        self._refresh_display()

    def _refresh_display(self):
        if self._redact:
            self._display.setText("●" * min(len(self._value), 64))
        else:
            self._display.setText(self._value)

    def _toggle_redact(self):
        self._redact = not self._redact
        self._eye.setText("HIDE" if not self._redact else "SHOW")
        self._refresh_display()

    def _copy(self):
        QApplication.clipboard().setText(self._value)


class StatCard(QFrame):
    """A mini stat card with a big number and a label."""
    def __init__(self, title: str, value: str = "—", color: str = C["accent"], parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)

        self._val_lbl = QLabel(value)
        self._val_lbl.setStyleSheet(
            f"color:{color}; font-size:22px; font-weight:700;"
            f" font-family: 'JetBrains Mono', monospace;"
        )
        ttl = QLabel(title)
        ttl.setStyleSheet(f"color:{C['text3']}; font-size:10px; font-weight:600; letter-spacing:1px;")

        lay.addWidget(self._val_lbl)
        lay.addWidget(ttl)

    def update_value(self, val: str):
        self._val_lbl.setText(val)


# ─────────────────────────────────────────────────────────────────────────────
#  WALLET TAB
# ─────────────────────────────────────────────────────────────────────────────
class WalletTab(QWidget):
    def __init__(self, wallet: WalletState, parent=None):
        super().__init__(parent)
        self.wallet = wallet
        self._balance = 0.0
        self._utxos   = []
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # ── Header ──────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("WALLET")
        title.setObjectName("heading")
        hdr.addWidget(title)
        hdr.addStretch()

        self._status_lbl = QLabel("No wallet loaded")
        self._status_lbl.setStyleSheet(f"color:{C['text3']}; font-size:11px;")
        hdr.addWidget(self._status_lbl)
        root.addLayout(hdr)
        root.addWidget(divider())

        # ── Two-column layout ────────────────────────────────────────
        cols = QHBoxLayout()
        cols.setSpacing(16)

        # ─── LEFT: Identity ─────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(12)

        id_group = QGroupBox("Identity")
        id_lay = QVBoxLayout(id_group)
        id_lay.setSpacing(10)

        self._addr_field = CopyableField(label_text="ADDRESS")
        self._pub_field  = CopyableField(label_text="PUBLIC KEY")
        self._priv_field = CopyableField(label_text="PRIVATE KEY", redact=True)

        id_lay.addWidget(self._addr_field)
        id_lay.addWidget(self._pub_field)
        id_lay.addWidget(self._priv_field)

        # Key management buttons
        btn_row = QHBoxLayout()
        gen_btn = QPushButton("⊕ Generate New Wallet")
        gen_btn.setObjectName("primary")
        gen_btn.clicked.connect(self._generate_wallet)

        import_btn = QPushButton("↓ Import Private Key")
        import_btn.clicked.connect(self._import_wallet)

        btn_row.addWidget(gen_btn)
        btn_row.addWidget(import_btn)
        id_lay.addLayout(btn_row)

        left.addWidget(id_group)
        left.addStretch()

        # ─── RIGHT: Balance + Send ───────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(12)

        bal_group = QGroupBox("Balance")
        bal_lay = QVBoxLayout(bal_group)
        bal_lay.setSpacing(12)

        bal_top = QHBoxLayout()
        self._bal_lbl = QLabel("0.00")
        self._bal_lbl.setStyleSheet(
            f"color:{C['accent']}; font-size:36px; font-weight:700;"
        )
        coin_lbl = QLabel("COIN")
        coin_lbl.setStyleSheet(
            f"color:{C['text3']}; font-size:14px; font-weight:600; margin-top:18px;"
        )
        bal_top.addWidget(self._bal_lbl)
        bal_top.addWidget(coin_lbl)
        bal_top.addStretch()

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setObjectName("ghost")
        refresh_btn.clicked.connect(self._refresh_balance)
        bal_top.addWidget(refresh_btn)
        bal_lay.addLayout(bal_top)

        # UTXO mini-list
        utxo_lbl = QLabel("UTXOs  (auto-selected when you send)")
        utxo_lbl.setStyleSheet(f"color:{C['text3']}; font-size:10px; font-weight:600;")
        bal_lay.addWidget(utxo_lbl)
        self._utxo_list = QListWidget()
        self._utxo_list.setMaximumHeight(110)
        self._utxo_list.setSelectionMode(QListWidget.NoSelection)
        bal_lay.addWidget(self._utxo_list)
        right.addWidget(bal_group)

        # ── Send Transaction ─────────────────────────────────────────
        send_group = QGroupBox("Send Transaction")
        send_lay = QVBoxLayout(send_group)
        send_lay.setSpacing(10)

        self._to_input = QLineEdit()
        self._to_input.setPlaceholderText("Recipient address (64-char hex)")

        self._amt_input = QLineEdit()
        self._amt_input.setPlaceholderText("Amount to send")
        self._amt_input.textChanged.connect(self._preview_coin_selection)

        # ── Coin selection preview ────────────────────────────────────
        preview_hdr = QHBoxLayout()
        prev_lbl = QLabel("COIN SELECTION PREVIEW")
        prev_lbl.setStyleSheet(f"color:{C['text3']}; font-size:10px; font-weight:600;")
        self._preview_refresh = QPushButton("↻")
        self._preview_refresh.setObjectName("ghost")
        self._preview_refresh.setFixedSize(24, 20)
        self._preview_refresh.setToolTip("Re-run selection with latest UTXOs")
        self._preview_refresh.clicked.connect(self._preview_coin_selection)
        preview_hdr.addWidget(prev_lbl)
        preview_hdr.addStretch()
        preview_hdr.addWidget(self._preview_refresh)

        self._preview_box = QTextEdit()
        self._preview_box.setReadOnly(True)
        self._preview_box.setMaximumHeight(90)
        self._preview_box.setStyleSheet(
            f"background:{C['bg']}; color:{C['text3']}; border:1px solid {C['border']};"
            f" border-radius:6px; font-size:10px;"
        )
        self._preview_box.setPlaceholderText("Enter an amount above to preview which UTXOs will be used…")

        send_btn = QPushButton("⇒ Broadcast Transaction")
        send_btn.setObjectName("primary")
        send_btn.clicked.connect(self._send_tx)

        self._tx_status = QLabel("")
        self._tx_status.setWordWrap(True)
        self._tx_status.setStyleSheet(f"color:{C['text2']}; font-size:11px;")

        send_lay.addWidget(self._to_input)
        send_lay.addWidget(self._amt_input)
        send_lay.addLayout(preview_hdr)
        send_lay.addWidget(self._preview_box)
        send_lay.addWidget(send_btn)
        send_lay.addWidget(self._tx_status)
        right.addWidget(send_group)

        cols.addLayout(left, 45)
        cols.addLayout(right, 55)
        root.addLayout(cols)

        self._refresh_display()

    # ── Slots ────────────────────────────────────────────────────────
    def _generate_wallet(self):
        reply = QMessageBox.question(
            self, "Generate Wallet",
            "This will overwrite the existing wallet.\nContinue?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.wallet.generate()
            self._refresh_display()

    def _import_wallet(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Import Private Key")
        dlg.setMinimumWidth(460)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(12)
        lay.addWidget(QLabel("Paste your 64-character hex private key:"))
        inp = QLineEdit()
        inp.setPlaceholderText("Private key hex…")
        lay.addWidget(inp)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() == QDialog.Accepted:
            try:
                self.wallet.load_from_private(inp.text().strip())
                self._refresh_display()
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def _refresh_display(self):
        if self.wallet.loaded:
            self._addr_field.setValue(self.wallet.address)
            self._pub_field.setValue(self.wallet.public_key_hex)
            self._priv_field.setValue(self.wallet.private_key_hex)
            self._status_lbl.setText("Wallet loaded  ✓")
            self._status_lbl.setStyleSheet(f"color:{C['success']}; font-size:11px;")
        else:
            self._addr_field.setValue("—")
            self._pub_field.setValue("—")
            self._priv_field.setValue("—")
            self._status_lbl.setText("No wallet loaded")

    def _refresh_balance(self):
        if not self.wallet.loaded:
            return
        self._bal_lbl.setText("…")
        self._worker = BalanceWorker(self.wallet.address)
        self._worker.result_signal.connect(self._on_balance)
        self._worker.error_signal.connect(self._on_balance_error)
        self._worker.start()

    def _on_balance(self, total: float, utxos: list):
        self._balance = total
        # Sort descending by amount — matches the coin-selection order
        self._utxos = sorted(utxos, key=lambda u: u["amount"], reverse=True)
        self._bal_lbl.setText(f"{total:.2f}")
        self._utxo_list.clear()
        for u in self._utxos:
            item = QListWidgetItem(f"  {truncate(u['key'], 12, 6)}   amount={u['amount']}")
            self._utxo_list.addItem(item)
        if not utxos:
            self._utxo_list.addItem(QListWidgetItem("  (no UTXOs found)"))
        # Refresh the preview if amount is already filled in
        self._preview_coin_selection()

    def _on_balance_error(self, msg: str):
        self._bal_lbl.setText("✗")
        self._bal_lbl.setStyleSheet(f"color:{C['error']}; font-size:36px; font-weight:700;")
        self._tx_status.setText(f"Server error: {msg}")

    # ── Coin selection ────────────────────────────────────────────────

    def _select_utxos(self, amount: float):
        """
        Greedy largest-first coin selection.
        Returns (selected_utxos, total_in, change) or raises ValueError.
        """
        if not self._utxos:
            raise ValueError("No UTXOs available — refresh balance first.")
        selected = []
        total_in = 0.0
        # UTXOs already sorted descending by amount in _on_balance
        for u in self._utxos:
            selected.append(u)
            total_in += u["amount"]
            if total_in >= amount:
                break
        if total_in < amount:
            raise ValueError(
                f"Insufficient funds: have {total_in}, need {amount}"
            )
        change = total_in - amount
        return selected, total_in, change

    def _preview_coin_selection(self):
        """Update the preview box whenever the amount field changes."""
        amt_str = self._amt_input.text().strip()
        if not amt_str:
            self._preview_box.setPlainText("")
            return
        try:
            amount = float(amt_str)
            if amount <= 0:
                raise ValueError
        except ValueError:
            self._preview_box.setPlainText("⚠  Enter a valid positive number")
            return

        if not self._utxos:
            self._preview_box.setPlainText("No UTXOs cached — click ↻ Refresh first")
            return

        try:
            selected, total_in, change = self._select_utxos(amount)
        except ValueError as e:
            self._preview_box.setPlainText(f"⚠  {e}")
            return

        lines = []
        for i, u in enumerate(selected):
            marker = "└" if i == len(selected) - 1 else "├"
            lines.append(f"{marker} {u['key']}  ×{u['amount']}")
        lines.append(f"  ─────────────────────────────")
        lines.append(f"  Total in : {total_in}    Send : {amount}    Change : {change}")
        if change > 0:
            lines.append(f"  Change returned to your own address")
        self._preview_box.setPlainText("\n".join(lines))

    # ── Broadcast ────────────────────────────────────────────────────

    def _send_tx(self):
        if not self.wallet.loaded:
            self._set_tx_status("⚠ Load a wallet first.", error=True)
            return

        to_addr = self._to_input.text().strip()
        amt_str  = self._amt_input.text().strip()

        if not to_addr:
            self._set_tx_status("⚠ Enter a recipient address.", error=True)
            return
        try:
            amount = int(float(amt_str))
            if amount <= 0:
                raise ValueError
        except ValueError:
            self._set_tx_status("⚠ Enter a valid positive amount.", error=True)
            return

        # ── Coin selection ───────────────────────────────────────────
        try:
            selected, total_in, change = self._select_utxos(amount)
        except ValueError as e:
            self._set_tx_status(f"⚠ {e}", error=True)
            return

        # ── Build raw inputs (no signatures yet) ─────────────────────
        raw_inputs = [
            {
                "tx_id":      u["key"].rsplit(":", 1)[0],
                "out_idx":    int(u["key"].rsplit(":", 1)[1]),
                "public_key": self.wallet.public_key_hex,
            }
            for u in selected
        ]

        # ── Build outputs (recipient + optional change) ───────────────
        outputs = [{"address": to_addr, "amount": int(amount)}]
        if change > 0:
            outputs.append({"address": self.wallet.address, "amount": int(change)})

        # ── Sign & broadcast ─────────────────────────────────────────
        try:
            signed_inputs = self.wallet.sign_tx(raw_inputs, outputs)

            # Build tx_id: SHA256 of the canonical JSON of (inputs, outputs)
            tx_data          = {"inputs": signed_inputs, "outputs": outputs}
            tx_bytes         = json.dumps(tx_data, sort_keys=True, ensure_ascii=False).encode()
            calculated_tx_id = hashlib.sha256(tx_bytes).hexdigest()

            tx = {
                "tx_id":   calculated_tx_id,
                "inputs":  signed_inputs,
                "outputs": outputs,
            }

            resp = api_post_binary("/add_transaction", wire.encode_add_transaction(tx))
            if resp.status_code == 201:
                tx_id   = wire.decode_add_transaction_response(resp.content)
                success = True
                msg     = f"Transaction broadcast (tx_id={tx_id[:12]}…)"
            else:
                success = False
                msg     = wire.decode_error(resp.content) if resp.content else f"HTTP {resp.status_code}"

            self._set_tx_status(
                f"✓ {msg}  ({len(selected)} input(s), change={change})" if success else f"✗ {msg}",
                error=not success
            )
            if success:
                # Clear form and refresh balance
                self._to_input.clear()
                self._amt_input.clear()
                self._preview_box.clear()
                self._refresh_balance()
        except Exception as e:
            self._set_tx_status(f"✗ Error: {e}", error=True)

    def _set_tx_status(self, msg: str, error: bool = False):
        color = C["error"] if error else C["success"]
        self._tx_status.setStyleSheet(f"color:{color}; font-size:11px;")
        self._tx_status.setText(msg)


# ─────────────────────────────────────────────────────────────────────────────
#  MINING TAB
# ─────────────────────────────────────────────────────────────────────────────
class MiningTab(QWidget):
    def __init__(self, wallet: WalletState, parent=None):
        super().__init__(parent)
        self.wallet  = wallet
        self._worker = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # ── Header row ───────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("MINING")
        title.setObjectName("heading")
        hdr.addWidget(title)
        hdr.addStretch()
        self._dot = StatusDot()
        hdr.addWidget(self._dot)
        self._state_lbl = QLabel("Idle")
        self._state_lbl.setStyleSheet(f"color:{C['text3']}; font-size:11px;")
        hdr.addWidget(self._state_lbl)
        root.addLayout(hdr)
        root.addWidget(divider())

        # ── Config area ──────────────────────────────────────────────
        cfg = QGroupBox("Configuration")
        cfg_lay = QVBoxLayout(cfg)
        cfg_lay.setSpacing(10)

        # Address row
        addr_row = QHBoxLayout()
        addr_row.addWidget(QLabel("Reward address:"))
        self._addr_input = QLineEdit()
        self._addr_input.setPlaceholderText("Auto-filled from wallet, or paste manually…")
        if self.wallet.loaded:
            self._addr_input.setText(self.wallet.address)
        addr_row.addWidget(self._addr_input, 1)

        self._start_btn = QPushButton("⛏  Start Mining")
        self._start_btn.setObjectName("primary")
        self._start_btn.setMinimumWidth(140)
        self._start_btn.clicked.connect(self._toggle_mining)
        addr_row.addWidget(self._start_btn)
        cfg_lay.addLayout(addr_row)

        cfg_lay.addWidget(divider())

        # Mode + Speed row
        options_row = QHBoxLayout()
        options_row.setSpacing(24)

        # — Miner mode —
        mode_lbl = QLabel("Miner:")
        mode_lbl.setStyleSheet(f"color:{C['text3']}; font-size:11px; font-weight:600;")
        options_row.addWidget(mode_lbl)

        self._mode_group = QButtonGroup(self)
        self._rb_cpu = QRadioButton("CPU")
        self._rb_cpu.setChecked(True)

        # GPU radio — disabled if no GPU detected
        gpu_label = "GPU"
        if GPU_AVAILABLE and _GPU_NAME:
            short_name = _GPU_NAME[:30] + ("…" if len(_GPU_NAME) > 30 else "")
            gpu_label  = f"GPU  ({short_name})"
        self._rb_gpu = QRadioButton(gpu_label)
        self._rb_gpu.setEnabled(GPU_AVAILABLE)
        if not GPU_AVAILABLE:
            tip = "No OpenCL GPU detected" if GpuMiner is not None else "gpu_miner.py not found"
            self._rb_gpu.setToolTip(tip)

        self._mode_group.addButton(self._rb_cpu, 0)
        self._mode_group.addButton(self._rb_gpu, 1)
        options_row.addWidget(self._rb_cpu)
        options_row.addWidget(self._rb_gpu)

        options_row.addSpacing(32)

        # — Speed —
        spd_lbl = QLabel("Speed:")
        spd_lbl.setStyleSheet(f"color:{C['text3']}; font-size:11px; font-weight:600;")
        options_row.addWidget(spd_lbl)

        self._speed_group = QButtonGroup(self)
        self._rb_normal = QRadioButton("Normal")
        self._rb_normal.setChecked(True)
        self._rb_normal.setToolTip(
            "CPU: 100 µs sleep/nonce — keeps PC responsive\n"
            "GPU: 80 ms rest between batches — GPU stays cool"
        )
        self._rb_high = QRadioButton("High Speed")
        self._rb_high.setToolTip(
            "CPU: no sleep — max hashrate, high CPU usage\n"
            "GPU: continuous batches — max hashrate"
        )
        self._speed_group.addButton(self._rb_normal, 0)
        self._speed_group.addButton(self._rb_high,   1)
        options_row.addWidget(self._rb_normal)
        options_row.addWidget(self._rb_high)

        options_row.addStretch()
        cfg_lay.addLayout(options_row)
        root.addWidget(cfg)

        # ── Stat cards ───────────────────────────────────────────────
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)

        self._hr_card     = StatCard("HASHRATE",      "—",  C["accent"])
        self._nonce_card  = StatCard("NONCES TRIED",  "0",  C["accent2"])
        self._block_card  = StatCard("CURRENT BLOCK", "—",  C["text"])
        self._found_card  = StatCard("BLOCKS FOUND",  "0",  C["accent4"])
        self._eta_card    = StatCard("ETA",            "—",  C["text2"])

        for c in (self._hr_card, self._nonce_card, self._block_card,
                  self._found_card, self._eta_card):
            stats_row.addWidget(c)
        root.addLayout(stats_row)

        # ── Progress bar (indeterminate) ─────────────────────────────
        self._prog = QProgressBar()
        self._prog.setRange(0, 0)        # indeterminate
        self._prog.setVisible(False)
        self._prog.setFixedHeight(6)
        root.addWidget(self._prog)

        # ── Log ──────────────────────────────────────────────────────
        log_group = QGroupBox("Mining Log")
        log_lay   = QVBoxLayout(log_group)
        self._log  = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("JetBrains Mono", 10))
        self._log.setMinimumHeight(220)
        self._log.setStyleSheet(
            f"background:{C['bg']}; color:{C['text2']}; border:1px solid {C['border']}; border-radius:6px;"
        )
        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("ghost")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._log.clear)
        log_top = QHBoxLayout()
        log_top.addStretch()
        log_top.addWidget(clear_btn)
        log_lay.addLayout(log_top)
        log_lay.addWidget(self._log)
        root.addWidget(log_group, 1)

    def wallet_updated(self):
        if self.wallet.loaded and not self._addr_input.text():
            self._addr_input.setText(self.wallet.address)

    # ── Toggle mining ────────────────────────────────────────────────
    def _toggle_mining(self):
        if self._worker and self._worker.isRunning():
            self._stop_mining()
        else:
            self._start_mining()

    def _start_mining(self):
        addr = self._addr_input.text().strip()
        if not addr:
            QMessageBox.warning(self, "No Address", "Enter a reward address first.")
            return

        high_speed = self._speed_group.checkedId() == 1   # 1 = High Speed
        use_gpu    = self._mode_group.checkedId()  == 1   # 1 = GPU

        if use_gpu:
            sleep = GPU_SLEEP_HIGH if high_speed else GPU_SLEEP_NORMAL
            self._worker = GpuMiningWorker(addr, inter_batch_sleep=sleep)
            mode_tag = f"GPU  {'[HIGH SPEED]' if high_speed else '[NORMAL]'}"
        else:
            sleep = CPU_SLEEP_HIGH if high_speed else CPU_SLEEP_NORMAL
            self._worker = MiningWorker(addr, throttle_sleep=sleep)
            mode_tag = f"CPU  {'[HIGH SPEED]' if high_speed else '[NORMAL — throttled]'}"

        self._worker.log_signal.connect(self._append_log)
        self._worker.stats_signal.connect(self._update_stats)
        self._worker.found_signal.connect(self._on_found)
        self._worker.start()

        self._append_log(f"  Mode: {mode_tag}")

        self._start_btn.setText("■  Stop Mining")
        self._start_btn.setObjectName("danger")
        self._start_btn.setStyle(self._start_btn.style())
        self._dot.set_active(True)
        self._state_lbl.setText("Mining…")
        self._state_lbl.setStyleSheet(f"color:{C['accent']}; font-size:11px;")
        self._prog.setVisible(True)

        # Lock mode/speed while running
        for w in (self._rb_cpu, self._rb_gpu, self._rb_normal, self._rb_high):
            w.setEnabled(False)

    def _stop_mining(self):
        if self._worker:
            self._worker.stop()
        self._start_btn.setText("⛏  Start Mining")
        self._start_btn.setObjectName("primary")
        self._start_btn.setStyle(self._start_btn.style())
        self._dot.set_active(False)
        self._state_lbl.setText("Idle")
        self._state_lbl.setStyleSheet(f"color:{C['text3']}; font-size:11px;")
        self._prog.setVisible(False)

        # Restore mode/speed controls (GPU only if actually available)
        self._rb_cpu.setEnabled(True)
        self._rb_normal.setEnabled(True)
        self._rb_high.setEnabled(True)
        self._rb_gpu.setEnabled(GPU_AVAILABLE)

    # ── Worker callbacks ─────────────────────────────────────────────
    def _append_log(self, msg: str):
        ts = time.strftime('%H:%M:%S')
        c3 = C["text3"]
        c2 = C["text2"]
        self._log.append(
            f"<span style='color:{c3}'>{ts}</span>"
            f"  <span style='color:{c2}'>{msg}</span>"
        )
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _update_stats(self, stats: dict):
        hr = stats.get("hashrate", 0)
        if hr >= 1_000_000_000:
            hr_str = f"{hr/1_000_000_000:.2f} GH/s"
        elif hr >= 1_000_000:
            hr_str = f"{hr/1_000_000:.2f} MH/s"
        elif hr >= 1_000:
            hr_str = f"{hr/1_000:.2f} kH/s"
        else:
            hr_str = f"{hr:.0f} H/s"

        self._hr_card.update_value(hr_str)
        self._nonce_card.update_value(f"{stats.get('nonce', 0):,}")
        self._block_card.update_value(str(stats.get("block", "—")))
        self._found_card.update_value(str(stats.get("blocks_found", 0)))
        self._eta_card.update_value(stats.get("eta", "—"))

    def _on_found(self, index: int, bh: str):
        ca = C["accent4"]
        self._append_log(f"<span style='color:{ca}'>★ Block {index} confirmed!</span>")

    def closeEvent(self, event):
        self._stop_mining()
        super().closeEvent(event)


# ─────────────────────────────────────────────────────────────────────────────
#  EXPLORER TAB
# ─────────────────────────────────────────────────────────────────────────────
class ExplorerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # ── Header ───────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("EXPLORER")
        title.setObjectName("heading")
        hdr.addWidget(title)
        hdr.addStretch()
        self._last_update = QLabel("Never refreshed")
        self._last_update.setStyleSheet(f"color:{C['text3']}; font-size:10px;")
        hdr.addWidget(self._last_update)
        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setObjectName("ghost")
        refresh_btn.clicked.connect(self.refresh)
        hdr.addWidget(refresh_btn)
        root.addLayout(hdr)
        root.addWidget(divider())

        # ── Summary stat cards ───────────────────────────────────────
        stat_row = QHBoxLayout()
        stat_row.setSpacing(12)
        self._height_card  = StatCard("CHAIN HEIGHT",   "—",  C["accent"])
        self._mempool_card = StatCard("MEMPOOL TXS",    "—",  C["accent2"])
        self._utxo_card    = StatCard("UTXO COUNT",     "—",  C["text"])
        self._diff_card    = StatCard("DIFFICULTY",     "—",  C["accent4"])
        for c in (self._height_card, self._mempool_card, self._utxo_card, self._diff_card):
            stat_row.addWidget(c)
        root.addLayout(stat_row)

        # ── Two-pane: blocks + mempool ───────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; width: 12px; }")

        # Blocks pane
        blk_frame = QFrame()
        blk_frame.setObjectName("card")
        blk_lay = QVBoxLayout(blk_frame)
        blk_lay.setContentsMargins(12, 12, 12, 12)
        blk_hdr = QLabel("RECENT BLOCKS")
        blk_hdr.setStyleSheet(f"color:{C['text3']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        blk_lay.addWidget(blk_hdr)
        self._block_list = QListWidget()
        blk_lay.addWidget(self._block_list)
        splitter.addWidget(blk_frame)

        # Mempool pane
        mp_frame = QFrame()
        mp_frame.setObjectName("card")
        mp_lay = QVBoxLayout(mp_frame)
        mp_lay.setContentsMargins(12, 12, 12, 12)
        mp_hdr = QLabel("MEMPOOL")
        mp_hdr.setStyleSheet(f"color:{C['text3']}; font-size:10px; font-weight:700; letter-spacing:1px;")
        mp_lay.addWidget(mp_hdr)
        self._mempool_list = QListWidget()
        mp_lay.addWidget(self._mempool_list)
        splitter.addWidget(mp_frame)

        splitter.setSizes([600, 300])
        root.addWidget(splitter, 1)

        # ── Block detail area ─────────────────────────────────────────
        detail_group = QGroupBox("Block Detail")
        detail_lay   = QVBoxLayout(detail_group)
        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumHeight(180)
        self._detail.setStyleSheet(
            f"background:{C['bg']}; color:{C['accent2']}; border:1px solid {C['border']}; border-radius:6px; font-size:11px;"
        )
        detail_lay.addWidget(self._detail)
        root.addWidget(detail_group)

        self._block_list.itemClicked.connect(self._show_block_detail)

        # No auto-refresh — triggered by tab switch in MainWindow

    def refresh(self):
        self._last_update.setText("Refreshing…")
        self._worker = ExplorerWorker()
        self._worker.result_signal.connect(self._populate)
        self._worker.error_signal.connect(self._on_error)
        self._worker.start()

    def _populate(self, data: dict):
        chain   = data["chain"]
        mempool = data["mempool"]
        utxos   = data["utxos"]

        self._chain_data = chain   # keep for detail view

        self._height_card.update_value(str(len(chain)))
        self._mempool_card.update_value(str(len(mempool)))
        self._utxo_card.update_value(str(len(utxos)))

        # Difficulty — Bitcoin-style: difficulty = max_target / current_target
        if chain:
            tgt = chain[-1].get("difficulty_target", 0)
            if isinstance(tgt, str):
                tgt = int(tgt, 16)
            self._diff_card.update_value(format_difficulty(tgt))

        # Recent blocks (newest first)
        self._block_list.clear()
        for blk in reversed(chain[-20:]):
            idx  = blk["index"]
            bh   = blk.get("block_hash", "?")
            ts   = blk.get("timestamp", 0)
            ntx  = len(blk.get("transactions", []))
            when = time.strftime("%H:%M:%S", time.localtime(ts))
            item = QListWidgetItem(
                f"  #{idx:>4}   {truncate(bh, 10, 8)}   {ntx} tx   {when}"
            )
            item.setData(Qt.UserRole, idx)
            self._block_list.addItem(item)

        # Mempool
        self._mempool_list.clear()
        if mempool:
            for tx in mempool:
                tid = truncate(tx.get("tx_id", "?"), 8, 6)
                outs = sum(o["amount"] for o in tx.get("outputs", []))
                item = QListWidgetItem(f"  {tid}   out={outs}")
                self._mempool_list.addItem(item)
        else:
            self._mempool_list.addItem(QListWidgetItem("  (mempool empty)"))

        self._last_update.setText(f"Updated {time.strftime('%H:%M:%S')}")

    def _on_error(self, msg: str):
        self._last_update.setText(f"✗ {msg}")
        self._last_update.setStyleSheet(f"color:{C['error']}; font-size:10px;")

    def _show_block_detail(self, item: QListWidgetItem):
            idx = item.data(Qt.UserRole)
            if idx is None or not hasattr(self, "_chain_data"):
                return
            blk = next((b for b in self._chain_data if b["index"] == idx), None)
            if not blk:
                return

            # 1. Create a copy so we don't accidentally ruin the real background data
            blk_display = blk.copy()

            # 2. Grab the target and check if it was mangled into a negative number
            tgt = blk_display.get("difficulty_target")
            if isinstance(tgt, int) and tgt < 0:
                # Reconstruct the massive unsigned 256-bit integer from the overflow
                blk_display["difficulty_target"] = tgt & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF

            # 3. Dump to text using the pure-Python encoder fallback
            pretty = json.dumps(blk_display, indent=2, check_circular=False, ensure_ascii=False)
            self._detail.setPlainText(pretty)


# ─────────────────────────────────────────────────────────────────────────────
#  LEADERBOARD TAB
# ─────────────────────────────────────────────────────────────────────────────
_RANK_ICONS = ["🥇", "🥈", "🥉"]
_RANK_COLORS = [C["accent4"], C["text"], C["accent2"]]   # gold / white / cyan

def _make_rank_item(rank: int, addr: str, value_str: str) -> QListWidgetItem:
    """Build a ranked list row with medal or numeric rank."""
    prefix = _RANK_ICONS[rank] if rank < 3 else f" #{rank + 1:>3}"
    item   = QListWidgetItem(f"  {prefix}   {addr}   {value_str}")
    if rank < 3:
        item.setForeground(QColor(_RANK_COLORS[rank]))
    return item


class LeaderboardTab(QWidget):
    """
    Shows two side-by-side leaderboards:
      Left  — Most Blocks Mined  (miner addresses ranked by coinbase count)
      Right — Highest Balance    (addresses ranked by total UTXO balance)

    Data is fetched lazily — only when this tab is first opened or manually
    refreshed — because scanning the whole chain is resource-intensive.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: LeaderboardWorker | None = None
        self._build()

    # ── UI construction ───────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)

        # Header row ─────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("LEADERBOARD")
        title.setObjectName("heading")
        hdr.addWidget(title)
        hdr.addStretch()

        self._ts_lbl = QLabel("Not yet loaded — open this tab to refresh")
        self._ts_lbl.setStyleSheet(f"color:{C['text3']}; font-size:11px;")
        hdr.addWidget(self._ts_lbl)

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setObjectName("ghost")
        refresh_btn.clicked.connect(self.refresh)
        hdr.addWidget(refresh_btn)
        root.addLayout(hdr)
        root.addWidget(divider())

        # Note about lazy loading ─────────────────────────────────────
        note = QLabel(
            "⚡ Data loads on first visit and on manual refresh — "
            "scanning the full chain is skipped while you're on other tabs."
        )
        note.setStyleSheet(f"color:{C['text3']}; font-size:10px;")
        note.setWordWrap(True)
        root.addWidget(note)

        # Two-column leaderboard panels ───────────────────────────────
        cols = QHBoxLayout()
        cols.setSpacing(16)

        # Left — Blocks Mined
        miners_group = QGroupBox("⛏  Most Blocks Mined")
        miners_lay   = QVBoxLayout(miners_group)
        self._miners_list = QListWidget()
        self._miners_list.setSelectionMode(QListWidget.SingleSelection)
        self._miners_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._miners_list.customContextMenuRequested.connect(
            lambda pos: self._show_copy_menu(self._miners_list, pos)
        )
        self._miners_list.currentItemChanged.connect(self._on_selection_changed)
        miners_lay.addWidget(self._miners_list)
        cols.addWidget(miners_group)

        # Right — Highest Balance
        bal_group  = QGroupBox("◎  Highest Balance")
        bal_lay    = QVBoxLayout(bal_group)
        self._bal_list = QListWidget()
        self._bal_list.setSelectionMode(QListWidget.SingleSelection)
        self._bal_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._bal_list.customContextMenuRequested.connect(
            lambda pos: self._show_copy_menu(self._bal_list, pos)
        )
        self._bal_list.currentItemChanged.connect(self._on_selection_changed)
        bal_lay.addWidget(self._bal_list)
        cols.addWidget(bal_group)

        root.addLayout(cols, 1)

        # Status bar + copy button ────────────────────────────────────
        bottom_row = QHBoxLayout()
        self._status_lbl = QLabel("")
        self._status_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._status_lbl.setStyleSheet(f"color:{C['text2']}; font-size:11px;")
        bottom_row.addWidget(self._status_lbl, 1)

        self._copy_btn = QPushButton("⎘ Copy Address")
        self._copy_btn.setObjectName("ghost")
        self._copy_btn.setEnabled(False)
        self._copy_btn.setToolTip("Select a row first, then click to copy its full address")
        self._copy_btn.clicked.connect(self._copy_selected)
        bottom_row.addWidget(self._copy_btn)
        root.addLayout(bottom_row)

    # ── Public slot — called by MainWindow._on_tab_changed ────────────
    def refresh(self):
        """Kick off a background fetch; silently skip if one is already running."""
        if self._worker and self._worker.isRunning():
            return

        self._status_lbl.setText("⏳  Scanning blockchain and UTXO set…")
        self._ts_lbl.setText("Updating…")

        for lst in (self._miners_list, self._bal_list):
            lst.clear()
            lst.addItem(QListWidgetItem("  Loading…"))

        self._worker = LeaderboardWorker()
        self._worker.result_signal.connect(self._on_result)
        self._worker.error_signal.connect(self._on_error)
        self._worker.start()

    # ── Result handlers ───────────────────────────────────────────────
    def _on_result(self, miners: list, balances: list):
        import time as _time
        self._ts_lbl.setText(f"Last updated  {_time.strftime('%H:%M:%S')}")
        self._status_lbl.setText(
            f"✓  {len(miners)} miner address{'es' if len(miners) != 1 else ''}  ·  "
            f"{len(balances)} balance{'s' if len(balances) != 1 else ''} found"
        )
        self._status_lbl.setStyleSheet(f"color:{C['success']}; font-size:11px;")

        # Populate miners list
        self._miners_list.clear()
        if miners:
            for i, entry in enumerate(miners[:100]):
                blocks = entry["blocks"]
                value_str = f"{blocks} block{'s' if blocks != 1 else ''}"
                item = _make_rank_item(i, truncate(entry["address"], 10, 10), value_str)
                item.setData(Qt.UserRole, entry["address"])
                self._miners_list.addItem(item)
        else:
            self._miners_list.addItem(QListWidgetItem("  No mined blocks found"))

        # Populate balance list
        self._bal_list.clear()
        if balances:
            for i, entry in enumerate(balances[:100]):
                value_str = f"{entry['balance']:.2f} COIN"
                item = _make_rank_item(i, truncate(entry["address"], 10, 10), value_str)
                item.setData(Qt.UserRole, entry["address"])
                self._bal_list.addItem(item)
        else:
            self._bal_list.addItem(QListWidgetItem("  No balances found"))

    def _selected_address(self):
        for lst in (self._miners_list, self._bal_list):
            item = lst.currentItem()
            if item:
                addr = item.data(Qt.UserRole)
                if addr:
                    return addr
        return None

    def _on_selection_changed(self, current, _previous):
        has_addr = (current is not None and current.data(Qt.UserRole) is not None)
        self._copy_btn.setEnabled(has_addr)

    def _copy_selected(self):
        addr = self._selected_address()
        if addr:
            QApplication.clipboard().setText(addr)
            self._status_lbl.setText(f"\u2713  Copied: {addr}")
            self._status_lbl.setStyleSheet(f"color:{C['success']}; font-size:11px;")

    def _show_copy_menu(self, lst, pos):
        from PySide6.QtWidgets import QMenu
        item = lst.itemAt(pos)
        if not item:
            return
        addr = item.data(Qt.UserRole)
        if not addr:
            return
        menu = QMenu(lst)
        act  = menu.addAction("\u2358  Copy address")
        def do_copy():
            QApplication.clipboard().setText(addr)
            self._status_lbl.setText(f"\u2713  Copied: {addr}")
            self._status_lbl.setStyleSheet(f"color:{C['success']}; font-size:11px;")
        act.triggered.connect(do_copy)
        menu.exec(lst.mapToGlobal(pos))

    def _on_error(self, msg: str):
        self._ts_lbl.setText("Update failed")
        self._status_lbl.setText(f"\u2717  {msg}")
        self._status_lbl.setStyleSheet(f"color:{C['error']}; font-size:11px;")
        for lst in (self._miners_list, self._bal_list):
            lst.clear()
            lst.addItem(QListWidgetItem("  Failed to load — check node connection"))


# ─────────────────────────────────────────────────────────────────────────────
#  ABOUT / SETTINGS TAB
# ─────────────────────────────────────────────────────────────────────────────
class AboutTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 30, 30, 30)
        root.setSpacing(20)

        title = QLabel("BLOCKCHAIN WALLET")
        title.setObjectName("heading")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        sub = QLabel("A minimal proof-of-work blockchain desktop client")
        sub.setObjectName("subheading")
        sub.setAlignment(Qt.AlignCenter)
        root.addWidget(sub)

        root.addWidget(divider())

        info_card = QFrame()
        info_card.setObjectName("card")
        info_lay  = QVBoxLayout(info_card)
        info_lay.setSpacing(8)

        rows = [
            ("Server URL",    SERVER_URL),
            ("Block Reward",  str(BLOCK_REWARD)),
            ("Curve",         "secp256k1"),
            ("Address derivation", "SHA-256 of raw public key bytes"),
            ("Hash function", "SHA-256 (block header + transactions)"),
            ("CPU check interval", "Every 10 s (time-based)"),
            ("GPU batch size", f"{GPU_BATCH_SIZE:,} nonces/dispatch"),
            ("GPU",           _GPU_NAME if GPU_AVAILABLE else "Not detected (install pyopencl)"),
            ("Wallet file",   WALLET_FILE),
        ]
        for k, v in rows:
            row = QHBoxLayout()
            kl = QLabel(k)
            kl.setStyleSheet(f"color:{C['text3']}; font-size:11px; min-width:180px;")
            vl = QLabel(v)
            vl.setStyleSheet(f"color:{C['text']}; font-size:11px;")
            row.addWidget(kl)
            row.addWidget(vl, 1)
            info_lay.addLayout(row)

        root.addWidget(info_card)

        # Server URL editor
        srv_group = QGroupBox("Server Connection")
        srv_lay   = QHBoxLayout(srv_group)
        srv_lay.addWidget(QLabel("URL:"))
        self._srv_input = QLineEdit(SERVER_URL)
        srv_lay.addWidget(self._srv_input, 1)
        test_btn = QPushButton("Test")
        test_btn.setObjectName("ghost")
        test_btn.clicked.connect(self._test_connection)
        srv_lay.addWidget(test_btn)
        self._srv_status = QLabel("")
        srv_lay.addWidget(self._srv_status)
        root.addWidget(srv_group)

        root.addStretch()

    def _test_connection(self):
        url = self._srv_input.text().strip()
        try:
            r = requests.get(f"{url}/utxos", timeout=3)
            if r.status_code == 200:
                self._srv_status.setText("✓ Connected")
                self._srv_status.setStyleSheet(f"color:{C['success']}; font-size:11px;")
            else:
                self._srv_status.setText(f"✗ HTTP {r.status_code}")
                self._srv_status.setStyleSheet(f"color:{C['error']}; font-size:11px;")
        except Exception as e:
            self._srv_status.setText(f"✗ {e}")
            self._srv_status.setStyleSheet(f"color:{C['error']}; font-size:11px;")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Blockchain Wallet")
        self.setMinimumSize(960, 700)
        self.resize(1100, 780)

        self.wallet = WalletState()
        self._build()
        self.setStyleSheet(STYLESHEET)

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(f"background:{C['bg']};")

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Top bar ──────────────────────────────────────────────────
        topbar = QFrame()
        topbar.setFixedHeight(52)
        topbar.setStyleSheet(
            f"background:{C['bg2']}; border-bottom: 1px solid {C['border']};"
        )
        tb_lay = QHBoxLayout(topbar)
        tb_lay.setContentsMargins(20, 0, 20, 0)

        logo = QLabel("⬡  CHAIN WALLET")
        logo.setStyleSheet(
            f"color:{C['accent']}; font-size:14px; font-weight:800; letter-spacing:2px;"
        )
        tb_lay.addWidget(logo)
        tb_lay.addStretch()

        self._net_dot   = StatusDot()
        self._net_label = QLabel("Checking…")
        self._net_label.setStyleSheet(f"color:{C['text3']}; font-size:11px;")
        tb_lay.addWidget(self._net_dot)
        tb_lay.addWidget(self._net_label)

        outer.addWidget(topbar)

        # ── Tabs ─────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self._wallet_tab      = WalletTab(self.wallet)
        self._mining_tab      = MiningTab(self.wallet)
        self._explorer_tab    = ExplorerTab()
        self._leaderboard_tab = LeaderboardTab()
        self._about_tab       = AboutTab()

        self.tabs.addTab(self._wallet_tab,      "  Wallet  ")
        self.tabs.addTab(self._mining_tab,      "  Mining  ")
        self.tabs.addTab(self._explorer_tab,    "  Explorer")
        self.tabs.addTab(self._leaderboard_tab, "  Leaderboard")
        self.tabs.addTab(self._about_tab,       "  Settings")

        content = QWidget()
        content.setStyleSheet(f"background:{C['bg']};")
        c_lay = QVBoxLayout(content)
        c_lay.setContentsMargins(16, 12, 16, 16)
        c_lay.addWidget(self.tabs)
        outer.addWidget(content, 1)

        self._health_workers: list = []
        # Health check every 30 s only — keeps background polls sparse so two
        # miners on the same network don’t saturate the 30 req/min budget.
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._check_server)
        self._health_timer.start(30_000)
        self._check_server()   # one ping on startup

        # Tab changes — NO automatic network calls on switch.
        # Explorer/Leaderboard show stale data until the user clicks Refresh.
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Auto-fill mining tab if wallet already loaded
        self._mining_tab.wallet_updated()

    def _on_tab_changed(self, idx: int):
        # Deliberately empty — no auto-refresh on tab switch.
        pass

    def _check_server(self):
        """Spin up a background ping so the main thread is never blocked."""
        # Prune finished workers, then launch a new one
        self._health_workers = [w for w in self._health_workers if w.isRunning()]
        worker = HealthCheckWorker()
        worker.result_signal.connect(self._on_health_result)
        self._health_workers.append(worker)
        worker.start()

    def _on_health_result(self, online: bool):
        if online:
            self._net_dot.set_active(True)
            self._net_label.setText("Node online")
            self._net_label.setStyleSheet(f"color:{C['success']}; font-size:11px;")
        else:
            self._net_dot.set_active(False)
            self._net_label.setText("Node offline")
            self._net_label.setStyleSheet(f"color:{C['error']}; font-size:11px;")

    def closeEvent(self, event):
        self._mining_tab._stop_mining()
        super().closeEvent(event)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Chain Wallet")
    app.setOrganizationName("BlockchainDev")

    # High-DPI support
    app.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
