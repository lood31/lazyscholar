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


def _ensure_vault(vault: Path) -> None:
    for d in ("inbox", "processed", "papers", "concepts", "daily"):
        (vault / d).mkdir(parents=True, exist_ok=True)


def run(vault: Path, once: bool, interval: int, ui: bool = True,
        open_ui: bool = False) -> int:
    inbox = vault / "inbox"
    processed = vault / "processed"
    _ensure_vault(vault)
    log(vault, f"START vault={vault} once={once} interval={interval}s")
    if not once and ui:
        threading.Thread(target=_serve_ui_safe, args=(vault, open_ui),
                         daemon=True).start()
    if not once:
        threading.Thread(target=_scheduler_loop, args=(vault,),
                         daemon=True).start()
    _poll_loop(vault, interval, once=once)
    log(vault, "STOP")
    return 0


def _poll_loop(vault: Path, interval: int, once: bool = False) -> None:
    inbox = vault / "inbox"
    processed = vault / "processed"
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


# ---------------------------------------------------------------------------
# Built-in web app: a sidebar-style reading room over the same vault files.
# Obsidian becomes optional; this page IS the app. Stdlib only, localhost.
# ---------------------------------------------------------------------------

import html as _html
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UI_PORT = int(os.environ.get("LAZYSCHOLAR_UI_PORT", "8765"))

