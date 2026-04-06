# Building & Distributing Chain Wallet

## Quick start (run from source — for developers)

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

pip install -r requirements.txt
python wallet.py
```

---

## Building a distributable binary

### 1. Install PyInstaller

```bash
pip install pyinstaller
```

### 2. Build

```bash
pyinstaller wallet.spec
```

Output lands in `dist/ChainWallet/`.

---

## Platform notes

### Windows
- Output: `dist/ChainWallet/ChainWallet.exe`
- Zip the whole `dist/ChainWallet/` folder and send it — the `.exe` needs the DLLs next to it.
- If you want a single `.exe` (slower to start, ~200 MB), replace the `COLLECT` block in the spec
  with a `EXE(..., a.binaries, a.zipfiles, a.datas, ...)` call — or just run:
  ```
  pyinstaller --onefile --windowed wallet.py
  ```
- Defender may flag unsigned executables. Recipients can right-click → Properties → Unblock.

### macOS
- Output: `dist/ChainWallet.app`
- Drag the `.app` into a `.dmg` for easy distribution:
  ```bash
  hdiutil create -volname ChainWallet -srcfolder dist/ChainWallet.app \
      -ov -format UDZO ChainWallet.dmg
  ```
- Gatekeeper will warn about unsigned apps. Recipients: right-click → Open → Open anyway.
- For Apple Silicon (M-series), build on the same architecture you target.
  To build a universal binary: `--target-arch universal2` (requires both Python archs installed).

### Linux
- Output: `dist/ChainWallet/ChainWallet`
- Tar it up:
  ```bash
  tar -czf ChainWallet-linux.tar.gz -C dist ChainWallet
  ```
- Recipients may need to `chmod +x ChainWallet` before running.
- PySide6 requires `libGL` and `libEGL` on the target machine. On a clean Ubuntu/Debian:
  ```bash
  sudo apt install libgl1-mesa-glx libegl1
  ```

---

## What to send your friends

| Platform | What to ship                       | How to run                    |
|----------|------------------------------------|-------------------------------|
| Windows  | `ChainWallet-win.zip` (whole folder) | Extract → double-click `.exe` |
| macOS    | `ChainWallet.dmg`                  | Mount → drag to Applications  |
| Linux    | `ChainWallet-linux.tar.gz`         | Extract → `./ChainWallet`     |

---

## Server distribution

The wallet connects to `server.py` — your friends need that running too (or you host it).

Options:
1. **Everyone runs their own node** — send them `server.py`, `requirements_server.txt`:
   ```
   flask
   flask-limiter
   ecdsa
   ```
2. **You host a shared node** — run `server.py` on a VPS, edit `SERVER_URL` in `wallet.py`
   to your public IP before building.

---

## Changing the server URL before building

Edit the top of `wallet.py`:

```python
SERVER_URL = "http://YOUR_SERVER_IP:8765"
```

Then rebuild with `pyinstaller wallet.spec`.

---

## Size reduction tips

- Install UPX (`https://upx.github.io`) before building — PyInstaller uses it automatically
  and typically cuts 30–40 % off binary size.
- The spec already excludes heavy Qt modules (WebEngine, Multimedia, 3D, etc.).
- To exclude even more, add unused Qt modules to the `excludes` list in `wallet.spec`.
