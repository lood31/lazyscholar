# LazyScholar

**A zero-effort knowledge butler.** Drop papers, links and chat logs into a folder — a background process does the rest: it fetches metadata, lets a local Gemma write structured study notes, and files them into your Obsidian vault, where they wait for you to simply read them.

Every productivity tool I tried asked me to be more disciplined. This one asks me for nothing.

*English | [简体中文](README.zh-CN.md)*

## Why

I read an AI paper every night and have long, deep discussions with AI — then forget most of it within days, because the knowledge is scattered across chat logs, PDFs and my head. My first idea was an Obsidian plugin, but a plugin still asks you to paste links and click buttons. That is a discipline tool, and discipline tools get abandoned. So LazyScholar removes the human from the loop entirely: knowledge files itself.

Coming this week, a daily card will also resurface 3 old notes before you forget them — an idea borrowed from a wishlist I once wrote for how an *AI agent's* memory should work: periodic random recall, resurfacing before forgetting. LazyScholar gives my own notes the memory system I wished AI had.

## How it works

```
inbox/  ──▶  route  ──▶  fetch / read  ──▶  local Gemma writes  ──▶  vault
(drop)     (by type)    (arXiv API)        (study note / card)     (filed)
```

1. **Watch** — a Python process polls `inbox/` every 30 seconds and only touches files whose size has stopped changing (so a file that is still copying is never half-read).
2. **Route & fetch** — a text file containing an arXiv link, or a PDF named with an arXiv ID, is resolved through the arXiv API for title, authors and abstract. Other `.txt`/`.md` files are treated as discussion logs.
3. **Read** — a local Gemma model (served by llama.cpp) writes the note:
   - *Papers* become a study note in a fixed template: one-line summary, a guided reading in Chinese, key points, bilingual vocabulary, and one read-aloud sentence picked from the abstract.
   - *Discussions* become a concept card: the core concept, the key points distilled, follow-up questions worth asking — with the original text preserved at the bottom.
4. **File** — notes land in `papers/`, concept cards in `concepts/`, processed sources move to `processed/`, and every event is written to `lazyscholar.log`.
5. **No silent failures** — if anything fails (arXiv rate limit, model server down), the file stays in `inbox/`, the error is logged, and a `daily/<date>-errors.md` card tells you what happened. LazyScholar never writes a fake note.

*Landing this week: candidate `[[backlinks]]` between notes, and the daily replay card.*

## The app

LazyScholar ships with a built-in web app — **no Obsidian required**. While the butler runs, open `http://127.0.0.1:8765` in any browser: a sidebar shows your inbox status, paper notes and concept cards, and the reading pane renders each note as a calm, magazine-like page. Model offline? The status pill tells you. Something failed today? A banner points you at the error card. The app and Obsidian read the same files, so use either — or both.

To run just the app without the butler: `python lazyscholar.py --vault <path> --ui`.

## What you drop in, what you get out

| You drop into `inbox/` | You get | Where |
| --- | --- | --- |
| A `.txt` file containing an arXiv link, e.g. `https://arxiv.org/abs/2510.16720` | A structured study note (summary / guide / key points / vocab / read-aloud sentence) + the abstract | `papers/<id>-<title>.md` |
| A PDF named with its arXiv ID, e.g. `2510.16720.pdf` | The same study note (v1 reads metadata + abstract; the PDF itself is archived, full-text parsing is not in v1) | `papers/<id>-<title>.md`, PDF in `processed/` |
| A discussion / chat log (`.md` or `.txt`, no arXiv link) | A concept card: core concept, distilled key points, follow-up questions, source attached | `concepts/<filename>.md` |
| Anything else | Left untouched in `inbox/` and noted in the log | — |
| (when something fails) | The file stays in `inbox/` and an error card explains why | `daily/<date>-errors.md` |

## Requirements

- **Python 3.11+** — standard library only, nothing to `pip install`.
- **llama.cpp** with any recent Gemma GGUF (developed and tested with Gemma 3 4B IT QAT, Q4_0, ~2.4 GB).
- **A vault folder** — an Obsidian vault is nicest, but any folder works; LazyScholar just writes Markdown.
- Developed on Windows 11; the script itself is plain cross-platform Python.