UI_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LazyScholar · 懒人学者</title>
<style>
:root{--paper:#f6f1e6;--card:#fffdf7;--ink:#33302a;--muted:#97897a;--accent:#d98a2b;--accent2:#8a6642;--line:#eadfc9;--soft:#fbf7ec;--ok:#43a97b;--bad:#d95f43}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--paper);color:var(--ink)}
#app{display:grid;grid-template-columns:308px 1fr;height:100vh}
#side{background:var(--soft);border-right:1px solid var(--line);padding:20px 16px;overflow-y:auto;display:flex;flex-direction:column;gap:6px}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:6px}
.sloth{font-size:32px;background:var(--card);border:1px solid var(--line);border-radius:16px;width:56px;height:56px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(138,102,66,.08)}
.brand h1{font-size:19px;margin:0}
.brand p{margin:2px 0 0;color:var(--muted);font-size:12px}
.status{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 12px;width:fit-content}
.dot{width:8px;height:8px;border-radius:50%;background:var(--bad);flex:none}
.dot.ok{background:var(--ok)}
.err{background:#fdeee8;border:1px solid #f2c4b2;color:#9c3d22;border-radius:12px;padding:8px 12px;font-size:12.5px;margin-top:8px}
.sec{margin-top:16px;font-size:12px;letter-spacing:.12em;color:var(--accent2);font-weight:700}
.inbox{font-size:13px;color:var(--muted);background:#fff8ea;border:1px dashed #e5cf9e;border-radius:12px;padding:8px 12px;line-height:1.7}
.inbox .f{color:var(--ink)}
.list{display:flex;flex-direction:column;gap:8px}
.item{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;cursor:pointer;transition:all .15s ease}
.item:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(138,102,66,.12);border-color:#e2cf9f}
.item.active{border-color:var(--accent);background:#fff6e3}
.item .t{font-size:13.5px;line-height:1.45;font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.item .d{font-size:11.5px;color:var(--muted);margin-top:4px}
.none{font-size:13px;color:var(--muted);padding:2px}
.foot{margin-top:auto;padding-top:18px;font-size:12px;color:var(--muted);line-height:1.7}
#main{overflow-y:auto;padding:34px 26px 60px}
.reader{max-width:760px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:40px 46px;box-shadow:0 6px 30px rgba(138,102,66,.07)}
.empty{text-align:center;color:var(--muted);padding:56px 10px;line-height:1.9}
.empty .big{font-size:44px}
.meta{font-size:12.5px;color:var(--muted);letter-spacing:.06em;margin-bottom:6px}
#note-body h1{font-family:Georgia,"Noto Serif SC","Songti SC",serif;font-size:27px;line-height:1.35;margin:0 0 18px}
#note-body h2{font-family:Georgia,"Noto Serif SC","Songti SC",serif;font-size:19px;margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
#note-body h3{font-size:16px;margin:22px 0 8px}
#note-body p{line-height:1.85;margin:8px 0;font-size:15px}
#note-body li{line-height:1.8;margin:5px 0;font-size:15px}
#note-body ul{padding-left:22px;margin:8px 0}
#note-body blockquote{background:#fff6e3;border-left:3px solid var(--accent);margin:12px 0;padding:10px 14px;border-radius:0 12px 12px 0;color:#5c4a2f}
#note-body code{background:#f3ecdc;padding:1px 6px;border-radius:6px;font-size:.9em}
#note-body a{color:var(--accent);text-decoration:none;border-bottom:1px dotted var(--accent)}
.wikilink{color:var(--accent2);background:#f3ecdc;border-radius:6px;padding:0 5px}
@media (max-width:820px){#app{grid-template-columns:1fr;height:auto}#side{border-right:none;border-bottom:1px solid var(--line)}#main{padding:18px 12px 40px}.reader{padding:26px 20px}}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <div class="brand"><div class="sloth">🦥</div><div><h1>LazyScholar</h1><p>懒人学者 · 什么都不用你管</p></div></div>
    <div class="status"><span class="dot"></span><span class="txt">正在看看管家在不在…</span></div>
    <div id="errbox"></div>
    <div class="sec">📥 收件箱</div>
    <div id="inbox" class="inbox">…</div>
    <div class="sec">📚 论文笔记</div>
    <div id="papers" class="list"></div>
    <div class="sec">💡 概念卡片</div>
    <div id="concepts" class="list"></div>
    <div class="foot">把论文链接、PDF、聊天记录丢进 inbox<br>剩下的交给管家，你只管读 🦥</div>
  </aside>
  <main id="main">
    <div class="reader">
      <div class="empty" id="empty"><div class="big">🦥</div>从左边挑一篇笔记开始读吧。<br>还没有笔记？往 inbox 里丢一篇论文，<br>一分钟后它自己就写好了。</div>
      <article id="note" hidden>
        <div class="meta" id="note-meta"></div>
        <div id="note-body"></div>
      </article>
    </div>
  </main>
</div>
<script>
let selKey = null;
function esc(s){return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function itemHTML(kind, it){
  return '<div class="item' + (selKey === kind + '/' + it.name ? ' active' : '') + '" data-kind="' + kind + '" data-name="' + esc(it.name) + '"><div class="t">' + esc(it.title) + '</div><div class="d">' + it.date + '</div></div>';
}
function renderList(kind, items){
  const el = document.getElementById(kind);
  el.innerHTML = items.length ? items.map(it => itemHTML(kind, it)).join('') : '<div class="none">还没有</div>';
  el.querySelectorAll('.item').forEach(d => d.addEventListener('click', () => openNote(d.dataset.kind, d.dataset.name)));
}
async function openNote(kind, name){
  selKey = kind + '/' + name;
  const r = await fetch('/api/note?kind=' + encodeURIComponent(kind) + '&name=' + encodeURIComponent(name));
  if(!r.ok) return;
  const n = await r.json();
  document.getElementById('empty').hidden = true;
  const art = document.getElementById('note');
  art.hidden = false;
  document.getElementById('note-meta').textContent = (kind === 'papers' ? '📚 论文笔记' : '💡 概念卡片') + ' · ' + n.date;
  document.getElementById('note-body').innerHTML = n.html;
  document.querySelectorAll('.item').forEach(d => d.classList.toggle('active', d.dataset.kind + '/' + d.dataset.name === selKey));
  document.getElementById('main').scrollTop = 0;
  if(window.innerWidth <= 820){ document.querySelector('.reader').scrollIntoView({behavior:'smooth'}); }
}
async function refresh(){
  try{
    const r = await fetch('/api/notes');
    const s = await r.json();
    document.querySelector('.status .dot').className = 'dot' + (s.llama_up ? ' ok' : '');
    document.querySelector('.status .txt').textContent = s.llama_up ? '本地 Gemma 在线 · 随时开工' : '模型服务未启动（双击启动脚本即可）';
    document.getElementById('errbox').innerHTML = s.errors_today ? '<div class="err">⚠️ 今天有处理失败的记录，去 daily 文件夹看看错误卡</div>' : '';
    document.getElementById('inbox').innerHTML = s.inbox.length ? s.inbox.map(n => '<div class="f">📄 ' + esc(n) + '</div>').join('') + '<div>处理中，稍等片刻…</div>' : '空的——把下一篇论文丢进来吧';
    renderList('papers', s.papers);
    renderList('concepts', s.concepts);
    if(!selKey){
      if(s.papers.length) openNote('papers', s.papers[0].name);
      else if(s.concepts.length) openNote('concepts', s.concepts[0].name);
    }
  }catch(e){ /* server hiccup; next refresh retries */ }
}
refresh();
setInterval(refresh, 25000);
</script>
</body>
</html>
"""


def md_to_html(md: str) -> str:
    """Tiny renderer for the Markdown subset LazyScholar notes use."""
    def inline(s: str) -> str:
        s = _html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\[\[(.+?)\]\]", r"<span class='wikilink'>\1</span>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                   r"<a href='\2' target='_blank' rel='noopener'>\1</a>", s)
        return s

    out, in_list = [], False
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line.strip():
            continue
        if line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<blockquote>{inline(line[2:])}</blockquote>")
        else:
            out.append(f"<p>{inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _note_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem


def vault_state(vault: Path) -> dict:
    def entries(kind: str) -> list:
        d = vault / kind
        items = []
        if d.exists():
            for p in d.glob("*.md"):
                st = p.stat()
                items.append({
                    "name": p.name,
                    "title": _note_title(p),
                    "date": datetime.fromtimestamp(st.st_mtime).date().isoformat(),
                    "mtime": st.st_mtime,
                })
        return sorted(items, key=lambda x: -x["mtime"])

    inbox_dir = vault / "inbox"
    inbox = (sorted(p.name for p in inbox_dir.iterdir() if p.is_file())
             if inbox_dir.exists() else [])
    today_err = vault / "daily" / f"{datetime.now().date().isoformat()}-errors.md"
    llama_up = False
    try:
        with urllib.request.urlopen(LLAMA_BASE + "/health", timeout=1.5) as r:
            llama_up = r.status == 200
    except Exception:
        llama_up = False
    return {
        "papers": entries("papers"),
        "concepts": entries("concepts"),
        "inbox": inbox,
        "errors_today": today_err.exists(),
        "llama_up": llama_up,
    }


# ---------------------------------------------------------------------------
# v2 app backend: config, queue, scheduler, model lifecycle, reader data.
# ---------------------------------------------------------------------------

import subprocess
import uuid

CONFIG_DEFAULTS = {
    "llama_exe": "",
    "model_path": "",
    "port": 8080,
    "ctx": 16384,
    "schedule": "23:30",
    "theme": "light",
    "font_size": 16,
    "serif": True,
}


def _default_llama_exe() -> str:
    alias = Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "llama.exe"
    return str(alias) if alias.exists() else "llama"


def _default_model_path() -> str:
    p = Path("D:/models/gemma-3-4b-it-qat-Q4_0.gguf")
    return str(p) if p.exists() else ""


def load_config(vault: Path) -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    cfg["llama_exe"] = _default_llama_exe()
    cfg["model_path"] = _default_model_path()
    f = vault / "config.json"
    if f.exists():
        try:
            cfg.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(vault: Path, cfg: dict) -> dict:
    merged = load_config(vault)
    for k in CONFIG_DEFAULTS:
        if k in cfg:
            merged[k] = cfg[k]
    (vault / "config.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


_queue_lock = threading.Lock()


def load_queue(vault: Path) -> list:
    f = vault / "queue.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_queue(vault: Path, items: list) -> None:
    (vault / "queue.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def add_queue_item(vault: Path, kind: str, value: str) -> dict:
    item = {
        "id": uuid.uuid4().hex[:10],
        "kind": kind,
        "value": value,
        "label": Path(value).name if kind == "pdf" else value,
        "status": "pending",
        "added": datetime.now().isoformat(timespec="seconds"),
        "note": None,
        "error": None,
    }
    if kind == "pdf":
        src = Path(value)
        if not src.exists():
            raise ValueError(f"找不到这个 PDF 文件：{value}")
        qdir = vault / "queue_files"
        qdir.mkdir(parents=True, exist_ok=True)
        dest = qdir / f"{item['id']}.pdf"
        dest.write_bytes(src.read_bytes())
        item["stored"] = dest.name
    with _queue_lock:
        items = load_queue(vault)
        items.append(item)
        save_queue(vault, items)
    return item


def llama_health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


_server = {"proc": None}


def start_server(vault: Path, cfg: dict) -> bool:
    if llama_health(int(cfg["port"])):
        return True
    exe = cfg.get("llama_exe") or "llama"
    model = cfg.get("model_path") or ""
    if not model:
        log(vault, "MODEL start requested but no model_path configured")
        return False
    try:
        _server["proc"] = subprocess.Popen(
            [exe, "serve", "-m", model, "-c", str(cfg.get("ctx", 16384)),
             "--port", str(cfg["port"])],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(vault, f"MODEL start failed: {e}")
        return False
    for _ in range(40):
        if llama_health(int(cfg["port"])):
            log(vault, f"MODEL server up on :{cfg['port']}")
            return True
        time.sleep(2)
    log(vault, "MODEL server did not become healthy in 80s")
    return False


def stop_server(vault: Path) -> bool:
    proc = _server.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        _server["proc"] = None
        log(vault, "MODEL server stopped")
        return True
    return False


_batch = {"running": False, "last_run": ""}


def _process_queue_item(vault: Path, item: dict) -> str:
    text = item["value"] if item["kind"] == "link" else item.get("label", "")
    m = ARXIV_ID_RE.search(text)
    if not m:
        raise ValueError("没有找到 arXiv 编号（v1 需要带 arXiv 编号的论文）")
    arxiv_id = m.group(1)
    meta = fetch_arxiv(arxiv_id)
    note = write_paper_note(vault, meta, arxiv_id)
    if item["kind"] == "pdf":
        stored = vault / "queue_files" / item.get("stored", "")
        if stored.exists():
            dest = vault / "processed" / f"{item['id']}-{item.get('label', 'paper.pdf')}"
            stored.rename(dest)
    return note.name


def process_queue(vault: Path) -> dict:
    global LLAMA_BASE
    if _batch["running"]:
        return {"ok": False, "msg": "正在处理中"}
    _batch["running"] = True
    done = failed = 0
    try:
        cfg = load_config(vault)
        LLAMA_BASE = f"http://127.0.0.1:{int(cfg['port'])}"
        items = load_queue(vault)
        pending = [i for i in items if i["status"] == "pending"]
        if not pending:
            return {"ok": True, "processed": 0, "failed": 0}
        started_here = False
        if not llama_health(int(cfg["port"])):
            if not start_server(vault, cfg):
                raise RuntimeError("模型服务启动失败，去「本地模型」页看看配置")
            started_here = True
        for item in pending:
            item["status"] = "processing"
            save_queue(vault, items)
            try:
                item["note"] = _process_queue_item(vault, item)
                item["status"] = "done"
                item["error"] = None
                done += 1
            except Exception as e:
                item["status"] = "failed"
                item["error"] = str(e)[:300]
                failed += 1
            save_queue(vault, items)
        if started_here:
            stop_server(vault)
        _batch["last_run"] = datetime.now().isoformat(timespec="seconds")
        log(vault, f"BATCH done: {done} ok, {failed} failed")
        return {"ok": True, "processed": done, "failed": failed}
    except Exception as e:
        log(vault, f"BATCH error: {e}")
        return {"ok": False, "msg": str(e)[:300]}
    finally:
        _batch["running"] = False


def _scheduler_loop(vault: Path) -> None:
    while True:
        try:
            cfg = load_config(vault)
            now = datetime.now()
            if now.strftime("%H:%M") == str(cfg.get("schedule", "23:30"))[:5]:
                if _batch["last_run"][:10] != now.date().isoformat():
                    items = load_queue(vault)
                    if any(i["status"] == "pending" for i in items):
                        log(vault, "SCHEDULED batch run starting")
                        process_queue(vault)
        except Exception as e:
            log(vault, f"scheduler error: {e}")
        time.sleep(30)


def load_highlights(vault: Path, arxiv_id: str) -> dict:
    f = vault / "highlights" / f"{arxiv_id}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"highlights": []}


def save_highlights(vault: Path, arxiv_id: str, data: dict) -> dict:
    d = vault / "highlights"
    d.mkdir(parents=True, exist_ok=True)
    clean = {"highlights": data.get("highlights", [])[:500]}
    (d / f"{arxiv_id}.json").write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def get_pdf_path(vault: Path, arxiv_id: str) -> Path:
    d = vault / "papers_pdf"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{arxiv_id}.pdf"
    if not p.exists():
        req = urllib.request.Request(
            f"https://arxiv.org/pdf/{arxiv_id}",
            headers={"User-Agent": "LazyScholar/0.2 (personal research tool)"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
        if not data.startswith(b"%PDF"):
            raise ValueError("arXiv 没有返回 PDF（这篇论文可能没有 PDF 版）")
        p.write_bytes(data)
    return p


def _vendor_dir() -> Path:
    cands = []
    if getattr(sys, "_MEIPASS", None):
        cands.append(Path(sys._MEIPASS) / "vendor")
    cands.append(Path(__file__).resolve().parent / "vendor")
    for c in cands:
        if c.exists():
            return c
    return cands[-1]


BALL_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:transparent;overflow:hidden}
#b{width:58px;height:58px;border-radius:50%;background:#B46A2B;color:#fff;font-size:29px;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 14px rgba(16,19,24,.35);border:2px solid #F8F6F0;margin:3px;user-select:none}
#b:active{transform:scale(.93)}
</style></head><body>
<div id="b" title="LazyScholar · 点我展开，右键退出">🦥</div>
<script>
document.getElementById('b').addEventListener('click',()=>{ if(window.pywebview&&window.pywebview.api) window.pywebview.api.toggle_main(); });
document.addEventListener('contextmenu',e=>{ e.preventDefault(); if(window.pywebview&&window.pywebview.api) window.pywebview.api.quit_app(); });
</script></body></html>
"""

APP_PAGE = r"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LazyScholar · 懒人学者</title>
<style>
:root{--ink:#101318;--bg:#F8F6F0;--card:#FFFDF7;--text:#1B1E24;--muted:#8B8378;--copper:#B46A2B;--copper2:#E09A4C;--line:#E9E0CF;--banner:#F0D9BA;--soft:#FBF7EC}
[data-theme="dark"]{--bg:#101114;--card:#191A1F;--text:#F2F0E9;--muted:#9A937F;--line:#2E2C26;--banner:#3A2C17;--soft:#17181C}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text)}
#app{display:grid;grid-template-columns:1fr 62px;height:100vh;box-shadow:-14px 0 34px rgba(16,19,24,.28)}
#rail{grid-column:2;grid-row:1;background:var(--ink);display:flex;flex-direction:column;align-items:center;padding:12px 0;gap:8px}
#content{grid-column:1;grid-row:1}
#rail .logo{font-size:26px;background:var(--card);border-radius:14px;width:42px;height:42px;display:flex;align-items:center;justify-content:center;margin-bottom:10px}
#rail button{width:48px;padding:7px 0 5px;border:none;border-radius:12px;background:transparent;color:#CFC9BC;font-size:17px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:1px}
#rail button span{font-size:10px}
#rail button:hover{background:#1E2126}
#rail button.on{background:#26221B;color:var(--copper2);box-shadow:inset -2px 0 0 var(--copper)}
.railfoot{margin-top:auto;display:flex;flex-direction:column;align-items:center;gap:8px}
#rail .mini{width:38px;padding:5px 0;font-size:14px}
#dot{width:10px;height:10px;border-radius:50%;background:#B3402A;display:block}
#dot.ok{background:#7A9A52}
#content{overflow-y:auto;padding:24px 26px 50px}
.panel{display:none;max-width:860px;margin:0 auto}
.panel.on{display:block}
h1{font-size:23px;margin:4px 0 6px;letter-spacing:.01em}
.sub{color:var(--muted);font-size:13.5px;line-height:1.7;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px 18px;margin-bottom:14px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
input[type=text],input:not([type]),input[type=number],select{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:8px 10px;color:var(--text);font-size:13.5px;flex:1;min-width:120px}
input[type=time]{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:7px 9px;color:var(--text)}
.btn{background:var(--ink);color:#F8F6F0;border:none;border-radius:10px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}
.btn.copper{background:var(--copper)}
.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.btn:active{transform:translateY(1px)}
.msg{font-size:13px;color:var(--copper);min-height:18px}
.frow{display:flex;align-items:center;gap:10px;margin:8px 0}
.frow label{width:88px;flex:none;font-size:13px;color:var(--muted)}
.frow input{flex:1}
.qitem{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 14px;margin-bottom:8px}
.qitem .lbl{flex:1;font-size:13.5px;word-break:break-all}
.qitem .err{color:#B3402A;font-size:12px}
.chip{font-size:11px;font-weight:700;border-radius:999px;padding:3px 9px;background:var(--banner);color:#7A5510;flex:none}
.chip.done{background:var(--ink);color:#F8F6F0}
.chip.fail{background:#B3402A;color:#fff}
.chip.proc{background:var(--copper);color:#fff}
.qitem .del{background:none;border:none;color:var(--muted);cursor:pointer;font-size:15px}
.nitem{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:11px 14px;margin-bottom:8px;cursor:pointer}
.nitem:hover{border-color:var(--copper2)}
.nitem .t{font-weight:600;font-size:14px;line-height:1.5}
.nitem .d{color:var(--muted);font-size:12px;margin-top:3px}
#n-body h1{font-family:Georgia,"Noto Serif SC","Songti SC",serif;font-size:24px;line-height:1.35}
#n-body h2{font-family:Georgia,"Noto Serif SC","Songti SC",serif;font-size:18px;margin:24px 0 8px;padding-bottom:5px;border-bottom:1px solid var(--line)}
#n-body p{line-height:1.85;margin:7px 0}
#n-body li{line-height:1.8;margin:4px 0}
#n-body blockquote{background:var(--banner);border-left:3px solid var(--copper);margin:10px 0;padding:9px 13px;border-radius:0 12px 12px 0}
#n-body code{background:var(--soft);padding:1px 6px;border-radius:6px}
#n-body a{color:var(--copper)}
.meta{font-size:12px;color:var(--muted);margin-bottom:4px}
#reader{position:fixed;inset:0;background:var(--bg);z-index:60;display:none;flex-direction:column}
#reader.on{display:flex}
.rhead{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card)}
#pages{overflow-y:auto;padding:16px 8px 60px}
.page{position:relative;margin:0 auto 16px;background:#fff;box-shadow:0 2px 10px rgba(16,19,24,.18)}
.page canvas{display:block}
.textLayer{position:absolute;inset:0;overflow:hidden;line-height:1}
.textLayer span{position:absolute;white-space:pre;color:transparent;transform-origin:0% 0%}
.textLayer ::selection{background:rgba(180,106,43,.35)}
.hl{position:absolute;background:rgba(217,154,76,.45);border-radius:2px;pointer-events:none}
</style></head>
<body>
<div id="app">
  <nav id="rail">
    <div class="logo pywebview-drag-region" title="拖动我可以移动面板">🦥</div>
    <button data-tab="queue" class="on">📥<span>队列</span></button>
    <button data-tab="notes">📝<span>笔记</span></button>
    <button data-tab="model">🦙<span>模型</span></button>
    <button data-tab="settings">⚙️<span>设置</span></button>
    <div class="railfoot">
      <button class="mini" id="win-hide" title="收起面板">—</button>
      <button class="mini" id="win-quit" title="退出 LazyScholar">⏻</button>
      <span id="dot" title="模型状态"></span>
    </div>
  </nav>
  <main id="content">
    <section id="tab-queue" class="panel on">
      <h1>论文队列</h1>
      <p class="sub">把论文先排队，不用等。每天 <b id="q-sched">23:30</b> 管家会自己把模型打开、批量处理完再关掉；等不及就点「立即处理」。</p>
      <div class="card">
        <div class="row"><input id="link-in" placeholder="粘贴 arXiv 链接，如 https://arxiv.org/abs/2510.16720"><button class="btn" id="add-link">＋ 加入队列</button></div>
        <div class="row"><button class="btn ghost" id="pick-pdf">选择本地 PDF…</button><button class="btn copper" id="run-now">立即处理队列</button></div>
        <div id="q-msg" class="msg"></div>
      </div>
      <div id="q-list"></div>
    </section>
    <section id="tab-notes" class="panel">
      <h1>笔记卡片</h1>
      <p class="sub">管家写好的都在这里。点开看全文；论文笔记还能直接进内置阅读器读原文、划线。</p>
      <div id="n-list"></div>
      <article id="n-view" class="card" hidden>
        <div class="meta" id="n-meta"></div>
        <div id="n-body"></div>
        <div class="row"><button class="btn copper" id="read-pdf" hidden>📖 在阅读器里读这篇论文</button></div>
      </article>
    </section>
    <section id="tab-model" class="panel">
      <h1>本地模型</h1>
      <p class="sub">状态：<b id="m-state">…</b></p>
      <div class="card">
        <div class="frow"><label>llama 程序</label><input id="m-exe"></div>
        <div class="frow"><label>模型文件</label><input id="m-model"></div>
        <div class="frow"><label>端口</label><input id="m-port" type="number"></div>
        <div class="frow"><label>上下文</label><input id="m-ctx" type="number"></div>
        <div class="row"><button class="btn" id="m-save">保存配置</button><button class="btn copper" id="m-start">启动模型</button><button class="btn ghost" id="m-stop">停止</button><button class="btn ghost" id="m-test">测试连接</button></div>
        <div id="m-msg" class="msg"></div>
      </div>
    </section>
    <section id="tab-settings" class="panel">
      <h1>设置</h1>
      <div class="card">
        <div class="frow"><label>定时处理</label><input id="s-sched" type="time"></div>
        <div class="frow"><label>主题</label><select id="s-theme"><option value="light">白天</option><option value="dark">黑夜</option></select></div>
        <div class="frow"><label>阅读字号</label><input id="s-font" type="range" min="14" max="20" step="1" style="flex:1"></div>
        <div class="frow"><label>衬线字体</label><input id="s-serif" type="checkbox"></div>
        <div class="frow"><label>仓库位置</label><input id="s-vault" disabled></div>
        <div class="row"><button class="btn" id="s-save">保存设置</button></div>
        <div id="s-msg" class="msg"></div>
      </div>
    </section>
  </main>
</div>
<div id="reader">
  <div class="rhead"><button class="btn ghost" id="r-back">← 返回笔记</button><b id="r-title"></b><span class="sub" style="margin:0">选中文字即可划线，高亮会自动保存</span></div>
  <div id="pages"></div>
</div>
<script src="/vendor/pdf.min.js"></script>
<script>
let CFG = {}, curArxiv = null, curNoteName = '';
const $ = id => document.getElementById(id);
function esc(s){return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function jget(u){ const r = await fetch(u); return r.json(); }
async function jpost(u, body){ const r = await fetch(u, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})}); return r.json(); }

document.querySelectorAll('#rail button[data-tab]').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#rail button[data-tab]').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('on', p.id === 'tab-' + b.dataset.tab));
  if(b.dataset.tab === 'notes') loadNotes();
  if(b.dataset.tab === 'queue') loadQueue();
}));
$('win-hide').addEventListener('click', () => { if(window.pywebview && window.pywebview.api) window.pywebview.api.hide_main(); });
$('win-quit').addEventListener('click', () => { if(window.pywebview && window.pywebview.api) window.pywebview.api.quit_app(); });

function applyPrefs(){
  document.documentElement.dataset.theme = CFG.theme || 'light';
  const nb = $('n-body');
  nb.style.fontSize = (CFG.font_size || 16) + 'px';
  nb.style.fontFamily = CFG.serif === false ? 'inherit' : 'Georgia,"Noto Serif SC","Songti SC",serif';
}
async function loadConfig(){
  CFG = await jget('/api/config');
  $('m-exe').value = CFG.llama_exe || ''; $('m-model').value = CFG.model_path || '';
  $('m-port').value = CFG.port || 8080; $('m-ctx').value = CFG.ctx || 16384;
  $('s-sched').value = CFG.schedule || '23:30'; $('s-theme').value = CFG.theme || 'light';
  $('s-font').value = CFG.font_size || 16; $('s-serif').checked = CFG.serif !== false;
  $('q-sched').textContent = CFG.schedule || '23:30';
  applyPrefs();
}
async function loadState(){
  try{
    const s = await jget('/api/state');
    $('dot').className = s.llama_up ? 'ok' : ''; $('dot').id = 'dot';
    $('m-state').textContent = s.llama_up ? '在线，随时能干活' : '未启动（处理队列时会自动打开）';
    if(s.vault) $('s-vault').value = s.vault;
    if(s.batch_running) $('q-msg').textContent = '正在处理队列…';
  }catch(e){}
}
const CHIP = {pending:['chip','等待中'], processing:['chip proc','处理中'], done:['chip done','已完成'], failed:['chip fail','失败']};
async function loadQueue(){
  const items = await jget('/api/queue');
  $('q-list').innerHTML = items.length ? items.map(it => {
    const c = CHIP[it.status] || CHIP.pending;
    return '<div class="qitem"><span class="' + c[0] + '">' + c[1] + '</span><span class="lbl">' + esc(it.label || it.value) +
      (it.error ? '<br><span class="err">' + esc(it.error) + '</span>' : '') +
      (it.note ? '<br><span style="color:var(--muted);font-size:12px">→ ' + esc(it.note) + '</span>' : '') +
      '</span>' + (it.status !== 'processing' ? '<button class="del" data-id="' + it.id + '" title="移除">✕</button>' : '') + '</div>';
  }).join('') : '<p class="sub">队列是空的。把下一篇论文丢进来排队吧。</p>';
  document.querySelectorAll('.qitem .del').forEach(b => b.addEventListener('click', async () => { await jpost('/api/queue/delete', {id: b.dataset.id}); loadQueue(); }));
}
$('add-link').addEventListener('click', async () => {
  const v = $('link-in').value.trim(); if(!v) return;
  const r = await jpost('/api/queue', {kind:'link', value:v});
  $('q-msg').textContent = r.ok ? '已加入队列 🦥' : ('出错了：' + (r.msg || ''));
  if(r.ok){ $('link-in').value = ''; loadQueue(); }
});
$('pick-pdf').addEventListener('click', async () => {
  let p = null;
  if(window.pywebview && window.pywebview.api && window.pywebview.api.pick_pdf){ p = await window.pywebview.api.pick_pdf(); }
  else { p = prompt('粘贴本地 PDF 的完整路径：'); }
  if(!p) return;
  const r = await jpost('/api/queue', {kind:'pdf', value:p});
  $('q-msg').textContent = r.ok ? 'PDF 已加入队列 🦥' : ('出错了：' + (r.msg || ''));
  if(r.ok) loadQueue();
});
$('run-now').addEventListener('click', async () => {
  $('q-msg').textContent = '已开始处理，模型启动需要十几秒…';
  const r = await jpost('/api/queue/run', {});
  $('q-msg').textContent = r.ok ? '处理完成，看看笔记页' : ('处理结束：' + (r.msg || '部分失败，看队列里的红色标记'));
  loadQueue(); loadState();
});
async function loadNotes(){
  const s = await jget('/api/notes');
  const all = s.papers.map(p => ({...p, kind:'papers'})).concat(s.concepts.map(c => ({...c, kind:'concepts'})));
  all.sort((a, b) => b.mtime - a.mtime);
  $('n-list').innerHTML = all.length ? all.map(n => '<div class="nitem" data-kind="' + n.kind + '" data-name="' + esc(n.name) + '"><div class="t">' + (n.kind === 'papers' ? '📚 ' : '💡 ') + esc(n.title) + '</div><div class="d">' + n.date + '</div></div>').join('') : '<p class="sub">还没有笔记。</p>';
  document.querySelectorAll('.nitem').forEach(d => d.addEventListener('click', () => openNote(d.dataset.kind, d.dataset.name)));
}
async function openNote(kind, name){
  const n = await jget('/api/note?kind=' + encodeURIComponent(kind) + '&name=' + encodeURIComponent(name));
  $('n-view').hidden = false;
  $('n-meta').textContent = (kind === 'papers' ? '📚 论文笔记' : '💡 概念卡片') + ' · ' + n.date;
  $('n-body').innerHTML = n.html;
  curArxiv = n.arxiv || null; curNoteName = n.title || '';
  $('read-pdf').hidden = !curArxiv;
  $('n-view').scrollIntoView({behavior:'smooth'});
}
$('m-save').addEventListener('click', async () => {
  await jpost('/api/config', {llama_exe: $('m-exe').value.trim(), model_path: $('m-model').value.trim(), port: parseInt($('m-port').value || '8080'), ctx: parseInt($('m-ctx').value || '16384')});
  $('m-msg').textContent = '配置已保存'; loadConfig();
});
$('m-start').addEventListener('click', async () => { $('m-msg').textContent = '正在启动模型，十几秒…'; const r = await jpost('/api/model/start', {}); $('m-msg').textContent = r.ok ? '模型已启动' : ('启动失败：' + (r.msg || '')); loadState(); });
$('m-stop').addEventListener('click', async () => { await jpost('/api/model/stop', {}); $('m-msg').textContent = '已停止（如果是别的方式启动的模型，这里停不掉它）'; loadState(); });
$('m-test').addEventListener('click', async () => { const s = await jget('/api/state'); $('m-msg').textContent = s.llama_up ? '连接正常，模型在线 ✅' : '连不上，模型没在运行'; });
$('s-save').addEventListener('click', async () => {
  CFG = await jpost('/api/config', {schedule: $('s-sched').value || '23:30', theme: $('s-theme').value, font_size: parseInt($('s-font').value || '16'), serif: $('s-serif').checked});
  $('s-msg').textContent = '设置已保存'; $('q-sched').textContent = CFG.schedule; applyPrefs();
});
$('s-theme').addEventListener('change', () => { document.documentElement.dataset.theme = $('s-theme').value; });

/* ---------------- PDF reader with highlights ---------------- */
let pdfDoc = null, hlData = {highlights: []};
pdfjsLib.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.js';
$('read-pdf').addEventListener('click', () => openReader());
$('r-back').addEventListener('click', () => { $('reader').classList.remove('on'); });
async function openReader(){
  if(!curArxiv) return;
  $('reader').classList.add('on');
  $('r-title').textContent = curNoteName;
  $('pages').innerHTML = '<p class="sub" style="text-align:center">正在从 arXiv 取 PDF…（第一次会慢一点，之后有缓存）</p>';
  try{
    hlData = await jget('/api/highlights?id=' + encodeURIComponent(curArxiv));
    const buf = await (await fetch('/api/pdf?id=' + encodeURIComponent(curArxiv))).arrayBuffer();
    pdfDoc = await pdfjsLib.getDocument({data: buf}).promise;
    $('pages').innerHTML = '';
    for(let i = 1; i <= pdfDoc.numPages; i++){ await renderPage(i); }
  }catch(e){ $('pages').innerHTML = '<p class="sub" style="text-align:center">PDF 打开失败：' + esc(String(e)) + '</p>'; }
}
async function renderPage(num){
  const page = await pdfDoc.getPage(num);
  const cont = $('pages');
  const base = page.getViewport({scale: 1});
  const scale = Math.min(860, cont.clientWidth - 24) / base.width;
  const vp = page.getViewport({scale});
  const wrap = document.createElement('div');
  wrap.className = 'page'; wrap.dataset.page = num;
  wrap.style.width = vp.width + 'px'; wrap.style.height = vp.height + 'px';
  const cv = document.createElement('canvas');
  cv.width = vp.width; cv.height = vp.height;
  wrap.appendChild(cv);
  const tl = document.createElement('div'); tl.className = 'textLayer'; wrap.appendChild(tl);
  cont.appendChild(wrap);
  await page.render({canvasContext: cv.getContext('2d'), viewport: vp}).promise;
  const tc = await page.getTextContent();
  pdfjsLib.renderTextLayer({textContentSource: tc, container: tl, viewport: vp, textDivs: []});
  drawHighlights(wrap, num);
}
function drawHighlights(wrap, num){
  wrap.querySelectorAll('.hl').forEach(e => e.remove());
  hlData.highlights.filter(h => h.page === num).forEach(h => {
    h.rects.forEach(r => {
      const d = document.createElement('div'); d.className = 'hl';
      d.style.left = (r[0] * 100) + '%'; d.style.top = (r[1] * 100) + '%';
      d.style.width = (r[2] * 100) + '%'; d.style.height = (r[3] * 100) + '%';
      wrap.appendChild(d);
    });
  });
}
document.addEventListener('mouseup', async () => {
  const sel = window.getSelection();
  if(!sel || sel.isCollapsed || !curArxiv || !$('reader').classList.contains('on')) return;
  const range = sel.getRangeAt(0);
  let node = range.commonAncestorContainer;
  while(node && !(node.classList && node.classList.contains('page'))) node = node.parentNode;
  if(!node) return;
  const pageNum = parseInt(node.dataset.page);
  const box = node.getBoundingClientRect();
  const rects = [];
  for(const r of range.getClientRects()){
    if(r.width < 2 || r.height < 2) continue;
    rects.push([(r.left - box.left) / box.width, (r.top - box.top) / box.height, r.width / box.width, r.height / box.height]);
  }
  if(!rects.length) return;
  hlData.highlights.push({page: pageNum, text: sel.toString().slice(0, 300), rects, ts: Date.now()});
  hlData = await jpost('/api/highlights?id=' + encodeURIComponent(curArxiv), hlData);
  drawHighlights(node, pageNum);
  sel.removeAllRanges();
});

loadConfig(); loadState(); loadQueue();
setInterval(() => { loadState(); loadQueue(); }, 20000);
</script>
</body></html>
"""


def serve_ui(vault: Path, open_browser: bool = False) -> None:
    page = UI_PAGE.encode("utf-8")
    app_page = APP_PAGE.encode("utf-8")
    ball_page = BALL_PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _body_json(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def do_GET(self) -> None:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif u.path == "/app":
                self._send(200, app_page, "text/html; charset=utf-8")
            elif u.path == "/ball":
                self._send(200, ball_page, "text/html; charset=utf-8")
            elif u.path.startswith("/vendor/"):
                name = Path(u.path).name
                f = _vendor_dir() / name
                if f.exists():
                    ctype = ("application/javascript"
                             if name.endswith(".js") else "application/octet-stream")
                    self._send(200, f.read_bytes(), ctype)
                else:
                    self._send(404, b"vendor file missing", "text/plain; charset=utf-8")
            elif u.path == "/api/notes":
                self._json(vault_state(vault))
            elif u.path == "/api/note":
                kind = q.get("kind", [""])[0]
                name = Path(q.get("name", [""])[0]).name
                if kind not in ("papers", "concepts") or not name.endswith(".md"):
                    self._send(400, b"bad request", "text/plain; charset=utf-8")
                    return
                p = vault / kind / name
                if not p.exists():
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                md = p.read_text(encoding="utf-8", errors="replace")
                m = ARXIV_ID_RE.search(md)
                payload = {
                    "title": _note_title(p),
                    "kind": kind,
                    "date": datetime.fromtimestamp(p.stat().st_mtime).date().isoformat(),
                    "html": md_to_html(md),
                    "arxiv": m.group(1) if m else None,
                }
                self._json(payload)
            elif u.path == "/api/state":
                cfg = load_config(vault)
                self._json({
                    "llama_up": llama_health(int(cfg["port"])),
                    "managed": (_server["proc"] is not None
                                and _server["proc"].poll() is None),
                    "batch_running": _batch["running"],
                    "last_run": _batch["last_run"],
                    "pending": sum(1 for i in load_queue(vault)
                                   if i["status"] == "pending"),
                    "vault": str(vault),
                })
            elif u.path == "/api/config":
                self._json(load_config(vault))
            elif u.path == "/api/queue":
                self._json(load_queue(vault))
            elif u.path == "/api/pdf":
                arxiv_id = q.get("id", [""])[0]
                if not ARXIV_ID_RE.fullmatch(arxiv_id or ""):
                    self._send(400, b"bad id", "text/plain; charset=utf-8")
                    return
                try:
                    p = get_pdf_path(vault, arxiv_id)
                    self._send(200, p.read_bytes(), "application/pdf")
                except Exception as e:
                    self._json({"ok": False, "msg": str(e)[:200]}, 502)
            elif u.path == "/api/highlights":
                arxiv_id = q.get("id", [""])[0]
                if not ARXIV_ID_RE.fullmatch(arxiv_id or ""):
                    self._send(400, b"bad id", "text/plain; charset=utf-8")
                    return
                self._json(load_highlights(vault, arxiv_id))
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            body = self._body_json()
            if u.path == "/api/config":
                self._json(save_config(vault, body))
            elif u.path == "/api/queue":
                kind = body.get("kind", "")
                value = (body.get("value") or "").strip()
                if kind not in ("link", "pdf") or not value:
                    self._json({"ok": False, "msg": "参数不对"}, 400)
                    return
                try:
                    item = add_queue_item(vault, kind, value)
                    self._json({"ok": True, "item": item})
                except Exception as e:
                    self._json({"ok": False, "msg": str(e)[:200]}, 400)
            elif u.path == "/api/queue/delete":
                with _queue_lock:
                    items = [i for i in load_queue(vault)
                             if i["id"] != body.get("id")]
                    save_queue(vault, items)
                self._json({"ok": True})
            elif u.path == "/api/queue/run":
                if _batch["running"]:
                    self._json({"ok": False, "msg": "正在处理中"})
                    return
                result = {}

                def _run():
                    result.update(process_queue(vault))

                t = threading.Thread(target=_run, daemon=True)
                t.start()
                t.join(timeout=900)
                if t.is_alive():
                    self._json({"ok": True, "msg": "还在处理中，队列状态会实时更新"})
                else:
                    self._json(result or {"ok": True})
            elif u.path == "/api/model/start":
                cfg = load_config(vault)
                ok = start_server(vault, cfg)
                self._json({"ok": ok,
                            "msg": "" if ok else "启动失败，检查程序与模型路径"})
            elif u.path == "/api/model/stop":
                stopped = stop_server(vault)
                self._json({"ok": True, "stopped": stopped})
            elif u.path == "/api/highlights":
                arxiv_id = q.get("id", [""])[0]
                if not ARXIV_ID_RE.fullmatch(arxiv_id or ""):
                    self._send(400, b"bad id", "text/plain; charset=utf-8")
                    return
                self._json(save_highlights(vault, arxiv_id, body))
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", UI_PORT), Handler)
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{UI_PORT}")).start()
    log(vault, f"UI serving at http://127.0.0.1:{UI_PORT}")
    server.serve_forever()


def _serve_ui_safe(vault: Path, open_browser: bool) -> None:
    try:
        serve_ui(vault, open_browser)
    except Exception as e:  # e.g. port already in use: daemon keeps working
        log(vault, f"UI failed to start: {e}")


def run_app(vault: Path, interval: int) -> int:
    """Packaged-app mode (v3): butler + scheduler + web UI in threads;
    in front, a small floating ball docked to the right edge of the
    screen; clicking it opens the main UI as a frameless side panel
    docked to the right edge (the Grammarly pose: icon rail on the
    panel's right side, ball staying visible at the extreme edge).
    The panel's rail carries collapse/quit buttons; right-clicking
    the ball quits. Falls back to headless if pywebview is missing."""
    _ensure_vault(vault)
    log(vault, f"START app mode vault={vault}")
    cfg = load_config(vault)
    global LLAMA_BASE
    LLAMA_BASE = f"http://127.0.0.1:{int(cfg['port'])}"
    threading.Thread(target=_serve_ui_safe, args=(vault, False),
                     daemon=True).start()
    threading.Thread(target=_poll_loop, args=(vault, interval),
                     kwargs={"once": False}, daemon=True).start()
    threading.Thread(target=_scheduler_loop, args=(vault,),
                     daemon=True).start()
    try:
        import webview
    except ImportError:
        print(f"pywebview not installed; running headless, "
              f"app at http://127.0.0.1:{UI_PORT}/app", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    screen_w, screen_h = 1920, 1080
    try:
        import ctypes
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        pass

    state = {"main_visible": False, "main": None, "ball": None}

    class AppApi:
        def toggle_main(self):
            win = state["main"]
            if win is None:
                return
            if state["main_visible"]:
                win.hide()
                state["main_visible"] = False
            else:
                win.show()
                state["main_visible"] = True

        def hide_main(self):
            win = state["main"]
            if win is not None and state["main_visible"]:
                win.hide()
                state["main_visible"] = False

        def quit_app(self):
            for key in ("main", "ball"):
                win = state.get(key)
                if win is not None:
                    try:
                        win.destroy()
                    except Exception:
                        pass

        def pick_pdf(self):
            try:
                dialog = getattr(webview, "FileDialog", None)
                open_const = dialog.OPEN if dialog is not None else webview.OPEN_DIALOG
                res = state["main"].create_file_dialog(
                    open_const, file_types=("PDF Files (*.pdf)",))
                if res:
                    return res[0]
            except Exception:
                pass
            return None

    api = AppApi()
    panel_w = 470
    panel_h = max(620, min(screen_h - 28, 920))
    main = webview.create_window(
        "LazyScholar · 懒人学者", f"http://127.0.0.1:{UI_PORT}/app",
        js_api=api, width=panel_w, height=panel_h,
        x=max(0, screen_w - panel_w - 84), y=max(0, (screen_h - panel_h) // 2),
        frameless=True, on_top=True, hidden=True)
    ball = webview.create_window(
        "", f"http://127.0.0.1:{UI_PORT}/ball",
        js_api=api, width=68, height=68,
        x=max(0, screen_w - 76), y=max(0, screen_h // 2 - 34),
        frameless=True, transparent=True, on_top=True)
    state["main"], state["ball"] = main, ball

    def _on_closing():
        # Closing the main window just tucks it away; the ball stays.
        if state["main_visible"]:
            main.hide()
            state["main_visible"] = False
            return False
        return True

    try:
        main.events.closing += _on_closing
    except Exception:
        pass
    webview.start()
    log(vault, "STOP app mode")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="LazyScholar unattended knowledge butler")
    ap.add_argument("--vault", default=str(Path.home() / "LazyScholarVault"),
                    help="Obsidian vault path (default: ~/LazyScholarVault)")
    ap.add_argument("--once", action="store_true", help="single poll cycle, then exit (for tests)")
    ap.add_argument("--interval", type=int, default=30, help="poll interval seconds (default 30)")
    ap.add_argument("--ui", action="store_true",
                    help="serve the web app only (no inbox polling)")
    ap.add_argument("--app", action="store_true",
                    help="packaged-app mode: butler + UI + native sidebar window")
    ap.add_argument("--no-ui", action="store_true", help="do not start the web app")
    ap.add_argument("--open-ui", action="store_true",
                    help="open the web app in a browser on start")
    args = ap.parse_args()
    vault = Path(args.vault).expanduser()
    if args.app:
        return run_app(vault, args.interval)
    if args.ui:
        _ensure_vault(vault)
        try:
            serve_ui(vault, args.open_ui)
        except KeyboardInterrupt:
            pass
        return 0
    return run(vault, args.once, args.interval,
               ui=not args.no_ui, open_ui=args.open_ui)


if __name__ == "__main__":
    sys.exit(main())
