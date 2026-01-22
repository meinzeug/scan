#!/usr/bin/env python3
"""Textual-based TUI for scanning with SANE (scanimage)."""

from __future__ import annotations

import asyncio
import importlib
import re
import shlex
import subprocess
import sys
import site
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

TEXTUAL_REQUIREMENT = "textual>=0.52"


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
    for line in output.splitlines():
        match = SCAN_LINE_RE.search(line)
        if not match:
            continue
        device_raw, name = match.group(1).strip(), match.group(2).strip()
        device = device_raw.strip("`'\"")
        if not device:
            continue
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

    #left, #right {
        width: 1fr;
        min-width: 40;
    }

    #left {
        border: round #3b3f46;
        padding: 1 2;
    }

    #right {
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

    #actions {
        height: auto;
        padding-top: 1;
    }

    #actions Button {
        width: 1fr;
        margin-right: 1;
    }

    #log {
        border: round #2f343a;
        height: 12;
        padding: 1 1;
        margin: 1 2;
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
    ]

    def __init__(self) -> None:
        super().__init__()
        self._scanners: List[ScannerInfo] = []
        self._scan_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Scanner", classes="section-title")
                yield Label("Available devices", classes="field-label")
                yield Select(options=[], id="scanner_select")
                yield Label("Device details", classes="field-label")
                yield Static("No scanner selected.", id="scanner_detail")
                with Horizontal(id="actions"):
                    yield Button("Refresh", id="refresh", variant="primary")
                    yield Button("Test Scan", id="test_scan", variant="default")
            with Vertical(id="right"):
                yield Static("Scan Settings", classes="section-title")
                yield Label("File name prefix", classes="field-label")
                yield Input(value="scan", id="prefix_input", placeholder="scan")
                yield Label("Output directory", classes="field-label")
                yield Input(value="./scans", id="output_dir_input", placeholder="./scans")
                yield Label("Format", classes="field-label")
                yield Select(
                    options=[
                        ("PNG", "png"),
                        ("JPEG", "jpeg"),
                        ("TIFF", "tiff"),
                        ("PDF", "pdf"),
                        ("PNM", "pnm"),
                    ],
                    id="format_select",
                    value="png",
                )
                yield Label("Resolution (DPI)", classes="field-label")
                yield Input(value="300", id="resolution_input", placeholder="300")
                yield Label("Mode", classes="field-label")
                yield Select(
                    options=[
                        ("Default", ""),
                        ("Color", "Color"),
                        ("Gray", "Gray"),
                        ("Lineart", "Lineart"),
                    ],
                    id="mode_select",
                    value="Color",
                )
                yield Label("Source", classes="field-label")
                yield Select(
                    options=[
                        ("Default", ""),
                        ("Flatbed", "Flatbed"),
                        ("ADF", "ADF"),
                        ("ADF Duplex", "ADF Duplex"),
                    ],
                    id="source_select",
                    value="Flatbed",
                )
                yield Label("Extra scanimage options", classes="field-label")
                yield Input(
                    id="extra_input",
                    placeholder="e.g. --brightness 10 --contrast 5",
                )
                with Horizontal(id="actions"):
                    yield Button("Scan (Space)", id="scan_button", variant="success")
                    yield Button("Clear Log", id="clear_log", variant="default")
                with Horizontal(id="status_bar"):
                    yield Label("Idle", id="status_label")
                    yield LoadingIndicator(id="spinner")
        yield RichLog(id="log", highlight=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#spinner", LoadingIndicator).display = False
        await self.action_refresh_scanners()

    def log_message(self, message: str) -> None:
        log = self.query_one("#log", RichLog)
        log.write(message)

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
            self.log_message("[red]Error:[/red] scanimage not found. Install SANE tools.")
            return
        except subprocess.TimeoutExpired:
            self.set_status("Scan timed out", busy=False)
            self.log_message("[red]Error:[/red] scanimage -L timed out.")
            return

        if result.returncode != 0:
            self.set_status("Scan failed", busy=False)
            self.log_message(f"[red]scanimage error:[/red] {result.stderr.strip() or result.stdout.strip()}")
            return

        scanners = parse_scanimage_list(result.stdout)
        self._scanners = scanners
        if not scanners:
            self._set_select_options([])
            self.query_one("#scanner_detail", Static).update("No scanners found.")
            self.set_status("No scanners found", busy=False)
            self.log_message("[yellow]No scanners detected.[/yellow]")
            return

        options = [(f"{s.name} [{short_device(s.device)}]", s.device) for s in scanners]
        self._set_select_options(options)
        self.set_status(f"Found {len(scanners)} scanner(s)", busy=False)
        self._update_scanner_detail(self.active_device())

    def _update_scanner_detail(self, device: Optional[str]) -> None:
        detail = self.query_one("#scanner_detail", Static)
        if not device:
            detail.update("No scanner selected.")
            return
        scanner = next((s for s in self._scanners if s.device == device), None)
        if not scanner:
            detail.update(device)
            return
        detail.update(f"{scanner.name}\n{scanner.device}")

    async def action_scan(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            return
        if self._scan_lock.locked():
            self.log_message("[yellow]Scan already in progress.[/yellow]")
            return
        async with self._scan_lock:
            await self._run_scan()

    async def _run_scan(self) -> None:
        device = self.active_device()
        if not device:
            self.log_message("[red]Select a scanner first.[/red]")
            return

        prefix = self.query_one("#prefix_input", Input).value.strip()
        if not prefix:
            self.log_message("[red]Prefix cannot be empty.[/red]")
            return

        output_dir_input = self.query_one("#output_dir_input", Input).value.strip()
        output_dir = Path(output_dir_input).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)

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
                return

        self.set_status(f"Scanning {filename.name}…", busy=True)
        self.log_message(f"[cyan]Scanning[/cyan] {filename.name} on {short_device(device)}")
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
            return

        if result.returncode != 0:
            self.set_status("Scan failed", busy=False)
            error = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            self.log_message(f"[red]scanimage failed:[/red] {error}")
            return

        self.set_status("Scan complete", busy=False)
        self.log_message(f"[green]Saved:[/green] {filename}")

    async def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()
        self.log_message("Log cleared.")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            await self.action_refresh_scanners()
        elif event.button.id == "scan_button":
            await self.action_scan()
        elif event.button.id == "clear_log":
            await self.action_clear_log()
        elif event.button.id == "test_scan":
            await self.action_scan()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "scanner_select":
            self._update_scanner_detail(event.value)

    async def on_key(self, event: events.Key) -> None:
        if event.key == "space":
            focused = self.focused
            if isinstance(focused, Input):
                return
            event.stop()
            await self.action_scan()


if __name__ == "__main__":
    ScanTUI().run()
