# scan

TUI Scanning Tool for Ubuntu Linux (SANE `scanimage`).

## Features
- Lists all connected scanners (USB + network) via `scanimage -L`
- Select a scanner, set a filename prefix, and press Space to scan each sheet
- Log panel with status/errors
- Common scan settings (format, resolution, mode, source) + extra raw options

## Requirements
- Ubuntu Linux with SANE tools installed (`scanimage` command available)
- Python 3.9+

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

## Run

```bash
./scan_tui.py
```

If the executable bit is missing:

```bash
chmod +x scan_tui.py
./scan_tui.py
```

## Tips
- Use the **prefix** once, then press **Space** for each new page.
- Use **Refresh** if you plug/unplug a scanner.
- If your scanner needs special options, add them in **Extra scanimage options**.

## Troubleshooting
- If no scanners appear, check `scanimage -L` in a terminal.
- Some devices need `--source Flatbed` or `--source ADF` depending on the feeder.
