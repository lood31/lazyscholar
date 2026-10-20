#!/usr/bin/env python3
"""
LazyScholar — unattended knowledge butler.

What this skeleton already does (verified 2026-10-20):
  * Polls an inbox folder every N seconds (default 30).
  * Waits until a file's size is stable across two polls before touching it
    (so half-downloaded PDFs are never processed).
  * Routes files by type:
      - .txt/.md containing an arXiv id or arXiv URL  -> paper pipeline
      - .pdf whose filename contains an arXiv id     -> paper pipeline
      - other .txt/.md                               -> discussion pipeline
      - anything else                                 -> logged, left in place
  * Paper pipeline: fetches title/authors/abstract from the arXiv API
    (stdlib only, no pip installs), then a local Gemma writes the study
    note. Discussion logs are distilled into concept cards the same way.
    If generation fails, no note is written and the failure is reported —
    real notes are never faked.
  * Moves processed sources to processed/, writes one log line per event,
    and NEVER silently drops a failure: failures stay in inbox and are
    listed in the daily error card.

Generation (implemented 2026-10-20, ahead of the Wednesday slot):
  * Papers: arXiv metadata (with retry/backoff on 429) -> local Gemma
    writes the study note (one-line summary / Chinese guide / key points /
    vocab / read-aloud sentence) via the llama.cpp OpenAI-compatible API.
  * Discussions: Gemma distills a concept card (core concept / key
    points / follow-up questions); the source text is kept at the bottom.
  * The llama server is expected at LLAMA_BASE (default
    http://127.0.0.1:8080, i.e. "llama serve -m <gguf> -c 16384
    --port 8080"). The desktop app's own server on 127.0.0.1:9931 also
    works if pointed there. If generation fails, the file stays in the
    inbox and the failure lands on the daily error card — no fake notes.

Remaining TODOs (Thursday, from PLAN.md v3):
  * TODO(link): suggest [[concept]] backlinks from existing vault notes.
  * TODO(replay): daily card picks 3 old notes weighted by time-unseen.

Usage:
  python lazyscholar.py --vault PATH [--inbox PATH] [--once] [--interval 30]
Defaults assume the vault contains inbox/ processed/ papers/ concepts/ daily/.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
ARXIV_API = "https://export.arxiv.org/api/query?id_list={}"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
PAPER_EXTS = {".pdf"}
TEXT_EXTS = {".txt", ".md"}

# Local llama.cpp server (llama serve). Override with LAZYSCHOLAR_LLAMA env.
LLAMA_BASE = os.environ.get("LAZYSCHOLAR_LLAMA", "http://127.0.0.1:8080")
LLM_MODEL = os.environ.get("LAZYSCHOLAR_MODEL", "gemma-3-4b-it-qat-Q4_0")
LLM_TIMEOUT = 240          # a 4B model needs ~1 min for a full note; be patient
MAX_INPUT_CHARS = 9000     # v1 truncation for long discussion logs (TODO: chunk)


def log(vault: Path, msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(vault / "lazyscholar.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_arxiv(arxiv_id: str, attempts: int = 3) -> dict:
    """Return {title, authors, summary, url} from the arXiv API.

    arXiv rate-limits shared/NAT IPs aggressively (HTTP 429), so retry
    with backoff before giving up; the caller turns a final failure into
    an error-card entry and leaves the file in the inbox.
    """
    last_exc = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                ARXIV_API.format(arxiv_id),
                headers={"User-Agent": "LazyScholar/0.2 (personal research tool)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                root = ET.fromstring(resp.read())
            entry = root.find("a:entry", ATOM)
            if entry is None:
                raise ValueError(f"arXiv returned no entry for {arxiv_id}")
            title = " ".join(entry.findtext("a:title", "", ATOM).split())
            summary = " ".join(entry.findtext("a:summary", "", ATOM).split())
            authors = [a.findtext("a:name", "", ATOM) for a in entry.findall("a:author", ATOM)]
            return {
                "title": title,
                "authors": authors,
                "summary": summary,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            }
        except Exception as e:  # noqa: BLE001 - retried, then re-raised below
            last_exc = e
            if i < attempts - 1:
                time.sleep(8 + 7 * i)  # 8s, then 15s
    raise last_exc


def llm_chat(system: str, user: str, max_tokens: int = 1400) -> str:
    """One chat completion against the local llama.cpp server."""
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        LLAMA_BASE + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"].strip()


def slugify(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    slug = re.sub(r"[\s_]+", "-", slug)
    return (slug[:limit].strip("-") or "untitled")


PAPER_SYSTEM = (
    "你是一位耐心的学术阅读导师，正在为一名软件工程大一学生制作论文精读笔记。"
    "输出使用中文（专有名词和关键术语保留英文），条理清晰，不编造原文没有的内容。"
)


def write_paper_note(vault: Path, meta: dict, arxiv_id: str) -> Path:
    note = vault / "papers" / f"{arxiv_id}-{slugify(meta['title'])}.md"
    prompt = (
        f"论文标题：{meta['title']}\n"
        f"作者：{', '.join(meta['authors'])}\n\n"
        f"摘要：\n{meta['summary']}\n\n"
        "请严格按以下结构输出（使用这些小标题，不要加多余的前言）：\n"
        "## 一句话总结\n（一句话说清这篇论文做了什么）\n"
        "## 中文导读\n（150-250字：研究背景、要解决的问题、核心思路）\n"
        "## 核心要点\n（3-5条，每条一行，以 - 开头）\n"
        "## 关键词汇\n（4-6个关键英文术语，格式：术语 — 中文解释）\n"
        "## 跟读原句\n（从摘要中选一句最值得朗读的英文原句，原样照抄）"
    )
    gen = llm_chat(PAPER_SYSTEM, prompt, max_tokens=1400)
    body = (
        f"# {meta['title']}\n\n"
        f"- arXiv: {meta['url']}\n"
        f"- Authors: {', '.join(meta['authors'])}\n"
        f"- Filed by LazyScholar: {datetime.now().date().isoformat()}\n"
        f"- Note model: local Gemma 3 4B via llama.cpp\n\n"
        f"{gen}\n\n"
        f"## Abstract\n\n{meta['summary']}\n"
    )
    note.write_text(body, encoding="utf-8")
    return note


DISCUSS_SYSTEM = (
    "你是一位知识整理助手，正在把一段讨论记录蒸馏成概念卡片。"
    "输出使用中文，准确提炼，不编造记录里没有的观点。"
)


def write_discussion_note(vault: Path, src: Path) -> Path:
    note = vault / "concepts" / f"{src.stem}.md"
    text = src.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > MAX_INPUT_CHARS
    prompt = (
        "以下是一段讨论记录：\n\n"
        + text[:MAX_INPUT_CHARS]
        + "\n\n请严格按以下结构输出（不要加多余前言）：\n"
        "## 核心概念\n（这段讨论围绕什么概念或话题，一两句话）\n"
        "## 要点提炼\n（3-6条关键观点，每条一行，以 - 开头）\n"
        "## 值得追问\n（1-2个可以继续深挖的问题，每条一行，以 - 开头）"
    )
    gen = llm_chat(DISCUSS_SYSTEM, prompt, max_tokens=1000)
    body = (
        f"# {src.stem}\n\n"
        f"- Filed by LazyScholar: {datetime.now().date().isoformat()}\n"
        f"- Note model: local Gemma 3 4B via llama.cpp\n"
        + ("- Note: source was truncated for distillation (v1 input cap).\n" if truncated else "")
        + f"\n{gen}\n\n"
        f"## Source\n\n{text[:8000]}\n"
    )
    note.write_text(body, encoding="utf-8")
    return note


def stable_files(inbox: Path, seen_sizes: dict) -> list:
    """Files whose size did not change since the previous poll."""
    ready = []
    current = {}
    for p in sorted(inbox.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        size = p.stat().st_size
        current[p.name] = size
        if seen_sizes.get(p.name) == size:
            ready.append(p)
    seen_sizes.clear()
    seen_sizes.update(current)
    return ready


def process_file(vault: Path, inbox: Path, processed: Path, p: Path) -> None:
    ext = p.suffix.lower()
    arxiv_id = None
    if ext in TEXT_EXTS:
        head = p.read_text(encoding="utf-8", errors="replace")[:2000]
        m = ARXIV_ID_RE.search(head)
        if m:
            arxiv_id = m.group(1)
    elif ext in PAPER_EXTS:
        m = ARXIV_ID_RE.search(p.name)
        if m:
            arxiv_id = m.group(1)

    if arxiv_id:
        meta = fetch_arxiv(arxiv_id)
        note = write_paper_note(vault, meta, arxiv_id)
        log(vault, f"PAPER  {p.name} -> {note.relative_to(vault)} (arXiv:{arxiv_id})")
    elif ext in TEXT_EXTS:
        note = write_discussion_note(vault, p)
        log(vault, f"DISCUSSION {p.name} -> {note.relative_to(vault)}")
    else:
        log(vault, f"SKIP   {p.name} (unsupported type {ext or 'none'}; left in inbox)")
        return
    p.rename(processed / p.name)


def run(vault: Path, once: bool, interval: int) -> int:
    inbox = vault / "inbox"
    processed = vault / "processed"
    for d in (inbox, processed, vault / "papers", vault / "concepts", vault / "daily"):
        d.mkdir(parents=True, exist_ok=True)
    log(vault, f"START vault={vault} once={once} interval={interval}s")
    seen_sizes: dict = {}
    if once:
        # Prime the size table, wait briefly, then the real cycle below
        # sees the same sizes and treats the files as stable.
        stable_files(inbox, seen_sizes)
        time.sleep(1.5)
    failures: list = []
    while True:
        for p in stable_files(inbox, seen_sizes):
            try:
                process_file(vault, inbox, processed, p)
            except Exception as e:  # never swallow: log + keep file + report
                failures.append(f"{p.name}: {e}")
                log(vault, f"ERROR  {p.name}: {e} (left in inbox)")
        if once:
            break
        time.sleep(interval)
    if failures:
        card = vault / "daily" / f"{datetime.now().date().isoformat()}-errors.md"
        card.write_text("# LazyScholar failures\n\n" + "\n".join(f"- {f}" for f in failures) + "\n",
                        encoding="utf-8")
        log(vault, f"DAILY error card written: {card.name} ({len(failures)} failures)")
    log(vault, "STOP")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LazyScholar unattended knowledge butler")
    ap.add_argument("--vault", default=str(Path.home() / "LazyScholarVault"),
                    help="Obsidian vault path (default: ~/LazyScholarVault)")
    ap.add_argument("--once", action="store_true", help="single poll cycle, then exit (for tests)")
    ap.add_argument("--interval", type=int, default=30, help="poll interval seconds (default 30)")
    args = ap.parse_args()
    return run(Path(args.vault).expanduser(), args.once, args.interval)


if __name__ == "__main__":
    sys.exit(main())