## Setup

**1. Get the model** (llama.cpp desktop app or CLI):

```bash
llama download -hf ggml-org/gemma-3-4b-it-qat-GGUF:Q4_0
```

**2. Start the model server** — and keep it running while LazyScholar works:

```bash
llama serve -m /path/to/gemma-3-4b-it-qat-Q4_0.gguf -c 16384 --port 8080
```

`-c 16384` matters: llama.cpp presets often default to a 4096-token context, which silently truncates longer inputs. Any port works as long as step 3 agrees with it (see Configuration).

**3. Start the butler:**

```bash
python lazyscholar.py --vault /path/to/your/vault
```

The vault folders (`inbox/`, `processed/`, `papers/`, `concepts/`, `daily/`) are created automatically on first run. Add `--once` to process whatever is in the inbox right now and exit — handy for testing. On Windows, a double-clickable launcher that starts both the server and the butler is easy to make from `start-lazyscholar.bat` (fill in your paths).

**4. Drop something into `inbox/`** and watch `lazyscholar.log`. Within about a minute, your note appears in `papers/` or `concepts/`.

## Day-to-day use

You only ever do three things:

1. **Drop** — save a paper link, a PDF, or a chat log into `inbox/`. From anywhere: the share sheet, a download folder, a screenshot of a chat (save the text) — if it lands in the folder, it gets filed.
2. **Read** — open your vault when you feel like it. Notes are already written, structured, and in your nightly reading format.
3. **Glance at the error card** — if `daily/` has an errors card, something needs you (usually: the model server wasn't running, or arXiv asked you to slow down). Nothing ever disappears silently.

## Configuration

| Setting | How | Default |
| --- | --- | --- |
| Model server URL | env `LAZYSCHOLAR_LLAMA` | `http://127.0.0.1:8080` |
| Model name sent to the server | env `LAZYSCHOLAR_MODEL` | `gemma-3-4b-it-qat-Q4_0` |
| Poll interval | `--interval <seconds>` | `30` |
| Single pass, then exit | `--once` | off (runs forever) |

Using the llama.cpp desktop app's built-in server instead? It listens on `http://127.0.0.1:9931` by default — set `LAZYSCHOLAR_LLAMA` to that and skip step 2.

## Troubleshooting

| Symptom | What it means | Fix |
| --- | --- | --- |
| Error card says `Connection refused` / `WinError 10061` | The model server isn't running | Start `llama serve` (step 2), then just wait — the file is still in `inbox/` and will be retried |
| Error card says `HTTP Error 429` | arXiv rate-limited your IP (common on shared campus networks) | Nothing to do: LazyScholar already retries with backoff; if it still fails, the file stays in `inbox/` for the next run |
| A file just sits in `inbox/` | Still copying, unsupported type, or the butler isn't running | Check `lazyscholar.log` — it says exactly what happened |
| Note sections look off | A 4B model improvising | The abstract/source is always attached at the bottom of the note — check against it, and tweak the prompt templates in `lazyscholar.py` |

## Design principles

- **Local and open only** — a local open-weight model, no API keys, no cloud; private reading history never leaves the machine, and dorm Wi-Fi can't hurt it.
- **Zero ongoing cost** — a student budget means zero API bills.
- **Honest failures** — errors are reported on a card, never swallowed; a note is only written when the model actually wrote it.
- **Stdlib only** — if Python runs, LazyScholar runs. No dependency roulette.

## Status

Built for DEV Hacktoberfest 2026, Week 3 ("Slow and Steady"). End-to-end generation is verified working on the target machine (2026-10-20): a dropped arXiv link produced a full study note, and a discussion log produced a concept card, with sources archived and zero manual steps. The built-in sidebar web app landed the same evening and is verified against real notes. Still to come this week: `[[backlink]]` suggestions and the daily replay card; then the DEV write-up.

## License

MIT
