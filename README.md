# LazyScholar

**A zero-effort knowledge butler.** Drop papers, links and chat logs into a folder — a background process does the rest: it fetches metadata, lets a local Gemma write structured study notes, files them into your Obsidian vault, links them to what you already know, and resurfaces old notes daily, before you forget them.

Every productivity tool I tried asked me to be more disciplined. This one asks me for nothing.

## Why

I read an AI paper every night and have long, deep discussions with AI — then forget most of it within days, because the knowledge is scattered across chat logs, PDFs and my head. My first idea was an Obsidian plugin, but a plugin still asks you to paste links and click buttons. That is a discipline tool, and discipline tools get abandoned. So LazyScholar removes the human from the loop entirely: knowledge files itself, and comes back to find you.

The daily replay is borrowed from a wishlist I once wrote for how an *AI agent's* memory should work — periodic random recall, resurfacing before forgetting. LazyScholar gives my own notes the memory system I wished AI had.

## How it works

1. **Watch** — a Python process polls an `inbox/` folder every 30 seconds and only touches files whose size has stopped changing.
2. **Fetch** — papers (PDF or arXiv link) are resolved through the arXiv API for title, authors and abstract; discussion logs (`.txt`/`.md`) go to concept distillation.
3. **Read** — local Gemma (via llama.cpp) writes a study note in a fixed template: one-line summary, guided reading, key points, bilingual vocabulary, a read-aloud sentence.
4. **File & link** — notes land in `papers/`, concept cards in `concepts/`, with candidate `[[links]]` to existing notes.
5. **Replay** — a daily card lists new arrivals and resurfaces 3 old notes (weighted by time-unseen, plus randomness), each with a self-test question.
6. **No silent failures** — processed sources move to `processed/`, everything is logged, and failures stay in the inbox and are reported on the daily card.

## Why open / local

- **Privacy** — private notes and reading history never leave the machine.
- **Offline** — dorm Wi-Fi is unreliable; a local model doesn't care.
- **Zero cost** — a student budget means zero API bills.
- **Hackable** — templates, prompts and replay rules are all yours to change.

## Status

Built for DEV Hacktoberfest 2026, Week 3 ("Slow and Steady"). End-to-end generation works (verified 2026-10-20 on the target machine): drop a paper link or a discussion log in the inbox and a local Gemma turns it into a structured study note / concept card in the vault. Still landing this week: [[concept]] backlink suggestions and the daily replay card.

## Quick start

```bash
llama serve -m /path/to/gemma-3-4b-it-Q4_K_M.gguf -c 16384   # keep running
python lazyscholar.py --vault /path/to/your/vault   # add --once for a single pass
```

MIT License.
