# LazyScholar

A Windows research companion with a floating sidebar, a local llama.cpp model, and a comfortable paper reader. Queue an arXiv paper and read it immediately with faithful mathematics and persistent highlights. Local whole-text study notes are generated independently, not required for reading.

**Local inference · No Ollama · No paid model API · Obsidian optional**

[中文说明](README.zh-CN.md) · [Windows builds](https://github.com/lood31/lazyscholar/actions/workflows/build.yml) · [Releases](https://github.com/lood31/lazyscholar/releases)

## What it does

- Queue arXiv links/IDs, local PDFs named with their arXiv ID, or saved HTML/MHTML webpages.
- Process all extracted source text in chunks, check quoted evidence, then hierarchically summarize with a local Gemma/GGUF model. Run immediately or on a schedule, not just from an abstract.
- Automatically fetch and cache arXiv HTML. Text reflows, figures are embedded, and WebView2 renders the source MathML locally—no model rewriting of equations.
- Adjust reading font size, scroll wide equations horizontally, or click a formula/figure to enlarge it.
- Add highlights by selecting text; erase only a selected portion without clearing the rest of the paper.
- Read directly from the queue before generating a note; imported local PDFs work offline.
- View original PDFs at 150% by default, with zoom, fit-width, horizontal scrolling, lazy rendering and a separate resizable reader window.
- Choose a storage folder without automatically moving existing files.

## Windows quick start

### 1. Get the application and local model

Download an available Windows release, or open a **successful** `build-windows` Actions run for the commit you want and download its `LazyScholar-windows` artifact. Extract it and run `LazyScholar.exe` (inside `dist/` in the Actions artifact).

The EXE does **not** require Python or Obsidian. It does require:

- Windows with the Microsoft Edge **WebView2 Runtime**, used by the desktop UI and native MathML.
- **For note generation**, a local llama.cpp installation and a compatible **GGUF model**, supplied separately. Reading does not require a model. Gemma 3 4B Q4 has been used on the target machine; model speed and memory use depend on your hardware.

**Launcher compatibility:** the app's managed startup command is `llama serve -m <model> -c <context> --port <port>`. Select the `llama` launcher that supports this command; an unrelated executable or a standalone binary without the `serve` subcommand is not interchangeable.

Pushes to `main` build Actions artifacts; only version tags publish GitHub Releases. A source push does not mean a new Release is already available—wait for the corresponding successful build.

### 2. Configure it once

1. Double-click the EXE, then click the floating ball to open the sidebar.
2. In **Settings / Model**, set the llama executable path and the GGUF file path. Save the settings.
3. Check the local model port/context size. Defaults are port **8080** and context **16384**.
4. Optionally choose a storage folder in Settings. **Quit and restart the app to activate the change. Existing files are never moved automatically.**

You can start/stop the model on the Model tab. When processing starts a model for a batch, the app stops that process afterward; an already-running service is not treated as an app-owned process.

### 3. Add a paper, read now, or generate a note

In **Queue**, add either:

- An arXiv URL, e.g. `https://arxiv.org/abs/2610.30130`, or an ID such as `2610.30130`.
- A local PDF whose filename contains an arXiv ID, e.g. `2610.30130.pdf`.
- **Import saved webpage…** accepts `.html`/`.htm` and `.mhtml`/`.mht`, without an arXiv ID.

Click **Read now** on a queue item without starting a model or generating a note. Local imports open the original PDF without fetching its abstract; links open arXiv HTML (uncached content still needs a network). Missing notes are explicitly indicated and do not block reading or annotations.

To generate notes, click **Process now** or configure the daily processing time. Keep the application running for scheduled processing. Generation errors do not block reading; fix the cause and retry rather than repeatedly adding duplicates.

Open the finished paper from **Notes**. The reader provides **Article** and **Note**; arXiv/PDF entries also offer **PDF source**.

### Saved webpages

For a browser's complete webpage save, keep the HTML and companion resource directory together. Single-file MHTML snapshots are also supported. Import only reads local/embedded resources, never downloads missing images or executes paper scripts.

Native MathML is preserved. When MathJax/KaTeX includes accessibility MathML, only that copy is kept to avoid duplicate formulas. SVGs become whitelisted standalone static images; styling may be simplified. Canvas pixels are commonly absent from saved pages. Missing figures/resources are explicitly disclosed: this is not a complete replica of website interactions, and saving an abstract page does not acquire the full paper.

Limits: 32 MB per saved file and 5 MB for HTML. Local image paths cannot escape the webpage directory. Source files remain unchanged.

### Whole-text study notes

The desktop queue reads arXiv HTML (or complete PDF text when HTML is unavailable), the actual imported local PDF, or imported static webpage text. It does not silently fall back to an abstract.

All extracted text is processed in chunks. The model selects source-passage IDs, and the application retrieves the corresponding extracted text before producing a hierarchical summary with references such as `[C0001]`. Each final section must select valid source IDs too. The queue shows progress. Notes disclose the actual source/limitations; fingerprints and evidence are saved under `fulltext/`. Index checks establish where evidence came from, not whether the model's interpretation is correct.

Context must be at least **8192** (default **16384**). Sources over 128 processing chunks or invalid model results fail explicitly: no dropped tail, partial note, or replacement of an existing good note. Image-only PDF pages cannot produce a whole-text note, but remain readable as original pages.

New model-generated content follows the interface language saved when each task starts (Chinese or English), including chunk analysis, intermediate merges and the final guide. Switching languages does not translate or rewrite existing Markdown; an active task keeps its starting language, and original evidence quotations stay in their source language.

## Reading and annotations

### Original article

Article view automatically obtains arXiv HTML and caches it locally—you do not need to save a webpage for each paper. Use `A− / A+` to change text size. More sections load as you continue reading; wide equations remain scrollable and formulas/figures can be enlarged.

If HTML is unavailable or a request fails, the app shows the error and a retry button. Open **PDF source** for the complete original instead. It does not silently substitute fragmented PDF formula extraction for valid mathematics.

### Comfortable PDF reading

PDF source defaults to 150%. Use `− / ＋` to zoom, **Fit width** to fit the page, and horizontal scrolling to pan without changing the original equations or figures. PDF zoom is separate from HTML's `A− / A+` text size. Pages are rasterized on demand, using higher-resolution canvases on high-DPI displays.

**Larger window** opens a separate resizable reader in desktop mode, or a new tab in HTTP mode. Saved annotations are shared. Scanned PDFs remain viewable, but text selection requires a usable text layer.

A different PDF with the same arXiv ID will not silently replace an existing cached version and misplace its annotations.

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

Reader windows sharing the same app use revision-checked saves. On conflict, they reread and reapply the current addition/erasure instead of replacing newer annotations with an old list. Persistent conflicts produce an explicit retry error.

Legacy PDF text annotations migrate to HTML only when the quoted text has a unique exact match. Original annotation files are retained; unmatched quotes remain available in the reader rather than being silently attached to a different sentence.

## Remove and restore papers

Use **Trash paper** on a paper card or queue entry, then confirm. **Notes → Trash** provides **Restore**. The app moves the identified paper's guides, known app-owned source/image caches, annotations, evidence and related queue data together into `.paper-trash/` inside the storage folder; it does not permanently delete files or move external originals. Unidentified hand-written notes are not treated as papers merely because they cite an arXiv link.

Generation or an active source/annotation request temporarily blocks moving a paper. Trashed IDs cannot be redownloaded, reimported or written back by old readers until restored. Restore never overwrites an occupied destination. Interrupted operations retain a journal and can be retried from Trash; clear a filename conflict manually before restoring.

## Storage, network and privacy

- Notes and reading data live in the selected folder. Typical folders include `papers/`, `concepts/`, `papers_pdf/`, `html_reader/`, `reader/`, `highlights/` and `fulltext/`; settings/queue state are stored alongside them.
- Changing the selected folder takes effect **after restarting**. It does not migrate, delete, or merge the old folder.
- Explicit `--vault` takes priority; `LAZYSCHOLAR_VAULT` also overrides the saved GUI selection. The app displays an override notice.
- Model inference is local. arXiv metadata and uncached HTML/images/PDFs still require network access; cached article reading does not require downloading the article again.
- The optional **HTML/image proxy** field is blank by default and uses system network settings. If needed, enter an HTTP(S) proxy address such as `http://127.0.0.1:<your-port>`. This field applies to HTML/images, not the local model, and does not modify system proxy/TUN settings. Proxy credentials in the URL are not supported.
- Downloaded paper scripts/styles are not embedded into the privileged desktop page. Source MathML is preserved and rendered locally.

## Important limits

- **The desktop queue processes all extractable source text, not a visual understanding of every figure, formula or layout.** PDF extraction order can be affected by columns, and a saved webpage may not contain the complete paper. The retained command-line inbox path still uses abstract-based notes.
- Valid source IDs do not prove that an interpretation is correct. A model may still turn “not demonstrated” into “cannot”; check claims, numbers and conditions against the original text.
- Some papers have no arXiv HTML edition. Use the complete PDF in that case.
- There is no automatic mathematical OCR or model-based equation repair.
- PDF/link inputs still require an arXiv identifier; arbitrary-PDF import is not supported. Saved webpages do not require an arXiv ID.

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

Annotation POSTs must echo the `revision` returned by GET. A conflict returns HTTP 409: reread and recompute the operation rather than blindly resending an old replacement list.

Offline backend/extractor tests:

```powershell
python -B scripts/test_app_backend.py
python -B scripts/test_paper_reader.py
python -B scripts/test_paper_html.py
python -B scripts/test_saved_html.py
python -B scripts/test_fulltext.py
python -B scripts/test_generation_language.py
python -B scripts/test_paper_trash.py
```

Browser regressions additionally require Playwright and its Chromium installation:

```powershell
python -m pip install playwright
python -m playwright install chromium
python -B scripts/test_reader_browser.py
python -B scripts/test_highlight_eraser.py
python -B scripts/test_independent_reading.py
python -B scripts/test_saved_html_browser.py
python -B scripts/test_reader_races.py
python -B scripts/test_pdf_loading_zoom.py
python -B scripts/test_paper_trash_browser.py
```

Interactive Windows acceptance uses an isolated test folder and briefly moves/restores the mouse. Keep its test window in the foreground and pause manual input:

```powershell
python -B scripts/test_native_highlight.py --exe dist/LazyScholar.exe --focus-wait 60
python -B scripts/test_native_pdf_zoom.py --exe dist/LazyScholar.exe --during-load --focus-wait 60
```

Highlighting/partial erasing and PDF zoom have had real Windows mouse acceptance. Trash/restore has isolated backend/browser coverage, and language switching has real local-model queue acceptance. Rebuilt packages should be rechecked; browser-created selections are **not** a substitute for native input. Generated QA folders, local notes, model weights, trash journals and handover snapshots are not published as source.

## Project and license

Built for DEV Hacktoberfest 2026, Week 3: **Slow and Steady**. The aim is a small, private study companion, not a cloud knowledge platform.

Application code: [MIT](LICENSE). llama.cpp, model weights and bundled third-party components retain their own licenses.
