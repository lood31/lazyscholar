# LazyScholar

A Windows research companion with a floating sidebar, a local llama.cpp model, and a comfortable paper reader. Queue an arXiv paper, generate an abstract-based study note, then read the original with faithful mathematics and persistent text highlights.

**Local inference · No Ollama · No paid model API · Obsidian optional**

[中文说明](README.zh-CN.md) · [Windows builds](https://github.com/lood31/lazyscholar/actions/workflows/build.yml) · [Releases](https://github.com/lood31/lazyscholar/releases)

## What it does

- Queue arXiv links/IDs or local PDFs named with their arXiv ID.
- Generate study notes with a local Gemma/GGUF model through llama.cpp; run immediately or on a schedule.
- Automatically fetch and cache arXiv HTML. Text reflows, figures are embedded, and WebView2 renders the source MathML locally—no model rewriting of equations.
- Adjust reading font size, scroll wide equations horizontally, or click a formula/figure to enlarge it.
- Add highlights by selecting text; erase only a selected portion without clearing the rest of the paper.
- Keep the complete original PDF available for comparison.
- Choose a storage folder without automatically moving existing files.

## Windows quick start

### 1. Get the application and local model

Download an available Windows release, or open a **successful** `build-windows` Actions run for the commit you want and download its `LazyScholar-windows` artifact. Extract it and run `LazyScholar.exe` (inside `dist/` in the Actions artifact).

The EXE does **not** require Python or Obsidian. It does require:

- Windows with the Microsoft Edge **WebView2 Runtime**, used by the desktop UI and native MathML.
- A local llama.cpp installation and a compatible **GGUF model**, supplied separately. Gemma 3 4B Q4 has been used on the target machine; model speed and memory use depend on your hardware.

**Launcher compatibility:** the app's managed startup command is `llama serve -m <model> -c <context> --port <port>`. Select the `llama` launcher that supports this command; an unrelated executable or a standalone binary without the `serve` subcommand is not interchangeable.

Pushes to `main` build Actions artifacts; only version tags publish GitHub Releases. A source push does not mean a new Release is already available—wait for the corresponding successful build.

### 2. Configure it once

1. Double-click the EXE, then click the floating ball to open the sidebar.
2. In **Settings / Model**, set the llama executable path and the GGUF file path. Save the settings.
3. Check the local model port/context size. Defaults are port **8080** and context **16384**.
4. Optionally choose a storage folder in Settings. **Quit and restart the app to activate the change. Existing files are never moved automatically.**

You can start/stop the model on the Model tab. When processing starts a model for a batch, the app stops that process afterward; an already-running service is not treated as an app-owned process.

### 3. Add a paper and generate its note

In **Queue**, add either:

- An arXiv URL, e.g. `https://arxiv.org/abs/2610.30130`, or an ID such as `2610.30130`.
- A local PDF whose filename contains an arXiv ID, e.g. `2610.30130.pdf`.

Click **Process now**, or configure the daily processing time. Keep the application running for scheduled processing. Errors are shown on the item; fix the cause and retry rather than repeatedly adding duplicates.

Open the finished paper from **Notes**. The reader provides **Article**, **Note**, and **PDF source** views.

## Reading and annotations

### Original article

Article view automatically obtains arXiv HTML and caches it locally—you do not need to save a webpage for each paper. Use `A− / A+` to change text size. More sections load as you continue reading; wide equations remain scrollable and formulas/figures can be enlarged.

If HTML is unavailable or a request fails, the app shows the error and a retry button. Open **PDF source** for the complete original instead. It does not silently substitute fragmented PDF formula extraction for valid mathematics.

### Highlight text

1. Open **Article** or **PDF source**.
2. Enable **Highlight**.
3. Drag across ordinary text and release to save a yellow mark.
4. Click the mode button again to return to ordinary selection/copying.

### Erase only what you select

1. Click **Erase highlights** to enter eraser mode. **Clicking the button does not delete any marks.**
2. Drag across the highlighted portion you want to remove.
3. Only the selected overlap is erased. Selecting the middle leaves both ends; other highlights and unmarked text stay unchanged.
4. Click **Exit eraser** to leave the mode. Highlight and eraser modes are mutually exclusive.

Partial erasing works in both Article and PDF comparison views, and saved changes survive reopening. Article text annotations also survive font changes. Formula glyphs, figure images, and Note view are not Article text-highlight anchors.

Legacy PDF text annotations migrate to HTML only when the quoted text has a unique exact match. Original annotation files are retained; unmatched quotes remain available in the reader rather than being silently attached to a different sentence.

## Storage, network and privacy

- Notes and reading data live in the selected folder. Typical folders include `papers/`, `concepts/`, `papers_pdf/`, `html_reader/`, `reader/` and `highlights/`; settings/queue state are stored alongside them.
- Changing the selected folder takes effect **after restarting**. It does not migrate, delete, or merge the old folder.
- Explicit `--vault` takes priority; `LAZYSCHOLAR_VAULT` also overrides the saved GUI selection. The app displays an override notice.
- Model inference is local. arXiv metadata and uncached HTML/images/PDFs still require network access; cached article reading does not require downloading the article again.
- The optional **HTML/image proxy** field is blank by default and uses system network settings. If needed, enter an HTTP(S) proxy address such as `http://127.0.0.1:<your-port>`. This field applies to HTML/images, not the local model, and does not modify system proxy/TUN settings. Proxy credentials in the URL are not supported.
- Downloaded paper scripts/styles are not embedded into the privileged desktop page. Source MathML is preserved and rendered locally.

## Important limits

- **Generated study notes are based on the arXiv abstract, not an AI analysis of the complete PDF.** Full-text reading is a separate source-preserving feature.
- Some papers have no arXiv HTML edition. Use the complete PDF in that case.
- There is no automatic mathematical OCR or model-based equation repair.
- The desktop queue currently expects an arXiv identifier; it is not a general arbitrary-PDF ingestion tool.
- Backlinks and daily replay are not included in this delivery.

## Run from source

Python **3.13 on Windows** is the tested setup. From PowerShell:

```powershell
git clone https://github.com/lood31/lazyscholar.git
cd lazyscholar
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe lazyscholar.py --app
```

To choose a folder explicitly:

```powershell
.\.venv\Scripts\python.exe lazyscholar.py --app --vault "D:\MyScholarVault"
```

HTTP-only mode, without inbox polling:

```powershell
.\.venv\Scripts\python.exe lazyscholar.py --ui --vault "D:\MyScholarVault"
```

Then open `http://127.0.0.1:8765/app`. Native tray/file-folder dialogs require desktop mode. Use `--help` for the retained command-line inbox-polling options; that path is separate from the desktop queue.

### Build a portable EXE

From the repository root, after installing the runtime dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --windowed --onefile --name LazyScholar --icon lazyscholar.ico --collect-all webview --collect-all pystray --collect-all pdfplumber --collect-all pdfminer --collect-all pypdfium2 --collect-all bleach --collect-all bs4 --collect-all defusedxml --add-data "vendor;vendor" --add-data "lazyscholar.ico;." lazyscholar.py
```

Output: `dist/LazyScholar.exe`. The bundled Python dependencies do not include the local GGUF model or the system WebView2 runtime. The older `build-app.cmd` / `deploy-v2.ps1` helpers contain maintainer-machine paths; use the commands above for a different checkout location.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No model starts | Check launcher/GGUF paths, the `llama serve` command, port conflicts and `model-server.log` in the storage folder. |
| A paper fails to process | Check its arXiv ID, connectivity and visible queue error, then retry. A PDF filename needs an arXiv ID. |
| HTML cannot be obtained | Retry, check the optional HTML proxy, or open PDF source. Not every paper has HTML. |
| New storage folder is not active | Quit and restart; remove an unintended `--vault` / `LAZYSCHOLAR_VAULT` override. Old files are deliberately not moved. |
| Text selection adds/removes marks | Exit Highlight/Eraser mode to return to normal copying. |
| UI or mathematics does not display | Check that WebView2 is installed and up to date. |

## Development checks

Use the same Python environment that has the dependencies installed. If it is not activated, replace `python` below with `.\.venv\Scripts\python.exe`.

Offline backend/extractor tests:

```powershell
python -B scripts/test_app_backend.py
python -B scripts/test_paper_reader.py
python -B scripts/test_paper_html.py
```

Browser regressions additionally require Playwright and its Chromium installation:

```powershell
python -m pip install playwright
python -m playwright install chromium
python -B scripts/test_reader_browser.py
python -B scripts/test_highlight_eraser.py
```

Interactive Windows acceptance uses an isolated test folder and briefly moves/restores the mouse. Keep its test window in the foreground and pause manual input:

```powershell
python -B scripts/test_native_highlight.py --exe dist/LazyScholar.exe --focus-wait 60
```

Backend/browser tests, source MathML display and real Windows mouse highlighting/partial erasing have been exercised locally. Browser-created selections are **not** a substitute for native-input acceptance. Generated QA folders, local notes, model weights and handover snapshots are not published as source.

## Project and license

Built for DEV Hacktoberfest 2026, Week 3: **Slow and Steady**. The aim is a small, private study companion, not a cloud knowledge platform.

Application code: [MIT](LICENSE). llama.cpp, model weights and bundled third-party components retain their own licenses.
