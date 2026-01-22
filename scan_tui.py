#!/usr/bin/env python3
"""Textual-based TUI for scanning with SANE (scanimage)."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import site
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from time import monotonic

TEXTUAL_REQUIREMENT = "textual>=0.52"
CONFIG_PATH = Path.home() / ".config" / "scan_tui" / "config.json"
HISTORY_PATH = Path.home() / ".config" / "scan_tui" / "history.jsonl"
RESOLUTION_PRESETS = [150, 300, 600]
FORMAT_OPTIONS = ["png", "jpeg", "tiff", "pdf", "pnm"]
MODE_OPTIONS = ["", "Color", "Gray", "Lineart"]
SOURCE_OPTIONS = ["", "Flatbed", "ADF", "ADF Duplex"]


def _install_textual() -> None:
    attempts = [
        [sys.executable, "-m", "pip", "install", "--user", TEXTUAL_REQUIREMENT],
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--user",
            "--break-system-packages",
            TEXTUAL_REQUIREMENT,
        ],
        [sys.executable, "-m", "pip", "install", "--break-system-packages", TEXTUAL_REQUIREMENT],
    ]
    last_error = None
    for cmd in attempts:
        try:
            print(f"[scan_tui] Installing dependency: {TEXTUAL_REQUIREMENT}")
            subprocess.run(cmd, check=True)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    print(
        "[scan_tui] Failed to install textual. "
        "Install it via your package manager or pip.",
        file=sys.stderr,
    )
    if last_error:
        print(f"[scan_tui] Last error: {last_error}", file=sys.stderr)
    sys.exit(1)


def _ensure_user_site_on_path() -> None:
    user_site = site.getusersitepackages()
    if not user_site:
        return
    if user_site in sys.path:
        return
    if Path(user_site).exists():
        site.addsitedir(user_site)
        sys.path.insert(0, user_site)


try:
    _ensure_user_site_on_path()
    import textual  # noqa: F401
except ModuleNotFoundError:
    _install_textual()
    importlib.invalidate_caches()
    _ensure_user_site_on_path()
    import textual  # noqa: F401

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    RichLog,
    Select,
    Static,
)


@dataclass(frozen=True)
class ScannerInfo:
    device: str
    name: str
    raw: str


SCAN_LINE_RE = re.compile(r"device\s+(.+?)\s+is\s+(.+)")


def parse_scanimage_list(output: str) -> List[ScannerInfo]:
    scanners: List[ScannerInfo] = []
    seen = set()
    for line in output.splitlines():
        match = SCAN_LINE_RE.search(line)
        if not match:
            continue
        device_raw, name = match.group(1).strip(), match.group(2).strip()
        device = device_raw.strip("`'\"")
        if not device:
            continue
        if device in seen:
            continue
        seen.add(device)
        scanners.append(ScannerInfo(device=device, name=name, raw=line.strip()))
    return scanners


def short_device(device: str) -> str:
    if len(device) <= 32:
        return device
    return f"{device[:14]}…{device[-14:]}"


def safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def sanitize_prefix(value: str) -> str:
    cleaned = value.strip().replace(" ", "_")
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned)
    cleaned = cleaned.strip("._-")
    return cleaned


def is_date_dir(name: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", name))


def next_index(prefix: str, output_dir: Path, ext: str) -> int:
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d{{4}}).*\.{re.escape(ext)}$", re.IGNORECASE)
    max_idx = 0
    if not output_dir.exists():
        return 1
    for entry in output_dir.iterdir():
        if not entry.is_file():
            continue
        match = pattern.match(entry.name)
        if match:
            try:
                max_idx = max(max_idx, int(match.group(1)))
            except ValueError:
                continue
    return max_idx + 1


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def append_history(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False)
        handle.write("\n")


class ScanTUI(App):
    CSS = """
    Screen {
        background: #1a1c1f;
        color: #e6e4df;
    }

    #main {
        height: 1fr;
        padding: 1 2;
    }

    #select_panel, #scan_panel {
        width: 1fr;
        min-width: 40;
    }

    #select_panel {
        border: round #3b3f46;
        padding: 1 2;
    }

    #scan_panel {
        border: round #3b3f46;
        padding: 1 2;
    }

    .section-title {
        text-style: bold;
        color: #f2b705;
        padding-bottom: 1;
    }

    .field-label {
        color: #9aa2ad;
        padding-top: 1;
    }

    Input, Select {
        width: 100%;
        background: #23262b;
        border: round #2f343a;
    }

    #select_actions, #scan_actions {
        height: auto;
        padding-top: 1;
    }

    #select_actions Button, #scan_actions Button {
        width: 1fr;
        margin-right: 1;
    }

    #advanced_panel {
        margin-top: 1;
    }

    #advanced_hint {
        color: #9aa2ad;
        padding-top: 1;
    }

    #log {
        border: round #2f343a;
        height: 12;
        padding: 1 1;
        margin-top: 1;
    }

    #status_bar {
        height: auto;
        padding-top: 1;
    }

    #spinner {
        margin-left: 1;
        height: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_scanners", "Refresh Scanners"),
        ("c", "clear_log", "Clear Log"),
        ("b", "back", "Back"),
        ("a", "toggle_advanced", "Toggle Advanced"),
        ("p", "focus_prefix", "Focus Prefix"),
        ("s", "focus_scan", "Focus Scan"),
        ("l", "focus_log", "Focus Log"),
        ("f5", "refresh_scanners", "Refresh"),
        ("t", "set_date_prefix", "Date Prefix"),
        ("g", "toggle_gray", "Toggle Gray"),
        ("d", "cycle_resolution", "Cycle DPI"),
        ("o", "toggle_source", "Toggle Source"),
        ("m", "toggle_format", "Toggle Format"),
        ("1", "preset_doc", "Preset Doc"),
        ("2", "preset_photo", "Preset Photo"),
        ("3", "preset_draft", "Preset Draft"),
        ("v", "open_last", "Open Last"),
        ("u", "toggle_auto_continue", "Auto Continue"),
        ("e", "open_output_dir", "Open Output Dir"),
        ("x", "clear_last_error", "Clear Error"),
        ("y", "set_date_dir", "Date Dir"),
        ("h", "show_help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._scanners: List[ScannerInfo] = []
        self._scan_lock = asyncio.Lock()
        self._stage: str = "select"
        self._advanced: bool = False
        self._settings = self._load_settings()
        self._last_saved: Optional[Path] = None
        self._session_scans: int = 0
        self._last_scan_seconds: Optional[float] = None
        self._scan_queued: bool = False
        self._session_bytes: int = 0
        self._session_total_seconds: float = 0.0
        self._auto_continue_single: bool = bool(self._settings.get("auto_continue_single", True))
        self._last_error: Optional[str] = None
        self._last_scan_time: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="select_panel"):
                yield Static("Scanner", classes="section-title")
                yield Label("Available devices", classes="field-label")
                yield Select(options=[], id="scanner_select")
                with Horizontal(id="select_actions"):
                    yield Button("Refresh", id="refresh", variant="primary")
                    yield Button("Continue", id="continue_button", variant="success")
                yield Label("Select a scanner to continue.", id="select_status")
                yield Label("Auto-continue: On (U)", id="auto_continue_label")
            with Vertical(id="scan_panel"):
                yield Static("Scan Settings", classes="section-title")
                yield Label("Selected scanner", classes="field-label")
                yield Static("-", id="selected_scanner")
                yield Label("File name prefix", classes="field-label")
                yield Input(value="scan", id="prefix_input", placeholder="scan")
                yield Label("Output directory", classes="field-label")
                yield Input(value="./scans", id="output_dir_input", placeholder="./scans")
                yield Label("Free space", classes="field-label")
                yield Static("-", id="free_space")
                yield Label("Ready", classes="field-label")
                yield Static("Place next page and press Space.", id="ready_label")
                yield Label("Last saved", classes="field-label")
                yield Static("-", id="last_saved")
                yield Label("Session scans", classes="field-label")
                yield Static("0", id="session_count")
                yield Label("Session total", classes="field-label")
                yield Static("0.0 B", id="session_total")
                yield Label("Avg time", classes="field-label")
                yield Static("-", id="session_avg")
                yield Label("Last scan", classes="field-label")
                yield Static("-", id="last_scan_info")
                yield Label("Last scan time", classes="field-label")
                yield Static("-", id="last_scan_time")
                yield Label("Last error", classes="field-label")
                yield Static("-", id="last_error")
                yield Label("Next file", classes="field-label")
                yield Static("-", id="next_file")
                yield Label("Advanced options hidden (press A)", id="advanced_hint")
                with Vertical(id="advanced_panel"):
                    yield Label("Format", classes="field-label")
                    yield Select(
                        options=[(value.upper(), value) for value in FORMAT_OPTIONS],
                        id="format_select",
                        value="png",
                    )
                    yield Label("Resolution (DPI)", classes="field-label")
                    yield Input(value="300", id="resolution_input", placeholder="300")
                    yield Label("Mode", classes="field-label")
                    yield Select(
                        options=[("Default", "")] + [(value, value) for value in MODE_OPTIONS if value],
                        id="mode_select",
                        value="Color",
                    )
                    yield Label("Source", classes="field-label")
                    yield Select(
                        options=[("Default", "")] + [(value, value) for value in SOURCE_OPTIONS if value],
                        id="source_select",
                        value="Flatbed",
                    )
                    yield Label("Extra scanimage options", classes="field-label")
                    yield Input(
                        id="extra_input",
                        placeholder="e.g. --brightness 10 --contrast 5",
                    )
                with Horizontal(id="scan_actions"):
                    yield Button("Scan (Space)", id="scan_button", variant="success")
                    yield Button("Advanced", id="advanced_button", variant="default")
                    yield Button("Back", id="back_button", variant="default")
                    yield Button("Reset Session", id="reset_count", variant="default")
                    yield Button("Clear Log", id="clear_log", variant="default")
                with Horizontal(id="status_bar"):
                    yield Label("Idle", id="status_label")
                    yield LoadingIndicator(id="spinner")
                    yield Label("H help  ↑/↓ focus  Enter/Space scan  P prefix  T date  Y dir  1/2/3 presets  G gray  D dpi  O source  M format  V view  E dir  X clear err  S scan  L log", id="hint_label")
                yield RichLog(id="log", highlight=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#spinner", LoadingIndicator).display = False
        log = self.query_one("#log", RichLog)
        try:
            log.can_focus = True
        except Exception:
            pass
        self._apply_scan_settings()
        self._set_advanced(self._advanced)
        self._set_stage("select")
        await self.action_refresh_scanners()

    def log_message(self, message: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(message)
        try:
            log.scroll_end(animate=False)
        except Exception:
            pass

    def set_last_error(self, message: str) -> None:
        self._last_error = message
        label = self.query_one("#last_error", Static)
        label.update(message)
        label.styles.color = "red" if message and message != "-" else "#9aa2ad"
        if message and message != "-":
            self.log_message(f"[red]Last error:[/red] {message}")

    def _update_session_stats(self) -> None:
        self.query_one("#session_count", Static).update(str(self._session_scans))
        self.query_one("#session_total", Static).update(format_bytes(self._session_bytes))
        if self._session_scans:
            avg = self._session_total_seconds / self._session_scans
            self.query_one("#session_avg", Static).update(f"{avg:.2f}s")
        else:
            self.query_one("#session_avg", Static).update("-")

    def active_device(self) -> Optional[str]:
        select = self.query_one("#scanner_select", Select)
        value = select.value
        if not value:
            return None
        if value is Select.BLANK:
            return None
        if value.__class__.__name__ == "NoSelection":
            return None
        return str(value)

    def set_status(self, message: str, busy: bool = False) -> None:
        self.query_one("#status_label", Label).update(message)
        spinner = self.query_one("#spinner", LoadingIndicator)
        spinner.display = busy

    def set_ready_message(self, message: str) -> None:
        self.query_one("#ready_label", Static).update(message)

    def set_select_status(self, message: str) -> None:
        self.query_one("#select_status", Label).update(message)
        auto_label = "On" if self._auto_continue_single else "Off"
        self.query_one("#auto_continue_label", Label).update(f"Auto-continue: {auto_label} (U)")

    def _set_stage(self, stage: str) -> None:
        self._stage = stage
        is_select = stage == "select"
        select_panel = self.query_one("#select_panel", Vertical)
        scan_panel = self.query_one("#scan_panel", Vertical)
        select_panel.styles.display = "block" if is_select else "none"
        scan_panel.styles.display = "none" if is_select else "block"
        if is_select:
            self.set_status("Select a scanner", busy=False)
            self.set_select_status("Select a scanner to continue.")
            try:
                self.query_one("#scanner_select", Select).focus()
            except Exception:
                pass
        else:
            self._set_advanced(self._advanced)
            self._update_scanner_detail(self.active_device())
            self._update_next_filename()
            self.set_ready_message("Place next page and press Space.")
            if self._last_saved:
                self.query_one("#last_saved", Static).update(str(self._last_saved))
            self._update_session_stats()
            if self._last_scan_seconds is not None:
                self.query_one("#last_scan_info", Static).update(f"{self._last_scan_seconds:.2f}s")
            if self._last_scan_time:
                self.query_one("#last_scan_time", Static).update(self._last_scan_time)
            if self._last_error:
                self.query_one("#last_error", Static).update(self._last_error)
            try:
                self.query_one("#scan_button", Button).focus()
            except Exception:
                pass

    def _set_advanced(self, enabled: bool) -> None:
        self._advanced = enabled
        panel = self.query_one("#advanced_panel", Vertical)
        hint = self.query_one("#advanced_hint", Label)
        button = self.query_one("#advanced_button", Button)
        panel.styles.display = "block" if enabled else "none"
        hint.styles.display = "none" if enabled else "block"
        button.label = "Simple" if enabled else "Advanced"

    def _set_select_options(self, options: Iterable[Tuple[str, str]]) -> None:
        select = self.query_one("#scanner_select", Select)
        options_list = list(options)
        try:
            select.set_options(options_list)
        except AttributeError:
            select.options = options_list
        if options_list:
            if not select.value:
                select.value = options_list[0][1]
        else:
            try:
                select.clear()
            except AttributeError:
                # Fallback for older Textual versions
                select.value = options_list[0][1] if options_list else ""

    async def action_refresh_scanners(self) -> None:
        self.set_status("Refreshing scanners…", busy=True)
        self.set_select_status("Scanning for devices…")
        self.log_message("[bold]Scanning for devices…[/bold]")
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["scanimage", "-L"],
                capture_output=True,
                text=True,
                timeout=12,
            )
        except FileNotFoundError:
            self.set_status("scanimage not found", busy=False)
            self.set_select_status("scanimage not found.")
            self.log_message("[red]Error:[/red] scanimage not found. Install SANE tools.")
            return
        except subprocess.TimeoutExpired:
            self.set_status("Scan timed out", busy=False)
            self.set_select_status("scanimage -L timed out.")
            self.log_message("[red]Error:[/red] scanimage -L timed out.")
            return

        if result.returncode != 0:
            self.set_status("Scan failed", busy=False)
            self.set_select_status("scanimage -L failed.")
            self.log_message(f"[red]scanimage error:[/red] {result.stderr.strip() or result.stdout.strip()}")
            return

        scanners = parse_scanimage_list(result.stdout)
        self._scanners = scanners
        if not scanners:
            self._set_select_options([])
            self.query_one("#selected_scanner", Static).update("-")
            self.set_status("No scanners found", busy=False)
            self.set_select_status("No scanners found.")
            self.log_message("[yellow]No scanners detected.[/yellow]")
            return

        options = [(f"{s.name} [{short_device(s.device)}]", s.device) for s in scanners]
        self._set_select_options(options)
        preferred = self._settings.get("last_device")
        if preferred and any(s.device == preferred for s in scanners):
            self.query_one("#scanner_select", Select).value = preferred
        self.set_status(f"Found {len(scanners)} scanner(s)", busy=False)
        self.set_select_status(f"Found {len(scanners)} scanner(s).")
        self._update_scanner_detail(self.active_device())
        if self._auto_continue_single and len(scanners) == 1 and self._stage == "select":
            self._set_stage("scan")

    def _update_scanner_detail(self, device: Optional[str]) -> None:
        detail = self.query_one("#selected_scanner", Static)
        if not device:
            detail.update("-")
            return
        scanner = next((s for s in self._scanners if s.device == device), None)
        if not scanner:
            detail.update(device)
            return
        detail.update(f"{scanner.name}\n{scanner.device}")

    async def action_scan(self) -> None:
        if self._stage != "scan":
            return
        focused = self.focused
        if isinstance(focused, Input):
            return
        if self._scan_lock.locked():
            if not self._scan_queued:
                self._scan_queued = True
                self.set_ready_message("Scan queued. Will scan next page after current.")
                self.log_message("[yellow]Queued one scan.[/yellow]")
            else:
                self.log_message("[yellow]Scan already queued.[/yellow]")
            return
        async with self._scan_lock:
            while True:
                ok = await self._run_scan()
                if self._scan_queued and ok:
                    self._scan_queued = False
                    self.set_ready_message("Queued scan starting…")
                    continue
                self._scan_queued = False
                break

    async def _run_scan(self) -> bool:
        device = self.active_device()
        if not device:
            self.log_message("[red]Select a scanner first.[/red]")
            self.set_last_error("No scanner selected")
            return False

        prefix_input = self.query_one("#prefix_input", Input)
        prefix = sanitize_prefix(prefix_input.value)
        if prefix != prefix_input.value:
            prefix_input.value = prefix
            self.log_message("[yellow]Prefix sanitized.[/yellow]")
        if not prefix:
            self.log_message("[red]Prefix cannot be empty.[/red]")
            self.set_last_error("Prefix empty")
            return False

        output_dir = self._ensure_output_dir()
        if output_dir is None:
            self.set_last_error("Output directory error")
            return False

        fmt = (self.query_one("#format_select", Select).value or "png").lower()
        ext = "jpg" if fmt == "jpeg" else fmt
        resolution = safe_int(self.query_one("#resolution_input", Input).value.strip(), 300)
        mode = self.query_one("#mode_select", Select).value or ""
        source = self.query_one("#source_select", Select).value or ""
        extra = self.query_one("#extra_input", Input).value.strip()

        index = next_index(prefix, output_dir, ext)
        filename = output_dir / f"{prefix}_{index:04d}.{ext}"

        cmd = ["scanimage", "-d", device, "--format", fmt, "--output-file", str(filename)]
        if resolution:
            cmd += ["--resolution", str(resolution)]
        if mode:
            cmd += ["--mode", mode]
        if source:
            cmd += ["--source", source]
        if extra:
            try:
                cmd += shlex.split(extra)
            except ValueError as exc:
                self.log_message(f"[red]Extra options parse error:[/red] {exc}")
                self.set_last_error("Extra options parse error")
                return False

        self.set_status(f"Scanning {filename.name}…", busy=True)
        self.log_message(f"[cyan]Scanning[/cyan] {filename.name} on {short_device(device)}")
        started = monotonic()
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            self.set_status("Scan timed out", busy=False)
            self.log_message("[red]Scan timed out.[/red]")
            self.set_last_error("Scan timed out")
            return False

        if result.returncode != 0:
            self.set_status("Scan failed", busy=False)
            error = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            self.log_message(f"[red]scanimage failed:[/red] {error}")
            self.set_last_error("Scan failed")
            return False

        self.set_status("Scan complete", busy=False)
        duration = monotonic() - started
        self._last_scan_seconds = duration
        size_info = ""
        size_bytes = None
        try:
            size_bytes = filename.stat().st_size
            size_info = f" ({format_bytes(size_bytes)})"
        except Exception:
            size_info = ""
        self.log_message(f"[green]Saved:[/green] {filename}{size_info} in {duration:.2f}s")
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
        self._last_saved = filename
        self.query_one("#last_saved", Static).update(f"{filename}{size_info}")
        self.query_one("#last_scan_info", Static).update(f"{duration:.2f}s{size_info}")
        self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.query_one("#last_scan_time", Static).update(self._last_scan_time)
        self.set_last_error("-")
        self.set_ready_message("Ready for next page. Press Space.")
        self._session_scans += 1
        if size_bytes is not None:
            self._session_bytes += size_bytes
        self._session_total_seconds += duration
        self._update_session_stats()
        self._append_history_entry(
            filename=filename,
            size_bytes=size_bytes,
            duration=duration,
        )
        self._save_settings()
        self._update_next_filename()
        return True

    async def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()
        self.log_message("Log cleared.")

    async def action_back(self) -> None:
        self._set_stage("select")

    async def action_toggle_advanced(self) -> None:
        if self._stage != "scan":
            return
        self._set_advanced(not self._advanced)

    async def action_reset_count(self) -> None:
        self._session_scans = 0
        self._session_bytes = 0
        self._session_total_seconds = 0.0
        self._update_session_stats()

    async def action_focus_prefix(self) -> None:
        if self._stage != "scan":
            return
        self.query_one("#prefix_input", Input).focus()

    async def action_focus_scan(self) -> None:
        if self._stage != "scan":
            return
        self.query_one("#scan_button", Button).focus()

    async def action_focus_log(self) -> None:
        if self._stage != "scan":
            return
        self.query_one("#log", RichLog).focus()

    async def action_set_date_prefix(self) -> None:
        if self._stage != "scan":
            return
        prefix = datetime.now().strftime("%Y%m%d")
        self.query_one("#prefix_input", Input).value = prefix
        self._update_next_filename()
        self._save_settings()

    async def action_toggle_gray(self) -> None:
        if self._stage != "scan":
            return
        mode_select = self.query_one("#mode_select", Select)
        current = (mode_select.value or "").lower()
        new_mode = "Gray" if current != "gray" else "Color"
        mode_select.value = new_mode
        self._save_settings()
        self.log_message(f"[blue]Mode:[/blue] {new_mode}")

    async def action_cycle_resolution(self) -> None:
        if self._stage != "scan":
            return
        input_widget = self.query_one("#resolution_input", Input)
        current = safe_int(input_widget.value.strip(), RESOLUTION_PRESETS[0])
        if current in RESOLUTION_PRESETS:
            idx = RESOLUTION_PRESETS.index(current)
            new_value = RESOLUTION_PRESETS[(idx + 1) % len(RESOLUTION_PRESETS)]
        else:
            new_value = RESOLUTION_PRESETS[0]
        input_widget.value = str(new_value)
        self._save_settings()
        self.log_message(f"[blue]Resolution:[/blue] {new_value} DPI")

    async def action_toggle_source(self) -> None:
        if self._stage != "scan":
            return
        source_select = self.query_one("#source_select", Select)
        options = SOURCE_OPTIONS
        current = source_select.value or ""
        if current in options:
            idx = options.index(current)
            new_value = options[(idx + 1) % len(options)]
        else:
            new_value = options[0] if options else ""
        source_select.value = new_value
        self._save_settings()
        label = new_value if new_value else "Default"
        self.log_message(f"[blue]Source:[/blue] {label}")

    async def action_toggle_format(self) -> None:
        if self._stage != "scan":
            return
        format_select = self.query_one("#format_select", Select)
        options = FORMAT_OPTIONS
        current = format_select.value or ""
        if current in options:
            idx = options.index(current)
            new_value = options[(idx + 1) % len(options)]
        else:
            new_value = options[0] if options else "png"
        format_select.value = new_value
        self._update_next_filename()
        self._save_settings()
        self.log_message(f"[blue]Format:[/blue] {new_value.upper()}")

    async def action_open_last(self) -> None:
        if self._stage != "scan":
            return
        if not self._last_saved or not self._last_saved.exists():
            self.log_message("[yellow]No last scan to open.[/yellow]")
            return
        try:
            await asyncio.to_thread(subprocess.run, ["xdg-open", str(self._last_saved)], check=False)
            self.log_message(f"[green]Opened:[/green] {self._last_saved}")
        except FileNotFoundError:
            self.log_message("[red]xdg-open not found.[/red]")
        except Exception as exc:
            self.log_message(f"[red]Failed to open file:[/red] {exc}")

    async def action_open_output_dir(self) -> None:
        if self._stage != "scan":
            return
        output_dir = self._output_dir_path()
        if not output_dir:
            self.log_message("[yellow]Output directory not set.[/yellow]")
            return
        try:
            await asyncio.to_thread(subprocess.run, ["xdg-open", str(output_dir)], check=False)
            self.log_message(f"[green]Opened:[/green] {output_dir}")
        except FileNotFoundError:
            self.log_message("[red]xdg-open not found.[/red]")
        except Exception as exc:
            self.log_message(f"[red]Failed to open dir:[/red] {exc}")

    async def action_toggle_auto_continue(self) -> None:
        self._auto_continue_single = not self._auto_continue_single
        self._save_settings()
        self.set_select_status("Select a scanner to continue.")
        state = "On" if self._auto_continue_single else "Off"
        self.log_message(f"[blue]Auto-continue:[/blue] {state}")

    async def action_clear_last_error(self) -> None:
        self.set_last_error("-")

    async def action_set_date_dir(self) -> None:
        if self._stage != "scan":
            return
        output_input = self.query_one("#output_dir_input", Input)
        base_path = Path(output_input.value.strip() or "./scans").expanduser()
        if is_date_dir(base_path.name):
            base_path = base_path.parent
        dated = base_path / datetime.now().strftime("%Y-%m-%d")
        output_input.value = str(dated)
        self._update_free_space()
        self._update_next_filename()
        self._save_settings()
        self.log_message(f"[blue]Output dir:[/blue] {dated}")

    async def action_show_help(self) -> None:
        if self._stage != "scan":
            return
        self.log_message("[bold]Keyboard cheatsheet[/bold]")
        self.log_message("Space/Enter: Scan   P: Prefix   T: Date prefix   Y: Date dir")
        self.log_message("1: Doc preset  2: Photo preset  3: Draft preset")
        self.log_message("G: Gray toggle  D: DPI cycle  O: Source cycle  M: Format cycle")
        self.log_message("V: Open last  E: Open dir  X: Clear error  U: Auto-continue")

    def _apply_preset(self, label: str, resolution: int, mode: str, fmt: str) -> None:
        if self._focus_is_inputlike():
            return
        self.query_one("#resolution_input", Input).value = str(resolution)
        self.query_one("#mode_select", Select).value = mode
        self.query_one("#format_select", Select).value = fmt
        self._update_next_filename()
        self._save_settings()
        self.log_message(f"[magenta]Preset:[/magenta] {label}")

    async def action_preset_doc(self) -> None:
        if self._stage != "scan":
            return
        self._apply_preset("Doc (300dpi Gray PNG)", 300, "Gray", "png")

    async def action_preset_photo(self) -> None:
        if self._stage != "scan":
            return
        self._apply_preset("Photo (600dpi Color PNG)", 600, "Color", "png")

    async def action_preset_draft(self) -> None:
        if self._stage != "scan":
            return
        self._apply_preset("Draft (150dpi Gray PNG)", 150, "Gray", "png")

    def _append_history_entry(self, filename: Path, size_bytes: Optional[int], duration: float) -> None:
        try:
            record = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "file": str(filename),
                "size_bytes": size_bytes,
                "duration_seconds": round(duration, 3),
                "device": self.active_device(),
                "format": self.query_one("#format_select", Select).value or "png",
                "resolution": self.query_one("#resolution_input", Input).value.strip(),
                "mode": self.query_one("#mode_select", Select).value or "",
                "source": self.query_one("#source_select", Select).value or "",
            }
            append_history(HISTORY_PATH, record)
        except Exception:
            return

    def _load_settings(self) -> dict:
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _save_settings(self) -> None:
        data = {
            "prefix": self.query_one("#prefix_input", Input).value.strip(),
            "output_dir": self.query_one("#output_dir_input", Input).value.strip(),
            "format": self.query_one("#format_select", Select).value or "png",
            "resolution": self.query_one("#resolution_input", Input).value.strip(),
            "mode": self.query_one("#mode_select", Select).value or "",
            "source": self.query_one("#source_select", Select).value or "",
            "extra": self.query_one("#extra_input", Input).value.strip(),
            "last_device": self.active_device(),
            "advanced": self._advanced,
            "auto_continue_single": self._auto_continue_single,
        }
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with CONFIG_PATH.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
        except Exception:
            return

    def _apply_scan_settings(self) -> None:
        settings = self._settings
        self.query_one("#prefix_input", Input).value = settings.get("prefix", "scan")
        self.query_one("#output_dir_input", Input).value = settings.get("output_dir", "./scans")
        self.query_one("#format_select", Select).value = settings.get("format", "png")
        self.query_one("#resolution_input", Input).value = settings.get("resolution", "300")
        self.query_one("#mode_select", Select).value = settings.get("mode", "Color")
        self.query_one("#source_select", Select).value = settings.get("source", "Flatbed")
        self.query_one("#extra_input", Input).value = settings.get("extra", "")
        self._advanced = bool(settings.get("advanced", False))
        self._auto_continue_single = bool(settings.get("auto_continue_single", True))
        self._update_free_space()

    def _output_dir_path(self) -> Optional[Path]:
        output_dir_input = self.query_one("#output_dir_input", Input).value.strip()
        if not output_dir_input:
            return None
        return Path(output_dir_input).expanduser()

    def _ensure_output_dir(self) -> Optional[Path]:
        output_dir = self._output_dir_path()
        if output_dir is None:
            self.log_message("[red]Output directory cannot be empty.[/red]")
            return None
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.log_message(f"[red]Failed to create output dir:[/red] {exc}")
            return None
        self._update_free_space(output_dir)
        return output_dir

    def _update_free_space(self, output_dir: Optional[Path] = None) -> None:
        try:
            if output_dir is None:
                output_dir = self._output_dir_path()
            if output_dir is None:
                self.query_one("#free_space", Static).update("-")
                return
            probe = output_dir if output_dir.exists() else output_dir.parent
            if not probe.exists():
                self.query_one("#free_space", Static).update("-")
                return
            usage = shutil.disk_usage(str(probe))
            self.query_one("#free_space", Static).update(format_bytes(usage.free))
        except Exception:
            self.query_one("#free_space", Static).update("-")

    def _update_next_filename(self) -> None:
        prefix_raw = self.query_one("#prefix_input", Input).value
        prefix = sanitize_prefix(prefix_raw)
        if not prefix:
            self.query_one("#next_file", Static).update("-")
            return
        output_dir = self._output_dir_path() or Path("./scans")
        fmt = (self.query_one("#format_select", Select).value or "png").lower()
        ext = "jpg" if fmt == "jpeg" else fmt
        index = next_index(prefix, output_dir, ext)
        filename = output_dir / f"{prefix}_{index:04d}.{ext}"
        self.query_one("#next_file", Static).update(str(filename))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            await self.action_refresh_scanners()
        elif event.button.id == "scan_button":
            await self.action_scan()
        elif event.button.id == "clear_log":
            await self.action_clear_log()
        elif event.button.id == "continue_button":
            device = self.active_device()
            if not device:
                self.log_message("[red]Select a scanner first.[/red]")
                self.set_select_status("Select a scanner to continue.")
                return
            self._set_stage("scan")
        elif event.button.id == "back_button":
            self._set_stage("select")
        elif event.button.id == "advanced_button":
            await self.action_toggle_advanced()
        elif event.button.id == "reset_count":
            await self.action_reset_count()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "scanner_select":
            self._update_scanner_detail(event.value)
            self._save_settings()
        elif event.select.id == "format_select":
            self._update_next_filename()
            self._save_settings()
        elif event.select.id in {"mode_select", "source_select"}:
            self._save_settings()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"prefix_input", "output_dir_input"}:
            self._update_next_filename()
            self._update_free_space()
        if event.input.id in {
            "prefix_input",
            "output_dir_input",
            "resolution_input",
            "extra_input",
        }:
            self._save_settings()

    def _focus_is_inputlike(self) -> bool:
        focused = self.focused
        if focused is None:
            return False
        if isinstance(focused, Input):
            return True
        if isinstance(focused, Select):
            return True
        if focused.__class__.__name__ == "SelectOverlay":
            return True
        return False

    async def on_key(self, event: events.Key) -> None:
        if event.key in {"up", "down", "left", "right"}:
            focused = self.focused
            if isinstance(focused, RichLog):
                event.stop()
                if event.key == "up":
                    focused.scroll_up(1)
                elif event.key == "down":
                    focused.scroll_down(1)
                return
            if self._focus_is_inputlike():
                return
            event.stop()
            if event.key in {"up", "left"}:
                self.action_focus_previous()
            else:
                self.action_focus_next()
            return

        if event.key in {"pageup", "pagedown", "home", "end"}:
            focused = self.focused
            if isinstance(focused, RichLog):
                event.stop()
                if event.key == "pageup":
                    focused.scroll_page_up()
                elif event.key == "pagedown":
                    focused.scroll_page_down()
                elif event.key == "home":
                    focused.scroll_home()
                else:
                    focused.scroll_end()
                return

        if event.key == "space":
            if self._focus_is_inputlike():
                return
            event.stop()
            await self.action_scan()
            return

        if event.key == "enter":
            if self._focus_is_inputlike():
                return
            event.stop()
            if self._stage == "select":
                device = self.active_device()
                if device:
                    self._set_stage("scan")
                else:
                    self.set_select_status("Select a scanner to continue.")
            else:
                await self.action_scan()


if __name__ == "__main__":
    ScanTUI().run()
