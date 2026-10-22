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
import html as _html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paper_html
import paper_reader

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
ARXIV_API = "https://export.arxiv.org/api/query?id_list={}"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
PAPER_EXTS = {".pdf"}
TEXT_EXTS = {".txt", ".md"}

# Local llama.cpp server (llama serve). Override with LAZYSCHOLAR_LLAMA env.
LLAMA_BASE = os.environ.get("LAZYSCHOLAR_LLAMA", "http://127.0.0.1:8080")
if urllib.parse.urlsplit(LLAMA_BASE).scheme not in ("http", "https"):
    raise ValueError("LAZYSCHOLAR_LLAMA must use http:// or https://")
LLM_MODEL = os.environ.get("LAZYSCHOLAR_MODEL", "gemma-3-4b-it-qat-Q4_0")
LLM_TIMEOUT = 240          # a 4B model needs ~1 min for a full note; be patient
MAX_INPUT_CHARS = 9000     # v1 truncation for long discussion logs (TODO: chunk)


def log(vault: Path, msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    try:
        with open(vault / "lazyscholar.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        raise OSError(f"无法写入运行日志 {vault / 'lazyscholar.log'}: {e}") from e


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
                raw = resp.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("arXiv metadata response is too large")
            xml = raw.decode("utf-8-sig")
            if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
                raise ValueError("arXiv metadata must not contain DTDs or entities")
            root = ET.fromstring(xml)
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
    raise last_exc or RuntimeError('arxiv fetch failed')


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
        try:
            data = json.loads(resp.read())
        except ValueError as e:
            raise RuntimeError("本地模型服务返回了无效的 JSON") from e
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
    if not gen:
        raise ValueError("模型返回了空内容，请重试")
    body = (
        f"# {meta['title']}\n\n"
        f"- arXiv: {meta['url']}\n"
        f"- Authors: {', '.join(meta['authors'])}\n"
        f"- Filed by LazyScholar: {datetime.now().date().isoformat()}\n"
        f"- Note model: local Gemma 3 4B via llama.cpp\n"
        f"- Source scope: arXiv abstract only; not a full-text review\n\n"
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

CONFIG_DEFAULTS = {
    "llama_exe": "",
    "model_path": "",
    "port": 8080,
    "ctx": 16384,
    "schedule": "23:30",
    "theme": "light",
    "lang": "zh",
    "font_size": 18,
    "html_proxy": "",
    "serif": True,
    "ball_size": 42,          # logical px of the floating ball (32-90)
    "ball_y": None,
    "panel_y": None,
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
            stored = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise ValueError(f"无法读取配置 {f}，已保留原文件：{e}") from e
        if not isinstance(stored, dict):
            raise ValueError("config.json 必须是 JSON 对象，已保留原文件")
        cfg.update(stored)
    return cfg


_config_lock = threading.RLock()


def _write_json(path: Path, data) -> None:
    """Replace a complete JSON file; interrupted writes never truncate it."""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def save_config(vault: Path, cfg: dict) -> dict:
    with _config_lock:
        merged = load_config(vault)
        for k in CONFIG_DEFAULTS:
            if k in cfg:
                merged[k] = cfg[k]
        for key, low, high in (("port", 1, 65535), ("ctx", 512, 262144),
                               ("font_size", 13, 30)):
            try:
                value = int(merged[key])
            except (TypeError, ValueError) as e:
                raise ValueError(f"{key} 必须是整数") from e
            if not low <= value <= high:
                raise ValueError(f"{key} 必须在 {low}–{high} 之间")
            merged[key] = value
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(merged["schedule"])):
            raise ValueError("定时运行时间必须是 HH:MM")
        merged["html_proxy"] = paper_html.validate_proxy(str(merged.get("html_proxy") or ""))
        _write_json(vault / "config.json", merged)
        return merged


def _preferences_path() -> Path:
    override = os.environ.get("LAZYSCHOLAR_PREFERENCES")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config")))
    return base / "LazyScholar" / "preferences.json"


def _preferred_vault() -> str:
    path = _preferences_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ValueError(f"无法读取保存目录配置 {path}：{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"保存目录配置必须是 JSON 对象：{path}")
    value = data.get("vault", "")
    if not value or not Path(value).is_absolute():
        raise ValueError(f"保存目录配置无效：{path}")
    return value


def storage_state(vault: Path) -> dict:
    selected = _preferred_vault() or str(vault)
    return {"active": str(vault), "selected": selected,
            "restart_required": Path(selected).resolve() != vault.resolve(),
            "overridden": bool(os.environ.get("LAZYSCHOLAR_VAULT")
                               or "--vault" in sys.argv
                               or any(a.startswith("--vault=") for a in sys.argv))}


def select_vault(vault: Path, value: str) -> dict:
    target = Path(value).expanduser()
    if not value.strip() or not target.is_absolute() or not target.is_dir():
        raise ValueError("请选择已经存在的文件夹（绝对路径）")
    target = target.resolve()
    # Check actual write access, including Windows ACLs, without moving user data.
    probe = target / f".lazyscholar-write-{uuid.uuid4().hex}.tmp"
    try:
        with probe.open("x", encoding="utf-8") as f:
            f.write("")
    finally:
        if probe.exists():
            probe.unlink()
    prefs = _preferences_path()
    prefs.parent.mkdir(parents=True, exist_ok=True)
    _write_json(prefs, {"vault": str(target)})
    return storage_state(vault)


_queue_lock = threading.RLock()
_batch_lock = threading.Lock()
_model_lock = threading.RLock()
_shutting_down = threading.Event()


def load_queue(vault: Path) -> list:
    with _queue_lock:
        f = vault / "queue.json"
        if not f.exists():
            return []
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise ValueError(f"无法读取队列 {f}，已保留原文件：{e}") from e
        if not isinstance(items, list) or any(
                not isinstance(i, dict) or not {"id", "kind", "value", "status"} <= i.keys()
                for i in items):
            raise ValueError("queue.json 格式损坏，已保留原文件，请先备份并检查")
        return items


def save_queue(vault: Path, items: list) -> None:
    with _queue_lock:
        _write_json(vault / "queue.json", items)


def update_queue_item(vault: Path, item_id: str, **changes) -> None:
    with _queue_lock:
        items = load_queue(vault)
        for item in items:
            if item["id"] == item_id:
                item.update(changes)
                save_queue(vault, items)
                return
        raise ValueError("队列项目不存在")


def recover_queue(vault: Path) -> int:
    """Only call at startup, never while a batch is running."""
    with _queue_lock:
        items = load_queue(vault)
        count = 0
        for item in items:
            if item["status"] == "processing":
                item.update(status="pending", error="上次处理被中断，已恢复为待处理")
                count += 1
        if count:
            save_queue(vault, items)
        return count


def retry_queue_item(vault: Path, item_id: str) -> None:
    with _queue_lock:
        items = load_queue(vault)
        item = next((i for i in items if i["id"] == item_id), None)
        if item is None or item["status"] != "failed":
            raise ValueError("只能重试失败的项目")
        update_queue_item(vault, item_id, status="pending", error=None)


def delete_queue_item(vault: Path, item_id: str) -> None:
    with _queue_lock:
        items = load_queue(vault)
        item = next((i for i in items if i["id"] == item_id), None)
        if item is not None and item["status"] == "processing":
            raise ValueError("正在处理的项目暂时不能删除")
        save_queue(vault, [i for i in items if i["id"] != item_id])


def add_queue_item(vault: Path, kind: str, value: str) -> dict:
    if kind not in ("link", "pdf"):
        raise ValueError("不支持的材料类型")
    source = Path(value).name if kind == "pdf" else value
    match = ARXIV_ID_RE.search(source)
    if not match:
        raise ValueError("目前仅支持 arXiv 链接，或文件名含 arXiv 编号的 PDF；生成内容基于摘要")
    arxiv_id = match.group(1)
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
    with _queue_lock:
        items = load_queue(vault)
        for existing in items:
            existing_source = (existing.get("label", "") if existing["kind"] == "pdf"
                               else existing["value"])
            previous = ARXIV_ID_RE.search(existing_source)
            if previous and previous.group(1) == arxiv_id:
                raise ValueError("这篇论文已在队列中；失败项目请点击重试")
        if kind == "pdf":
            src = Path(value)
            if not src.is_file() or src.suffix.lower() != ".pdf":
                raise ValueError(f"找不到这个 PDF 文件：{value}")
            with src.open("rb") as f:
                if not f.read(5).startswith(b"%PDF"):
                    raise ValueError("所选文件不是有效的 PDF")
            import shutil
            qdir = vault / "queue_files"
            qdir.mkdir(parents=True, exist_ok=True)
            dest = qdir / f"{item['id']}.pdf"
            shutil.copyfile(src, dest)
            item["stored"] = dest.name
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


_server: dict[str, "subprocess.Popen[bytes] | None"] = {"proc": None}


def start_server(vault: Path, cfg: dict) -> bool:
    # Shutdown sets its gate BEFORE acquiring this same lock, so a launch
    # already in flight is either prevented or included in final cleanup.
    with _model_lock:
        if _shutting_down.is_set():
            return False
        if llama_health(int(cfg["port"])):
            return True
        exe = cfg.get("llama_exe") or "llama"
        model = cfg.get("model_path") or ""
        if not model:
            log(vault, "MODEL start requested but no model_path configured")
            return False
        proc = _server["proc"]
        if proc is None or proc.poll() is not None:
            try:
                with (vault / "model-server.log").open("ab") as model_log:
                    _server["proc"] = subprocess.Popen(
                        [exe, "serve", "-m", model, "-c", str(cfg.get("ctx", 16384)),
                         "--port", str(cfg["port"])],
                        stdout=model_log, stderr=subprocess.STDOUT)
            except Exception as e:
                log(vault, f"MODEL start failed: {e}")
                return False
    for _ in range(40):
        if _shutting_down.is_set():
            return False
        with _model_lock:
            proc = _server["proc"]
            if proc is None or proc.poll() is not None:
                log(vault, "MODEL exited before becoming healthy; see model-server.log")
                return False
        if llama_health(int(cfg["port"])):
            log(vault, f"MODEL server up on :{cfg['port']}")
            return True
        _shutting_down.wait(2)
    log(vault, "MODEL server did not become healthy in 80s")
    return False


def stop_server(vault: Path) -> bool:
    with _model_lock:
        proc = _server.get("proc")
        if proc is None:
            return False
        alive = proc.poll() is None
        if alive:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)
        _server["proc"] = None
        if alive:
            log(vault, "MODEL server stopped")
        return alive


def shutdown_model(vault: Path) -> bool:
    _shutting_down.set()
    return stop_server(vault)


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
            import shutil
            cache = vault / "papers_pdf"
            cache.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stored, cache / f"{arxiv_id}.pdf")
            dest = vault / "processed" / f"{item['id']}-{item.get('label', 'paper.pdf')}"
            stored.rename(dest)
    return note.name


def process_queue(vault: Path) -> dict:
    global LLAMA_BASE
    if not _batch_lock.acquire(blocking=False):
        return {"ok": False, "msg": "正在处理中"}
    _batch["running"] = True
    done = failed = 0
    started_here = False
    try:
        cfg = load_config(vault)
        LLAMA_BASE = f"http://127.0.0.1:{int(cfg['port'])}"
        pending_ids = [i["id"] for i in load_queue(vault) if i["status"] == "pending"]
        if not pending_ids:
            return {"ok": True, "processed": 0, "failed": 0}
        if not llama_health(int(cfg["port"])):
            # We own cleanup even when startup times out or fails part-way.
            started_here = True
            if not start_server(vault, cfg):
                raise RuntimeError("模型服务启动失败，请检查模型页配置和 model-server.log")
        for item_id in pending_ids:
            with _queue_lock:
                item = next((i for i in load_queue(vault) if i["id"] == item_id), None)
                if item is None or item["status"] != "pending":
                    continue
                update_queue_item(vault, item_id, status="processing", error=None)
            try:
                note = _process_queue_item(vault, item)
                update_queue_item(vault, item_id, note=note, status="done", error=None)
                done += 1
            except Exception as e:
                update_queue_item(vault, item_id, status="failed", error=str(e)[:300])
                failed += 1
        _batch["last_run"] = datetime.now().isoformat(timespec="seconds")
        log(vault, f"BATCH done: {done} ok, {failed} failed")
        return {"ok": True, "processed": done, "failed": failed}
    except Exception as e:
        log(vault, f"BATCH error: {e}")
        return {"ok": False, "msg": str(e)[:300]}
    finally:
        try:
            if started_here:
                stop_server(vault)
        finally:
            _batch["running"] = False
            _batch_lock.release()


def _scheduler_loop(vault: Path) -> None:
    while True:
        try:
            cfg = load_config(vault)
            now = datetime.now()
            if (now.strftime("%H:%M") == str(cfg.get("schedule", "23:30"))[:5]
                    and _batch["last_run"][:10] != now.date().isoformat()):
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
            stored = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise ValueError(f"无法读取划线文件 {f}，已保留原文件：{e}") from e
        if not isinstance(stored, dict) or not isinstance(stored.get("highlights"), list):
            raise ValueError("划线文件格式损坏，已保留原文件")
        return stored
    return {"highlights": []}


def save_highlights(vault: Path, arxiv_id: str, data: dict) -> dict:
    highlights = data.get("highlights", [])
    if not isinstance(highlights, list) or len(highlights) > 500:
        raise ValueError("划线数量必须在 0–500 之间")
    for h in highlights:
        if (not isinstance(h, dict) or not isinstance(h.get("page"), int)
                or h["page"] < 1 or any(
                    not isinstance(h.get(k), (int, float)) or not 0 <= h[k] <= 1
                    for k in ("x", "y", "w", "h"))):
            raise ValueError("划线坐标无效")
    d = vault / "highlights"
    d.mkdir(parents=True, exist_ok=True)
    clean = {"highlights": highlights}
    _write_json(d / f"{arxiv_id}.json", clean)
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
        cands.append(Path(getattr(sys, "_MEIPASS", "")) / "vendor")
    cands.append(Path(__file__).resolve().parent / "vendor")
    for c in cands:
        if c.exists():
            return c
    return cands[-1]


BALL_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;background:transparent;overflow:hidden}
#b{width:58px;height:58px;border-radius:50%;background:#33507E;color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 14px rgba(16,19,24,.35);border:2px solid #F8F6F0;margin:3px;user-select:none}
#b:active{transform:scale(.93)}
</style></head><body>
<div id="b" title="LazyScholar · 点我展开，右键退出"><svg width="30" height="30" viewBox="0 0 24 24" fill="none"><path d="M12 5c-1.8-1.5-4.2-2-7-2v14c2.8 0 5.2.5 7 2 1.8-1.5 4.2-2 7-2V3c-2.8 0-5.2.5-7 2z" fill="#fff"/><path d="M12 5v14" stroke="#33507E" stroke-width="1.4"/></svg></div>
<script>
document.getElementById('b').addEventListener('click',()=>{ if(window.pywebview&&window.pywebview.api) window.pywebview.api.toggle_main(); });
document.addEventListener('contextmenu',e=>{ e.preventDefault(); if(window.pywebview&&window.pywebview.api) window.pywebview.api.quit_app(); });
</script></body></html>
"""

APP_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>LazyScholar</title>
<style>
  :root{
    --blue:#33507E; --blue-strong:#2F5AA8; --sel:#E3ECFA;
    --ink:#33507E; --copper:#2F5AA8; --copper-bright:#33507E;
    --paper:#F8F9FC; --card:#FFFFFF; --line:#E4E8EF; --muted:#6B7686;
    --bg:#FFFFFF; --fg:#17233B; --titlebar:#F5F7FA; --pill:#FFFFFF;
  }
  body.dark{
    --bg:#12151C; --fg:#E8ECF4; --card:#1A1F2A; --line:#2A3140; --muted:#93A0B4;
    --titlebar:#171B24; --pill:#1A1F2A; --sel:#22355C; --paper:#12151C;
    --blue:#A9C3EE; --copper-bright:#A9C3EE;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  html,body{height:100%;}
  body{
    font-family:"Segoe UI","Microsoft YaHei",sans-serif;
    background:var(--bg); color:var(--fg); overflow:hidden;
    display:grid; grid-template-rows:46px 1fr; grid-template-columns:1fr 66px;
  }
  #titlebar{
    grid-column:1 / 3; grid-row:1; background:var(--titlebar);
    border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:8px; padding:0 8px 0 14px;
    user-select:none;
  }
  #titlebar .tb-drag{display:flex; align-items:center; gap:9px; flex:1; height:100%;}
  #titlebar img{width:22px; height:22px; border-radius:6px;}
  #titlebar .tb-name{font-size:13.5px; font-weight:600; letter-spacing:.2px;}
  #titlebar button{
    width:34px; height:30px; border:0; border-radius:8px; cursor:pointer;
    background:transparent; color:var(--muted); font-size:14px; font-family:inherit;
  }
  #titlebar button:hover{background:#E7EBF2; color:var(--fg);}
  body.dark #titlebar button:hover{background:#232A38;}
  #rail{
    grid-column:2; grid-row:2; background:transparent;
    display:flex; flex-direction:column; align-items:center;
    padding:10px 8px 12px 2px; gap:10px; user-select:none;
  }
  #pill{
    flex:1; width:50px; background:var(--pill); border:1px solid var(--line);
    border-radius:999px; box-shadow:0 4px 16px rgba(23,35,59,.10);
    display:flex; flex-direction:column; align-items:center;
    padding:8px 0; gap:4px;
  }
  #pill button{
    width:42px; height:42px; border:0; border-radius:14px; cursor:pointer;
    background:transparent; color:var(--blue); padding:0;
    display:flex; align-items:center; justify-content:center;
    font-family:inherit;
  }
  #pill button svg{width:21px; height:21px;}
  #pill button:hover{background:#EFF4FC;}
  body.dark #pill button:hover{background:#232C40;}
  #pill button.active{background:var(--sel); color:var(--blue-strong);}
  #pill .spacer{flex:1;}
  #avatar{
    width:34px; height:34px; border-radius:50%; background:var(--sel);
    display:flex; align-items:center; justify-content:center; flex:0 0 auto;
  }
  #avatar .dot{width:9px; height:9px; border-radius:50%; background:#C0392B;}
  #avatar .dot.on{background:#3FA46A;}
  .panel{
    grid-column:1; grid-row:2; height:100%; overflow-y:auto;
    padding:18px 18px 22px; scrollbar-width:thin;
  }
  .page{display:none;}
  .page.active{display:block;}
  h2{font-size:19px; font-weight:600; margin-bottom:6px; letter-spacing:.2px;}
  .sub{color:var(--muted); font-size:13px; line-height:1.55; margin-bottom:16px;}
  .card{
    background:var(--card); border:1px solid var(--line); border-radius:16px;
    padding:16px; margin-bottom:14px; box-shadow:0 1px 3px rgba(23,35,59,.05);
  }
  label{display:block; font-size:12.5px; color:var(--muted); margin:10px 0 4px;}
  label:first-child{margin-top:0;}
  input[type=text], input[type=number], input[type=time], select{
    width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:10px;
    background:var(--paper); color:var(--fg); font-size:14px; font-family:inherit;
  }
  .row{display:flex; flex-wrap:wrap; gap:9px; align-items:center;}
  .row > *{flex:0 0 auto;}
  .row .grow{flex:1; min-width:0;}
  button.btn{
    border:0; border-radius:11px; padding:10px 16px; cursor:pointer;
    font-size:14px; font-family:inherit; background:var(--ink); color:#F7FAFF;
  }
  body.dark button.btn{background:var(--copper); color:#FFFFFF;}
  button.btn.copper{background:var(--copper); color:#FFFFFF;}
  button.btn.ghost{background:transparent; color:var(--fg); border:1px solid var(--line);}
  button.btn:disabled{opacity:.45; cursor:default;}
  .qitem, .note{
    background:var(--card); border:1px solid var(--line); border-radius:14px;
    padding:12px 14px; margin-bottom:9px;
  }
  .qitem .ttl{font-weight:600; font-size:14px; word-break:break-all;}
  .qitem .meta, .note .meta{color:var(--muted); font-size:12px; margin-top:3px;}
  .qitem .del{float:right; border:0; background:none; color:var(--muted); cursor:pointer; font-size:12px; font-family:inherit;}
  .st{font-weight:600;}
  .st-pending{color:var(--muted);} .st-processing{color:var(--copper);}
  .st-done{color:#3FA46A;} .st-failed{color:#C0392B;}
  .note{cursor:pointer;}
  .note:hover{border-color:var(--copper-bright);}
  .note .ttl{font-size:15.5px; font-weight:600;}
  .badge{
    display:inline-block; font-size:11px; padding:1.5px 8px; border-radius:20px;
    background:var(--sel); color:var(--blue); margin-right:6px; vertical-align:2px;
  }
  body.dark .badge{background:var(--sel); color:var(--blue);}
  .empty{color:var(--muted); font-size:13.5px; padding:16px 2px; line-height:1.6;}
  .gempty{text-align:center; padding:34px 12px 26px; color:var(--muted);}
  .gempty svg{width:84px; height:84px; margin-bottom:12px;}
  .gempty .gt{color:var(--fg); font-size:16.5px; font-weight:600; margin-bottom:6px;}
  .gempty .gs{font-size:13px; line-height:1.6; max-width:250px; margin:0 auto 14px;}
  .status-line{font-size:14px; margin-bottom:10px;}
  .status-line b{color:var(--copper);}
  body.dark .status-line b{color:var(--copper-bright);}
  .flash{color:#3FA46A; font-size:13px; margin-left:8px;}
  /* reader overlay */
  #reader{
    position:fixed; inset:0; background:var(--bg); display:none;
    flex-direction:column; z-index:50;
  }
  #reader.open{display:flex;}
  #reader .bar{
    display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:10px 14px;
    border-bottom:1px solid var(--line);
  }
  #reader .bar .ttl{font-weight:600; font-size:16px; flex:1 0 100%; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  #reader .bar button{padding:7px 12px; font-size:13px;}
  #reader .bar button.on{background:var(--copper); color:#FFF7EA;}
  #pdf-wrap{flex:1; overflow-y:auto; padding:14px 10px; text-align:center;}
  #pdf-wrap canvas{margin:0 auto 12px; display:block; box-shadow:0 2px 14px rgba(16,19,24,.25); max-width:100%;}
  #pdf-wrap .pg{position:relative; margin:0 auto 12px; width:fit-content;}
  #pdf-wrap .pg canvas{margin:0;}
  #pdf-wrap .txtlayer{position:absolute; inset:0; overflow:hidden;}
  #pdf-wrap .txtlayer span{position:absolute; white-space:pre; transform-origin:0 0; color:transparent;}
  #pdf-wrap .hl{position:absolute; background:rgba(255,201,60,.45); border-radius:2px; pointer-events:none;}
  #note-body{flex:1; overflow-y:auto; padding:18px 22px; display:none;}
  #note-body .md{max-width:640px; margin:0 auto; line-height:1.75; font-size:15px; word-break:break-word; font-family:Georgia,"Songti SC","SimSun",serif;}
  #note-body .md p,#note-body .md ul{margin:10px 0;}
  #note-body .md h2{margin:22px 0 8px; font-size:18px;}
  #note-body .md ul{padding-left:22px;}
  .path{overflow-wrap:anywhere; margin-top:12px;}
  #app-message{position:fixed; bottom:12px; left:12px; right:12px; padding:12px; z-index:100; background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:10px; box-shadow:0 2px 12px #0002; font-size:13px; overflow-wrap:anywhere;}
  #app-message.error{border-color:#C0392B;}
  .txtlayer{line-height:1; text-align:initial; forced-color-adjust:none;}
  .txtlayer span{cursor:text;}
  #paper-wrap{flex:1; min-height:0; overflow:auto; padding:18px 22px 32px; line-height:1.85; font-family:Georgia,"Songti SC",serif; overflow-wrap:anywhere;}
  .paper-page{max-width:680px; margin:0 auto;}
  .paper-origin{font:12px "Segoe UI",sans-serif; color:var(--muted); border-top:1px solid var(--line); padding-top:14px; margin:20px 0;}
  .paper-text{margin:0 0 1em;}
  #paper-wrap h2{font-size:1.28em; line-height:1.5; margin:1.1em 0 .6em;}
  .paper-caption{font-size:.88em; color:var(--muted); margin:.6em 0 1.2em;}
  .paper-warning{font:12px/1.7 "Segoe UI",sans-serif; color:var(--muted); margin-bottom:18px;}
  #paper-wrap figure{margin:18px 0; background:white; border-radius:8px; padding:6px;}
  #paper-wrap figure img,.paper-aside img{display:block; max-width:100%; height:auto; margin:auto; cursor:zoom-in;}
  .paper-aside{font:12px/1.7 "Segoe UI",sans-serif; color:var(--muted); margin:12px 0;}
  .paper-aside img{max-height:260px; object-fit:contain;}
  #paper-wrap mark{background:#FFE596; color:#17233B; border-radius:2px;}
  #paper-more{margin:16px 0;}
  .source-html{line-height:1.85; overflow-wrap:anywhere;}
  .source-html p{margin:0 0 1em; overflow-x:auto; padding:.2em 0;}
  .source-html h1{font-size:1.45em; line-height:1.5; margin:1em 0;}
  .source-html h2,.source-html h3,.source-html h4,.source-html h5,.source-html h6{line-height:1.5; margin:1em 0 .6em;}
  .source-html .ltx_font_bold{font-weight:bold;}
  .source-html .ltx_font_italic{font-style:italic;}
  .source-html ul,.source-html ol{padding-left:1.4em;}
  .source-html a{color:var(--blue-strong);}
  .source-html math{font-family:"Cambria Math","STIX Two Math",math; font-size:1em; overflow:visible;}
  .source-html math[display="block"]{width:max-content; margin:.3em 0;}
  .source-html table{border-collapse:collapse; width:max-content;}
  .source-html td,.source-html th{padding:.2em .4em;}
  .source-html .ltx_eqn_eqno{white-space:nowrap; padding-left:.7em;}
  .source-html .ls-math-scroll{max-width:100%; overflow-x:auto; padding:.5em 0;}
  .source-html img{display:block; max-width:100%; min-height:80px; height:auto; margin:1em auto; cursor:zoom-in;}
  .source-html .ls-anchor{scroll-margin-top:12px;}
  #math-zoom,#legacy-dialog{position:fixed; inset:4vh 4vw; margin:auto; max-width:92vw; max-height:92vh; padding:12px; background:var(--card); color:var(--fg); border:1px solid var(--line); border-radius:12px;}
  #math-zoom::backdrop,#legacy-dialog::backdrop{background:#0008;}
  #math-zoom-body{max-width:84vw; max-height:76vh; overflow:auto; margin-top:12px; font-size:28px; padding:10px;}
  #legacy-items{max-height:70vh; overflow:auto; overflow-wrap:anywhere; padding:10px;}
  #legacy-items p{padding:10px 0; border-bottom:1px solid var(--line);}
  #paper-progress{font-size:12px; color:var(--muted);}
  #image-zoom{position:fixed; inset:4vh 4vw; margin:auto; max-width:92vw; max-height:92vh; padding:12px; background:var(--card); border:1px solid var(--line); border-radius:12px;}
  #image-zoom::backdrop{background:#0008;}
  #image-zoom > div{max-width:84vw; max-height:78vh; overflow:auto; margin-top:10px;}
  #image-zoom img{display:block; max-width:none; max-height:none; background:white;}
</style>
</head>
<body>
  <header id="titlebar">
    <div class="tb-drag pywebview-drag-region">
      <img src="/icon.png" alt="">
      <span class="tb-name">LazyScholar</span>
    </div>
    <button id="win-hide" data-i18n-title="hide" title="收起">✕</button>
  </header>
  <main class="panel">
    <section class="page active" id="page-queue">
      <h2 data-i18n="q_title">论文队列</h2>
      <p class="sub" data-i18n="q_sub">把论文先排队，不用等。每天 <b>23:30</b> 管家会自己把模型打开、批量处理完再关掉；等不及点「立即处理」。</p>
      <div class="card">
        <div class="row">
          <input type="text" id="q-input" class="grow" data-i18n-ph="q_ph" placeholder="粘贴 arXiv 链接，如 https://arxiv.org/abs/…">
          <button class="btn" id="btn-add" data-i18n="q_add">加入队列</button>
        </div>
        <div class="row" style="margin-top:10px;">
          <button class="btn ghost" id="btn-pick" data-i18n="q_pick">选择本地 PDF…</button>
          <button class="btn copper" id="btn-run" data-i18n="q_run">立即处理队列</button>
        </div>
      </div>
      <div id="qlist"></div>
    </section>

    <section class="page" id="page-notes">
      <h2 data-i18n="n_title">笔记卡片</h2>
      <p class="sub" data-i18n="n_sub">管家生成的精读笔记与概念卡都在这里，点开可读论文原文并划线。</p>
      <input type="text" id="n-search" data-i18n-ph="n_ph" placeholder="搜索标题或关键词…">
      <div id="nlist" style="margin-top:14px;"></div>
    </section>

    <section class="page" id="page-model">
      <h2 data-i18n="m_title">本地模型</h2>
      <p class="sub" data-i18n="m_sub">模型不用一直开着：在这里手动开关，或者让管家每天定时自己跑。</p>
      <div class="card">
        <div class="status-line"><span data-i18n="m_status">模型状态：</span><b id="m-state">…</b></div>
        <div class="row">
          <button class="btn copper" id="btn-mstart" data-i18n="m_start">启动模型</button>
          <button class="btn ghost" id="btn-mstop" data-i18n="m_stop">停止模型</button>
        </div>
        <div class="meta" style="color:var(--muted); font-size:12.5px; margin-top:12px;">
          <span data-i18n="m_addr">服务地址：</span><span id="m-addr">—</span>
        </div>
      </div>
      <div class="card">
        <label><span data-i18n="m_every">每天</span> <input type="time" id="f-schedule2" style="width:130px; display:inline-block;"> <span data-i18n="m_sched">自动运行队列（管家会自己开关模型）</span></label>
        <div class="row" style="margin-top:12px;">
          <button class="btn" id="btn-save-model" data-i18n="save">保存设置</button><span class="flash" id="flash2"></span>
        </div>
      </div>
    </section>

    <section class="page" id="page-settings">
      <h2 data-i18n="s_title">设置</h2>
      <div class="card">
        <label data-i18n="s_vault">论文与笔记保存目录</label>
        <input type="text" id="f-vault" readonly>
        <div class="row" style="margin-top:10px;">
          <button class="btn ghost" id="btn-vault" data-i18n="s_browse">选择目录…</button>
          <button class="btn ghost" id="btn-vault-open" data-i18n="s_open">打开当前目录</button>
        </div>
        <p class="sub path" id="storage-active"></p>
        <p class="sub" id="storage-notice"></p>
      </div>
      <div class="card">
        <label data-i18n="s_html_proxy">HTML／插图代理（可选，留空沿用系统）</label>
        <input type="text" id="f-html-proxy" placeholder="http://127.0.0.1:端口">
        <p class="sub" data-i18n="s_html_proxy_hint">只影响 HTML 和插图下载，不影响本地模型；TUN 是否能直连取决于你的网络。</p>
        <label data-i18n="s_exe">模型可执行文件 (llama)</label>
        <input type="text" id="f-exe">
        <label data-i18n="s_model">模型文件（GGUF）</label>
        <input type="text" id="f-model">
        <div class="row">
          <div class="grow">
            <label data-i18n="s_port">服务端口</label>
            <input type="number" id="f-port">
          </div>
          <div class="grow">
            <label data-i18n="s_ctx">上下文长度</label>
            <input type="number" id="f-ctx">
          </div>
        </div>
        <label data-i18n="s_lang">界面语言</label>
        <select id="f-lang">
          <option value="zh">中文</option>
          <option value="en">English</option>
        </select>
        <label data-i18n="s_theme">界面主题</label>
        <select id="f-theme">
          <option value="light" data-i18n="th_light">浅色</option>
          <option value="dark" data-i18n="th_dark">深色</option>
        </select>
        <div class="row">
          <div class="grow">
            <label><span data-i18n="s_font">阅读字号</span> (px)</label>
            <input type="number" id="f-font" min="13" max="30">
          </div>
          <div class="grow">
            <label data-i18n="s_sched">定时运行时间</label>
            <input type="time" id="f-schedule">
          </div>
        </div>
        <div class="row" style="margin-top:14px;">
          <button class="btn" id="btn-save" data-i18n="save">保存设置</button><span class="flash" id="flash"></span>
        </div>
      </div>
    </section>
  </main>

  <nav id="rail">
    <div id="pill">
      <button data-tab="queue" class="active" data-i18n-title="tab_queue" title="队列"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v10"/><path d="M7 10.5 12 15.5 17 10.5"/><path d="M4.5 19.5h15"/></svg></button>
      <button data-tab="notes" data-i18n-title="tab_notes" title="笔记"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="13" height="16.5" rx="2"/><path d="M9.8 9h5.7M9.8 12.5h5.7M9.8 16h3.7"/></svg></button>
      <button data-tab="model" data-i18n-title="tab_model" title="模型"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 7V4.5M14 7V4.5M10 19.5V17M14 19.5V17M7 10H4.5M7 14H4.5M19.5 10H17M19.5 14H17"/></svg></button>
      <button data-tab="settings" data-i18n-title="tab_settings" title="设置"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7h14M5 12h14M5 17h14"/><circle cx="9.5" cy="7" r="2.1" fill="var(--pill)"/><circle cx="15" cy="12" r="2.1" fill="var(--pill)"/><circle cx="8" cy="17" r="2.1" fill="var(--pill)"/></svg></button>
      <div class="spacer"></div>
      <button id="win-quit" data-i18n-title="quit" title="退出应用"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v7"/><path d="M6.3 6.5a8 8 0 1 0 11.4 0"/></svg></button>
    </div>
    <div id="avatar"><div class="dot" id="m-dot"></div></div>
  </nav>

  <div id="app-message" role="alert" hidden></div>
  <div id="reader">
    <div class="bar">
      <span class="ttl" id="r-title"></span>
      <button class="btn ghost" id="btn-reader-paper" data-i18n="r_paper">原文</button>
      <button class="btn ghost" id="btn-reader-note" data-i18n="r_note">导读</button>
      <button class="btn ghost" id="reader-font-down" title="减小字号">A−</button>
      <button class="btn ghost" id="reader-font-up" title="增大字号">A+</button>
      <span id="paper-progress"></span>
      <button class="btn ghost" id="btn-reader-pdf" data-i18n="r_pdf">原文 PDF</button>
      <button class="btn ghost" id="btn-hl" data-i18n="r_hl">划线模式</button>
      <button class="btn ghost" id="btn-hl-clear" data-i18n="r_hl_clear">擦除划线</button>
      <button class="btn" id="btn-reader-close" data-i18n="r_close">关闭</button>
    </div>
    <div id="paper-wrap" style="display:none;"><p class="paper-warning" data-i18n="paper_notice">优先读取并缓存 arXiv HTML，公式保留 MathML 结构；超长公式可横向滚动或点击放大。HTML 不可用时请重试或查看完整原版 PDF。</p><p id="paper-source-info" class="paper-warning" role="status"></p><button id="paper-legacy" class="btn ghost" hidden data-i18n="legacy_marks">查看未迁移的旧标注</button><div id="paper-pages"></div><button class="btn ghost" id="paper-more" data-i18n="paper_more">继续阅读下一页</button></div>
    <div id="pdf-wrap"></div>
    <div id="note-body"><div class="md" id="note-md"></div></div>
  </div>

<dialog id="math-zoom"><button class="btn" id="math-zoom-close" data-i18n="r_close">关闭</button><div id="math-zoom-body" class="source-html"></div></dialog>
<dialog id="legacy-dialog"><button class="btn" id="legacy-close" data-i18n="r_close">关闭</button><p class="sub" data-i18n="legacy_hint">以下旧标注无法唯一匹配到 HTML，原始数据仍保留。</p><div id="legacy-items"></div></dialog>
<dialog id="image-zoom"><button class="btn" id="zoom-close" data-i18n="r_close">关闭</button><div><img id="zoom-image" alt="原文图表与公式"></div></dialog>
<script src="/vendor/pdf.min.js"></script>
<script>
const $ = s => document.querySelector(s);
let LANG = 'zh';
let settingsLoaded = false, scheduleTime = '23:30', messageTimer;
async function api(url, options){
  const response = await fetch(url, options);
  const data = await response.json().catch(() => { throw Error('HTTP ' + response.status); });
  if (!response.ok || data.ok === false) throw Error(data.msg || ('HTTP ' + response.status));
  return data;
}
const post = (url,body={}) => api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
function showMessage(message, error=false){
  const box = $('#app-message');
  box.textContent = message; box.classList.toggle('error',error); box.hidden = false;
  clearTimeout(messageTimer); messageTimer = setTimeout(() => { box.hidden = true; }, error ? 15000 : 6000);
}
async function runAction(button, action){
  if (button) button.disabled = true;
  try { await action(); } catch(e) { showMessage(e.message,true); }
  finally { if (button) button.disabled = false; }
}

const STR = {
zh: {
  tab_queue:'队列', tab_notes:'笔记', tab_model:'模型', tab_settings:'设置',
  hide:'收起', quit:'退出应用',
  q_title:'论文队列',
  q_sub:'每天 <b>{time}</b> 自动处理，也可立即运行。目前支持 arXiv 链接或文件名含 arXiv 编号的 PDF；导读基于论文摘要，不是全文精读。',
  q_ph:'粘贴 arXiv 链接，如 https://arxiv.org/abs/…', q_add:'加入队列',
  q_pick:'选择本地 PDF…', q_run:'立即处理队列',
  q_empty:'队列是空的。把下一篇论文丢进来排队吧。',
  q_empty_t:'队列是空的', q_focus:'去粘贴链接', n_empty_t:'还没有笔记',
  q_src_link:'链接', q_src_file:'本地文件',
  st_pending:'排队中', st_processing:'处理中', st_done:'完成', st_failed:'失败', del:'删除',
  n_title:'笔记卡片', n_sub:'打开论文原文会自动重排正文与插图，可调整字号、划线，也可切换摘要导读。',
  n_ph:'搜索标题或关键词…', badge_paper:'论文笔记', badge_concept:'概念卡',
  untitled:'未命名讨论', n_empty:'还没有笔记。先去队列里丢一篇论文吧。',
  m_title:'本地模型', m_sub:'模型不用一直开着：在这里手动开关，或者让管家每天定时自己跑。',
  m_status:'模型状态：', m_start:'启动模型', m_stop:'停止模型', m_addr:'服务地址：',
  m_every:'每天', m_sched:'自动运行队列（管家会自己开关模型）',
  save:'保存设置', saved:'已保存 ✓',
  m_running:'运行中', m_stopped:'未运行', m_checking:'检测中…',
  s_title:'设置', s_exe:'模型可执行文件 (llama)', s_model:'模型文件（GGUF）',
  s_port:'服务端口', s_ctx:'上下文长度', s_lang:'界面语言', s_theme:'界面主题',
  th_light:'浅色', th_dark:'深色', s_font:'阅读字号', s_sched:'定时运行时间',
  r_hl:'划线模式', r_hl_exit:'退出划线', r_hl_clear:'擦除划线', r_erase_exit:'退出擦除', r_close:'关闭',
  erase_hint:'拖选要擦除的黄色文字，只取消选中部分；其他划线和未划线文字保持不变。',
  hl_hint:'用鼠标拖选正文文字，松开后自动保存黄色标记；图表图片暂不支持文字划线。',
  pdf_loading:'正在加载原 PDF…', pdf_none:'PDF 加载失败，仍可阅读笔记。',
  pdf_unavailable:'PDF 阅读组件未加载', retry:'重试', batch_result:'已完成 {done} 篇，失败 {failed} 篇',
  r_note:'导读', r_pdf:'原版 PDF', r_paper:'原文',
  paper_loading:'正在加载 HTML 原文…', paper_more:'继续阅读下一部分', paper_page:'原文第 {n} / {total} 页',
  paper_notice:'优先读取并缓存 arXiv HTML，公式保留 MathML 结构；超长公式可横向滚动或点击放大。HTML 不可用时请重试或查看完整原版 PDF。',
  paper_part:'HTML 原文第 {n} / {total} 部分', source_html:'来源：arXiv HTML（本地缓存）', math_nodes:'数学节点',
  paper_retry:'重新获取 HTML', html_fallback:'可使用上方“原版 PDF”完整对照，未使用有损公式切图。',
  s_html_proxy:'HTML／插图代理（可选，留空沿用系统）', s_html_proxy_hint:'只影响 HTML 和插图下载，不影响本地模型；TUN 是否能直连取决于你的网络。',
  legacy_marks:'查看未迁移的旧标注', legacy_hint:'以下旧标注无法唯一匹配到 HTML，原始数据仍保留。',
  paper_scanned:'此页没有可提取的正文，暂保留原页图像，不会伪造 OCR 文字。',
  paper_changed:'论文文件已更新，请重新打开原文。', paper_aside:'原文侧注／旋转文字（点击展开）', paper_region:'原文图表或公式', paper_zoom:'点击放大图表或公式', s_vault:'论文与笔记保存目录', s_browse:'选择目录…', s_open:'打开当前目录',
  s_active:'当前使用：', s_storage_hint:'更换目录不会搬动旧论文、笔记和标注。',
  s_restart:'新目录将在退出并重新启动后生效。', s_override:'当前启动指定了 --vault 或环境变量；需取消指定才能使用所选目录。'
},
en: {
  tab_queue:'Queue', tab_notes:'Notes', tab_model:'Model', tab_settings:'Settings',
  hide:'Collapse', quit:'Quit LazyScholar',
  q_title:'Paper Queue',
  q_sub:'Runs daily at <b>{time}</b>, or process now. Supports arXiv links and PDFs named with an arXiv ID. Notes are based on the abstract, not the full paper.',
  q_ph:'Paste an arXiv link, e.g. https://arxiv.org/abs/…', q_add:'Add to queue',
  q_pick:'Choose a local PDF…', q_run:'Process queue now',
  q_empty:'The queue is empty. Drop in the next paper.',
  q_empty_t:'The queue is empty', q_focus:'Paste a link', n_empty_t:'No notes yet',
  q_src_link:'Link', q_src_file:'Local file',
  st_pending:'Queued', st_processing:'Processing', st_done:'Done', st_failed:'Failed', del:'Delete',
  n_title:'Note Cards', n_sub:'Read locally reflowed original text and figures, adjust font size and highlight, or switch to the abstract-based note.',
  n_ph:'Search titles or keywords…', badge_paper:'Study note', badge_concept:'Concept card',
  untitled:'Untitled discussion', n_empty:'No notes yet. Queue a paper first.',
  m_title:'Local Model', m_sub:'The model does not need to stay on: start or stop it here, or let the butler run it on a daily schedule.',
  m_status:'Model status:', m_start:'Start model', m_stop:'Stop model', m_addr:'Service:',
  m_every:'Every day at', m_sched:'auto-process the queue (the butler starts and stops the model itself)',
  save:'Save settings', saved:'Saved ✓',
  m_running:'Running', m_stopped:'Stopped', m_checking:'Checking…',
  s_title:'Settings', s_exe:'Model executable (llama)', s_model:'Model file (GGUF)',
  s_port:'Service port', s_ctx:'Context length', s_lang:'Language', s_theme:'Theme',
  th_light:'Light', th_dark:'Dark', s_font:'Reading font size', s_sched:'Daily run time',
  r_hl:'Highlight', r_hl_exit:'Exit highlight', r_hl_clear:'Erase highlights', r_erase_exit:'Exit eraser', r_close:'Close',
  erase_hint:'Select highlighted text to erase only the selected portion. Other highlights and unmarked text stay unchanged.',
  hl_hint:'Drag to select text, then release to save a yellow highlight. Text inside figure images is not selectable.',
  pdf_loading:'Loading the original PDF…', pdf_none:'PDF could not load. Your note is still available.',
  pdf_unavailable:'PDF reader is unavailable', retry:'Retry', batch_result:'Completed {done}; failed {failed}',
  r_note:'Note', r_pdf:'PDF source', r_paper:'Article',
  paper_loading:'Loading HTML article…', paper_more:'Read the next part', paper_page:'Source page {n} / {total}',
  paper_notice:'Cached arXiv HTML with native MathML. Wide equations scroll horizontally and can be enlarged. If HTML is unavailable, retry or open the complete PDF source.',
  paper_part:'HTML part {n} / {total}', source_html:'Source: cached arXiv HTML', math_nodes:'math nodes',
  paper_retry:'Fetch HTML again', html_fallback:'Use PDF source above for a complete comparison; no lossy formula crops were substituted.',
  s_html_proxy:'HTML/image proxy (optional; blank uses system settings)', s_html_proxy_hint:'Only used for HTML/images, never the local model. Direct access depends on your network.',
  legacy_marks:'View unmigrated highlights', legacy_hint:'These old highlights could not be matched uniquely. Their original data is retained.',
  paper_scanned:'No extractable text on this page. The original page image is shown without invented OCR.',
  paper_changed:'The paper file changed. Please reopen it.', paper_aside:'Original side note / rotated text (expand)', paper_region:'Original figure, table or equation', paper_zoom:'Click to zoom', s_vault:'Paper and note folder', s_browse:'Choose folder…', s_open:'Open current folder',
  s_active:'Currently using: ', s_storage_hint:'Changing folders does not move existing papers, notes or highlights.',
  s_restart:'The new folder takes effect after quitting and restarting.', s_override:'This launch uses --vault or an environment override; remove it to use the selected folder.'
}};
const t = k => (STR[LANG] && STR[LANG][k]) || STR.zh[k] || k;

function applyLang(l){
  LANG = (l === 'en') ? 'en' : 'zh';
  document.documentElement.lang = LANG === 'en' ? 'en' : 'zh-CN';
  document.querySelectorAll('[data-i18n]').forEach(el => { el.innerHTML = t(el.dataset.i18n).replace('{time}', scheduleTime); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  $('#btn-hl').textContent = hlMode ? t('r_hl_exit') : t('r_hl');
  $('#btn-hl-clear').textContent = eraseMode ? t('r_erase_exit') : t('r_hl_clear');
  refreshQueue(); renderNotes(window._notes || []); refreshModel();
}

/* ---------- tabs ---------- */
document.querySelectorAll('#rail button[data-tab]').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('#rail button[data-tab]').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    $('#page-' + b.dataset.tab).classList.add('active');
    if (b.dataset.tab === 'notes') refreshNotes();
    if (b.dataset.tab === 'queue') refreshQueue();
    try { window.pywebview.api.set_panel_tab(b.dataset.tab); } catch(e) {}
  });
});
$('#win-hide').addEventListener('click', () => { try { window.pywebview.api.hide_main(); } catch(e) {} });
$('#win-quit').addEventListener('click', () => { try { window.pywebview.api.quit_app(); } catch(e) {} });

/* ---------- empty states (Grammarly-style: line art, title, sub) ---------- */
const BOOK_SVG = `<svg viewBox="0 0 96 96" fill="none" stroke="#33507E" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M48 30c-6-4.5-14-6.5-24-6v46c10-.5 18 1.5 24 6 6-4.5 14-6.5 24-6V24c-10-.5-18 1.5-24 6z"/><path d="M48 30v46"/><path d="M76 13l1.8 4.7 4.7 1.8-4.7 1.8L76 26l-1.8-4.7L69.5 19.5l4.7-1.8z" fill="#FFC93C" stroke="none"/><path d="M19 11l1.2 3.2 3.2 1.2-3.2 1.3L19 20l-1.2-3.3-3.2-1.3 3.2-1.2z" fill="#FFC93C" stroke="none"/></svg>`;
function emptyState(title, sub, act){
  return `<div class="gempty">${BOOK_SVG}<div class="gt">${title}</div><div class="gs">${sub}</div>${act === 'q' ? `<button class="btn" onclick="document.getElementById('q-input').focus()">${t('q_focus')}</button>` : ''}</div>`;
}

/* ---------- queue ---------- */
async function refreshQueue(){
  try {
    const items = await api('/api/queue');
    const box = $('#qlist');
    if (!items.length) { box.innerHTML = emptyState(t('q_empty_t'), t('q_empty'), 'q'); return; }
    box.innerHTML = items.map(it => `
      <div class="qitem">
        <button class="del" data-del="${it.id}" ${it.status === 'processing' ? 'disabled' : ''}>${t('del')}</button>
        <div class="ttl">${escapeHtml(it.label || it.value || '')}</div>
        <div class="meta">${it.kind === 'pdf' ? t('q_src_file') : t('q_src_link')} · <span class="st st-${it.status}">${t('st_' + it.status)}</span>${it.error ? ' · ' + escapeHtml(it.error) : ''}</div>
        ${it.status === 'failed' ? `<button class="btn ghost" data-retry="${it.id}">${t('retry')}</button>` : ''}
      </div>`).join('');
    for (const [attribute, route] of [['del', 'delete'], ['retry', 'retry']]) {
      box.querySelectorAll('[data-' + attribute + ']').forEach(b => b.addEventListener('click', () => runAction(b, async () => {
        await post('/api/queue/' + route, {id:b.dataset[attribute]});
        await refreshQueue();
      })));
    }
  } catch(e) { showMessage(e.message, true); }
}
$('#btn-add').addEventListener('click', () => runAction($('#btn-add'), async () => {
  const value = $('#q-input').value.trim(); if (!value) return;
  await post('/api/queue', {kind:'link', value});
  $('#q-input').value = ''; await refreshQueue();
}));
$('#q-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-add').click(); });
$('#btn-pick').addEventListener('click', () => runAction($('#btn-pick'), async () => {
  const path = await window.pywebview.api.pick_pdf();
  if (path) { await post('/api/queue', {kind:'pdf', value:path}); await refreshQueue(); }
}));
$('#btn-run').addEventListener('click', () => runAction($('#btn-run'), async () => {
  const result = await post('/api/queue/run');
  await refreshQueue(); await refreshNotes(); await refreshModel();
  showMessage(result.msg || t('batch_result').replace('{done}', result.processed || 0).replace('{failed}', result.failed || 0), !!result.failed);
}));

/* ---------- notes + reader ---------- */
let notesCache = [];
async function refreshNotes(){
  try {
    const d = await api('/api/notes');
    notesCache = [...(d.papers || []).map(n => ({...n, kind:'papers'})),
                  ...(d.concepts || []).map(n => ({...n, kind:'concepts'}))]
      .sort((a,b) => b.mtime - a.mtime);
    renderNotes(notesCache);
  } catch(e) { showMessage(e.message, true); }
}
function renderNotes(notes){
  window._notes = notes;
  const q = ($('#n-search').value || '').toLowerCase();
  const list = notes.filter(n => !q || n.title.toLowerCase().includes(q));
  $('#nlist').innerHTML = list.length ? list.map((n,index) => `
    <div class="note" data-open="${index}">
      <div class="ttl"><span class="badge">${n.kind === 'papers' ? t('badge_paper') : t('badge_concept')}</span>${escapeHtml(n.title)}</div>
      <div class="meta">${new Date(n.mtime * 1000).toLocaleDateString(LANG === 'en' ? 'en-US' : 'zh-CN')}</div>
    </div>`).join('') : emptyState(t('n_empty_t'), t('n_empty'), '');
  document.querySelectorAll('[data-open]').forEach(el => el.addEventListener('click', () =>
    runAction(null, () => openNote(list[Number(el.dataset.open)]))));
}
$('#n-search').addEventListener('input', () => renderNotes(notesCache));

let curNote = null, hlMode = false, eraseMode = false, curArxiv = null, readerVersion = 0, pdfLoaded = false;
let readerView = 'note', pdfVersion = 0;
let highlightWrite = Promise.resolve();
function showNote(){
  readerView = 'note';
  $('#paper-wrap').style.display = 'none';
  if (!pdfLoaded) { ++pdfVersion; $('#pdf-wrap').innerHTML = ''; }
  $('#btn-reader-pdf').disabled = !curArxiv;
  setHighlightMode(null);
  $('#pdf-wrap').style.display = 'none'; $('#note-body').style.display = 'block';
  $('#btn-hl').disabled = true; $('#btn-hl-clear').disabled = true;
}
async function openNote(note){
  const version = ++readerVersion;
  const d = await api('/api/note?kind=' + note.kind + '&name=' + encodeURIComponent(note.name));
  if (version !== readerVersion) return;
  curNote = note; curArxiv = d.arxiv || null; pdfLoaded = false;
  $('#r-title').textContent = d.title || note.title;
  $('#note-md').innerHTML = d.html || '';
  $('#note-md').style.fontSize = (window._fontSize || 18) + 'px';
  $('#btn-reader-pdf').disabled = !curArxiv;
  $('#pdf-wrap').innerHTML = '';
  $('#reader').classList.add('open');
  showNote();
  $('#btn-reader-paper').disabled = !curArxiv;
  if (curArxiv) await showPaper();
}
$('#btn-reader-note').addEventListener('click', showNote);
$('#btn-reader-pdf').addEventListener('click', () => runAction(null, async () => {
  if (!curArxiv) return;
  readerView = 'pdf'; setHighlightMode(null);
  $('#btn-hl').disabled = true; $('#btn-hl-clear').disabled = true;
  $('#paper-wrap').style.display = 'none';
  $('#btn-reader-pdf').disabled = true;
  const version = readerVersion, render = pdfLoaded ? pdfVersion : ++pdfVersion;
  $('#note-body').style.display = 'none'; $('#pdf-wrap').style.display = 'block';
  if (!pdfLoaded) await loadPdf(curArxiv, version, render);
  if (version === readerVersion && render === pdfVersion && readerView === 'pdf') {
    $('#btn-reader-pdf').disabled = false;
    $('#btn-hl').disabled = !pdfLoaded; $('#btn-hl-clear').disabled = !pdfLoaded;
  }
}));
$('#btn-reader-close').addEventListener('click', () => {
  ++readerVersion; curArxiv = null; curNote = null; paperState = null; setHighlightMode(null);
  $('#paper-pages').replaceChildren();
  $('#reader').classList.remove('open'); $('#pdf-wrap').innerHTML = '';
});
function setHighlightMode(mode){
  hlMode = mode === 'highlight'; eraseMode = mode === 'erase';
  window.getSelection()?.removeAllRanges();
  $('#btn-hl').classList.toggle('on', hlMode);
  $('#btn-hl-clear').classList.toggle('on', eraseMode);
  $('#btn-hl').textContent = hlMode ? t('r_hl_exit') : t('r_hl');
  $('#btn-hl-clear').textContent = eraseMode ? t('r_erase_exit') : t('r_hl_clear');
  $('#btn-hl').setAttribute('aria-pressed', String(hlMode));
  $('#btn-hl-clear').setAttribute('aria-pressed', String(eraseMode));
}
$('#btn-hl').addEventListener('click', () => {
  setHighlightMode(hlMode ? null : 'highlight');
  if (hlMode) showMessage(t('hl_hint'));
});
$('#btn-hl-clear').addEventListener('click', () => {
  setHighlightMode(eraseMode ? null : 'erase');
  if (eraseMode) showMessage(t('erase_hint'));
});
function sameHighlightList(before, after){
  return before.length === after.length && before.every((row, i) => row === after[i]);
}
function eraseTextHighlights(highlights, cuts){
  return highlights.flatMap(mark => {
    let spans = [[mark.start, mark.end]];
    for (const cut of cuts) {
      if (cut.block !== mark.block) continue;
      spans = spans.flatMap(([start, end]) => {
        if (cut.end <= start || cut.start >= end) return [[start, end]];
        const rest = [];
        if (start < cut.start) rest.push([start, cut.start]);
        if (cut.end < end) rest.push([cut.end, end]);
        return rest;
      });
    }
    if (spans.length === 1 && spans[0][0] === mark.start && spans[0][1] === mark.end) return [mark];
    return spans.map(([start,end]) => ({...mark,start,end,quote:mark.quote.slice(start-mark.start,end-mark.start)}));
  });
}
function erasePdfHighlights(highlights, cuts){
  return highlights.flatMap(mark => {
    let pieces = [mark];
    for (const cut of cuts) {
      if (cut.page !== mark.page) continue;
      pieces = pieces.flatMap(part => {
        const left = Math.max(part.x,cut.x), right = Math.min(part.x+part.w,cut.x+cut.w);
        const top = Math.max(part.y,cut.y), bottom = Math.min(part.y+part.h,cut.y+cut.h);
        if (left >= right || top >= bottom) return [part];
        const rest = [];
        if (part.y < top) rest.push({...part,h:top-part.y});
        if (bottom < part.y+part.h) rest.push({...part,y:bottom,h:part.y+part.h-bottom});
        if (part.x < left) rest.push({...part,y:top,w:left-part.x,h:bottom-top});
        if (right < part.x+part.w) rest.push({...part,x:right,y:top,w:part.x+part.w-right,h:bottom-top});
        return rest;
      });
    }
    return pieces;
  });
}

async function loadPdf(arxiv, version, render){
  const isCurrent = () => version === readerVersion && render === pdfVersion && readerView === 'pdf';
  const wrap = $('#pdf-wrap');
  wrap.innerHTML = `<div class="empty">${t('pdf_loading')}</div>`;
  try {
    const lib = window.pdfjsLib;
    if (!lib) throw Error(t('pdf_unavailable'));
    lib.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.js';
    const pdf = await lib.getDocument('/api/pdf?id=' + encodeURIComponent(arxiv)).promise;
    if (!isCurrent()) return;
    wrap.innerHTML = '';
    await highlightWrite.catch(() => {});
    if (!isCurrent()) return;
    const hls = await fetchHighlights(arxiv);
    for (let p = 1; p <= pdf.numPages; p++) {
      const page = await pdf.getPage(p);
      if (!isCurrent()) return;
      const base = page.getViewport({scale:1});
      const vp = page.getViewport({scale:Math.max(0.1, (wrap.clientWidth - 24) / base.width)});
      const div = document.createElement('div'); div.className = 'pg';
      div.style.width = vp.width + 'px'; div.style.height = vp.height + 'px';
      div.dataset.page = p;
      const canvas = document.createElement('canvas');
      canvas.width = vp.width; canvas.height = vp.height;
      div.appendChild(canvas);
      const txt = document.createElement('div'); txt.className = 'txtlayer';
      txt.style.setProperty('--scale-factor', vp.scale);
      div.appendChild(txt); wrap.appendChild(div);
      await page.render({canvasContext:canvas.getContext('2d'), viewport:vp}).promise;
      const tc = await page.getTextContent();
      if (!isCurrent()) return;
      await lib.renderTextLayer({textContentSource:tc, container:txt, viewport:vp}).promise;
      if (!isCurrent()) return;
      hls.filter(h => h.page === p).forEach(h => drawHl(div,h));
    }
    if (isCurrent()) pdfLoaded = true;
  } catch(e) {
    if (isCurrent()) { showNote(); showMessage(t('pdf_none') + ' ' + e.message, true); }
  }
}
function drawHl(pgDiv, h){
  const el = document.createElement('div'); el.className = 'hl';
  el.style.left = (h.x * 100) + '%'; el.style.top = (h.y * 100) + '%';
  el.style.width = (h.w * 100) + '%'; el.style.height = (h.h * 100) + '%';
  pgDiv.appendChild(el);
}
async function fetchHighlights(arxiv){
  const d = await api('/api/highlights?id=' + encodeURIComponent(arxiv));
  return d.highlights || [];
}
async function saveHighlights(arxiv, list){
  await post('/api/highlights?id=' + encodeURIComponent(arxiv), {highlights:list});
}
document.addEventListener('mouseup', () => {
  if ((!hlMode && !eraseMode) || !curArxiv || readerView !== 'pdf' || !pdfLoaded) return;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  const anchor = range.commonAncestorContainer;
  const pgDiv = (anchor.nodeType === Node.ELEMENT_NODE ? anchor : anchor.parentElement).closest('.pg');
  if (!pgDiv || !pgDiv.contains(range.endContainer) || !pgDiv.contains(range.startContainer)) return;
  const pr = pgDiv.getBoundingClientRect(), out = [];
  for (const r of range.getClientRects()) {
    if (r.width < 2 || r.height < 2) continue;
    out.push({page:Number(pgDiv.dataset.page), x:Math.max(0,(r.left-pr.left)/pr.width),
      y:Math.max(0,(r.top-pr.top)/pr.height), w:Math.min(1,r.width/pr.width), h:Math.min(1,r.height/pr.height)});
  }
  if (!out.length) return;
  const id = curArxiv, version = readerVersion, erasing = eraseMode;
  sel.removeAllRanges();
  const operation = highlightWrite.catch(() => {}).then(async () => {
    const current = await fetchHighlights(id);
    const merged = erasing ? erasePdfHighlights(current,out) : current.concat(out);
    if (!erasing || !sameHighlightList(current,merged)) await saveHighlights(id, merged);
    if (version === readerVersion) {
      document.querySelectorAll('#pdf-wrap .hl').forEach(el => el.remove());
      for (const mark of merged) {
        const page = document.querySelector('#pdf-wrap .pg[data-page="' + mark.page + '"]');
        if (page) drawHl(page,mark);
      }
    }
  });
  highlightWrite = operation;
  operation.catch(e => showMessage(e.message,true));
});

/* ---------- reflowed original text + figures ---------- */
let paperState = null;
function refreshReadingMarks(){
  const state = paperState; if (!state) return;
  $('#paper-pages').querySelectorAll('[data-block]').forEach(el => {
    const text = state.texts.get(el.dataset.block);
    const ranges = state.highlights.filter(h => h.block === el.dataset.block).sort((a,b) => a.start-b.start);
    const merged = [];
    for (const h of ranges) {
      const last = merged[merged.length-1];
      if (last && h.start <= last.end) last.end = Math.max(last.end,h.end);
      else merged.push({start:h.start,end:h.end});
    }
    el.replaceChildren(); let offset = 0;
    for (const h of merged) {
      el.appendChild(document.createTextNode(text.slice(offset,h.start)));
      const mark = document.createElement('mark'); mark.textContent = text.slice(h.start,h.end);
      el.appendChild(mark); offset = h.end;
    }
    el.appendChild(document.createTextNode(text.slice(offset)));
  });
}
async function showPaper(){
  if (!curArxiv) return;
  readerView = 'paper'; ++pdfVersion;
  $('#btn-reader-pdf').disabled = !curArxiv;
  if (!pdfLoaded) $('#pdf-wrap').innerHTML = '';
  setHighlightMode(null);
  $('#btn-hl').disabled = true; $('#btn-hl-clear').disabled = true;
  $('#note-body').style.display = 'none'; $('#pdf-wrap').style.display = 'none';
  $('#paper-wrap').style.display = 'block';
  $('#paper-wrap').style.fontSize = (window._fontSize || 18) + 'px';
  if (!paperState || paperState.id !== curArxiv || paperState.version !== readerVersion) {
    paperState = {id:curArxiv, version:readerVersion, next:1, total:1, loading:false, source:null,
                  highlights:[], texts:new Map()};
    $('#paper-pages').replaceChildren();
  }
  if (paperState.next === 1) await loadNextPaper();
  if (readerView === 'paper' && paperState && paperState.source && !paperState.annotationError) {
    $('#btn-hl').disabled = false; $('#btn-hl-clear').disabled = false;
  }
}
function loadNextPaper(){
  const state = paperState;
  if (!state || state.next > state.total) return Promise.resolve(false);
  if (state.loading) return state.loading;
  state.loading = fetchPaperPart(state).finally(() => { state.loading = null; });
  return state.loading;
}
async function fetchPaperPart(state){
  const more = $('#paper-more'); more.disabled = true; more.textContent = t('paper_loading');
  try {
    const d = await api('/api/paper?id=' + encodeURIComponent(state.id) + '&page=' + state.next + (state.failed && state.next === 1 ? '&refresh=1' : ''));
    if (paperState !== state || state.version !== readerVersion) return false;
    state.failed = false;
    if (!state.source) {
      try {
        await highlightWrite.catch(() => {});
        if (paperState !== state || state.version !== readerVersion) return false;
        const marks = await api('/api/reading-highlights?id=' + encodeURIComponent(state.id));
        if (paperState !== state || state.version !== readerVersion) return false;
        if (marks.source !== d.source) throw Error(t('paper_changed'));
        state.highlights = marks.highlights || []; state.legacy = marks.legacy_unresolved || [];
      } catch(e) {
        if (paperState !== state || state.version !== readerVersion) return false;
        state.annotationError = true; showMessage(e.message,true);
      }
      if (paperState !== state || state.version !== readerVersion) return false;
      state.source = d.source;
    }
    if (state.source !== d.source) throw Error(t('paper_changed'));
    const section = document.createElement('section'); section.className = 'paper-page';
    const origin = document.createElement('div'); origin.className = 'paper-origin';
    origin.textContent = t('paper_part').replace('{n}',d.page).replace('{total}',d.pages);
    section.appendChild(origin);
    for (const block of d.blocks) {
      if (block.kind === 'html') {
        const rich = document.createElement('div');
        rich.innerHTML = block.html; // Server allowlist sanitizer; no paper scripts/styles.
        for (const run of block.runs) state.texts.set(run.id,run.text);
        section.appendChild(rich);
      }
    }
    $('#paper-pages').appendChild(section);
    state.next = d.page + 1; state.total = d.pages;
    $('#paper-progress').textContent = 'HTML · ' + d.page + ' / ' + d.pages;
    $('#paper-source-info').textContent = t('source_html') + ' · ' + d.math_count + ' ' + t('math_nodes');
    $('#paper-legacy').hidden = !(state.legacy && state.legacy.length);
    refreshReadingMarks();
    if (readerView === 'paper') {
      $('#btn-hl').disabled = !!state.annotationError; $('#btn-hl-clear').disabled = !!state.annotationError;
    }
    return true;
  } catch(e) {
    if (paperState === state && state.version === readerVersion) {
      state.failed = true;
      $('#paper-source-info').textContent = e.message + ' ' + t('html_fallback');
      showMessage(e.message,true);
    }
    return false;
  } finally {
    if (paperState === state && state.version === readerVersion) {
      more.disabled = false; more.hidden = state.next > state.total;
      more.textContent = state.failed ? t('paper_retry') : t('paper_more');
    }
  }
}
$('#paper-pages').addEventListener('click', e => runAction(null,async () => {
  const state = paperState; if (!state) return;
  const anchor = e.target.closest('a[href^="#"]');
  if (anchor) {
    e.preventDefault();
    if (hlMode || eraseMode) return;
    const name = anchor.getAttribute('href').slice(1);
    let target = document.getElementById(name);
    if (!target) {
      const result = await api('/api/paper/anchor?id=' + encodeURIComponent(state.id) + '&name=' + encodeURIComponent(name));
      while (paperState === state && state.next <= result.page) {
        if (!await loadNextPaper()) return;
      }
      target = document.getElementById(name);
    }
    if (paperState === state && target) target.scrollIntoView({block:'start'});
    return;
  }
  const math = e.target.closest('math');
  if (math && !hlMode && !eraseMode && window.getSelection().isCollapsed) {
    const whole = math.closest('.ls-math-scroll') || math;
    const clone = whole.cloneNode(true); clone.removeAttribute('id');
    clone.querySelectorAll('[id]').forEach(n => n.removeAttribute('id'));
    $('#math-zoom-body').replaceChildren(clone); $('#math-zoom').showModal();
  }
  const image = e.target.closest('.source-html img');
  if (image) { $('#zoom-image').src = image.src; $('#image-zoom').showModal(); }
}));
$('#math-zoom-close').addEventListener('click', () => { $('#math-zoom').close(); $('#math-zoom-body').replaceChildren(); });
$('#paper-legacy').addEventListener('click', () => {
  const box = $('#legacy-items'); box.replaceChildren();
  for (const row of paperState?.legacy || []) {
    const item = document.createElement('p'); item.textContent = row.quote || ''; box.appendChild(item);
  }
  $('#legacy-dialog').showModal();
});
$('#legacy-close').addEventListener('click', () => $('#legacy-dialog').close());
$('#btn-reader-paper').addEventListener('click', () => runAction(null, showPaper));
$('#paper-more').addEventListener('click', () => runAction(null, loadNextPaper));
$('#paper-wrap').addEventListener('scroll', () => {
  const el = $('#paper-wrap');
  if (readerView === 'paper' && el.scrollHeight-el.scrollTop-el.clientHeight < 180) loadNextPaper();
});
$('#zoom-close').addEventListener('click', () => $('#image-zoom').close());
for (const [id, delta] of [['reader-font-down',-1],['reader-font-up',1]]) {
  $('#' + id).addEventListener('click', () => runAction(null,async () => {
    const size = Math.max(13,Math.min(30,(window._fontSize || 18)+delta));
    window._fontSize = size; $('#f-font').value = size;
    $('#paper-wrap').style.fontSize = size + 'px'; $('#note-md').style.fontSize = size + 'px';
    await post('/api/config',{font_size:size});
  }));
}
document.addEventListener('mouseup', () => {
  const state = paperState;
  if ((!hlMode && !eraseMode) || readerView !== 'paper' || !state || !state.source || state.annotationError) return;
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return;
  const range = selection.getRangeAt(0), rows = [];
  for (const el of $('#paper-pages').querySelectorAll('[data-block]')) {
    if (!range.intersectsNode(el)) continue;
    const text = state.texts.get(el.dataset.block);
    const prefix = document.createRange(); prefix.selectNodeContents(el);
    let start = 0, end = text.length;
    if (el.contains(range.startContainer)) { prefix.setEnd(range.startContainer,range.startOffset); start = prefix.toString().length; }
    if (el.contains(range.endContainer)) { prefix.selectNodeContents(el); prefix.setEnd(range.endContainer,range.endOffset); end = prefix.toString().length; }
    if (end > start) rows.push({block:el.dataset.block,start,end,quote:text.slice(start,end)});
  }
  if (!rows.length) return;
  const erasing = eraseMode;
  selection.removeAllRanges();
  const operation = highlightWrite.catch(() => {}).then(async () => {
    const current = await api('/api/reading-highlights?id=' + encodeURIComponent(state.id));
    if (current.source !== state.source) throw Error(t('paper_changed'));
    const highlights = erasing ? eraseTextHighlights(current.highlights,rows) : current.highlights.concat(rows);
    if (!erasing || !sameHighlightList(current.highlights,highlights)) {
      await post('/api/reading-highlights?id=' + encodeURIComponent(state.id),{source:state.source,highlights});
    }
    state.highlights = highlights;
    if (paperState === state && state.version === readerVersion) refreshReadingMarks();
  });
  highlightWrite = operation;
  operation.catch(e => showMessage(e.message,true));
});

/* ---------- model + settings ---------- */
let wasRunning = false;
async function refreshModel(){
  try {
    const d = await api('/api/state');
    $('#m-state').textContent = d.llama_up ? t('m_running') : t('m_stopped');
    $('#m-dot').classList.toggle('on', !!d.llama_up);
    $('#m-addr').textContent = 'http://127.0.0.1:' + ($('#f-port').value || '8080');
    $('#btn-mstart').disabled = !!d.batch_running;
    $('#btn-mstop').disabled = !!d.batch_running || !d.managed;
    if (wasRunning && !d.batch_running) await refreshNotes();
    wasRunning = !!d.batch_running;
  } catch(e) { $('#m-state').textContent = t('m_checking'); showMessage(e.message,true); }
}
for (const [id,route] of [['btn-mstart','start'],['btn-mstop','stop']]) {
  $('#' + id).addEventListener('click', () => runAction($('#' + id), async () => {
    await post('/api/model/' + route); await refreshModel();
  }));
}
async function loadStorage(){
  const d = await api('/api/storage');
  $('#f-vault').value = d.selected;
  $('#storage-active').textContent = t('s_active') + d.active;
  $('#storage-notice').textContent = t('s_storage_hint') + (d.restart_required ? ' ' + t('s_restart') : '') +
    (d.overridden ? ' ' + t('s_override') : '');
}
$('#btn-vault').addEventListener('click', () => runAction($('#btn-vault'), async () => {
  const path = await window.pywebview.api.pick_vault();
  if (!path) return;
  await post('/api/storage', {path}); await loadStorage(); showMessage(t('s_restart'));
}));
$('#btn-vault-open').addEventListener('click', () => runAction($('#btn-vault-open'), () => window.pywebview.api.open_vault()));

async function loadSettings(){
  try {
    const cfg = await api('/api/config');
    $('#f-html-proxy').value = cfg.html_proxy || '';
    $('#f-exe').value = cfg.llama_exe || '';
    $('#f-model').value = cfg.model_path || '';
    $('#f-port').value = cfg.port || 8080; $('#f-ctx').value = cfg.ctx || 16384;
    $('#f-schedule').value = cfg.schedule || '23:30'; $('#f-schedule2').value = cfg.schedule || '23:30';
    scheduleTime = cfg.schedule || '23:30';
    $('#f-theme').value = cfg.theme || 'light'; $('#f-font').value = cfg.font_size || 16;
    $('#f-lang').value = cfg.lang || 'zh'; window._fontSize = cfg.font_size || 16;
    document.body.classList.toggle('dark', cfg.theme === 'dark');
    settingsLoaded = true; applyLang(cfg.lang || 'zh'); await loadStorage();
  } catch(e) { showMessage(e.message,true); }
}
function collectSettings(){
  return {html_proxy:$('#f-html-proxy').value.trim(), llama_exe:$('#f-exe').value.trim(), model_path:$('#f-model').value.trim(),
    port:Number($('#f-port').value), ctx:Number($('#f-ctx').value),
    schedule:$('#f-schedule').value, theme:$('#f-theme').value,
    font_size:Number($('#f-font').value), lang:$('#f-lang').value};
}
async function saveSettings(flashSel){
  const data = collectSettings();
  await post('/api/config', data);
  document.body.classList.toggle('dark', data.theme === 'dark');
  window._fontSize = data.font_size; $('#note-md').style.fontSize = data.font_size + 'px';
  scheduleTime = data.schedule; applyLang(data.lang); await loadStorage();
  const f = $(flashSel); f.textContent = t('saved'); setTimeout(() => f.textContent = '', 2000);
}
$('#btn-save').addEventListener('click', () => runAction($('#btn-save'), () => saveSettings('#flash')));
$('#btn-save-model').addEventListener('click', () => runAction($('#btn-save-model'), async () => {
  $('#f-schedule').value = $('#f-schedule2').value;
  await saveSettings('#flash2'); await refreshModel();
}));
$('#f-lang').addEventListener('change', () => runAction(null, async () => {
  applyLang($('#f-lang').value);
  if (settingsLoaded) await saveSettings('#flash');
}));
$('#f-theme').addEventListener('change', () => document.body.classList.toggle('dark', $('#f-theme').value === 'dark'));
function escapeHtml(s){
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
applyLang('zh');
loadSettings(); refreshQueue(); refreshNotes(); refreshModel();
setInterval(() => { refreshModel(); refreshQueue(); }, 5000);
</script>
</body>
</html>

"""


def html_reader_state(vault: Path, arxiv_id: str):
    if not paper_html.ID_RE.fullmatch(arxiv_id):
        raise ValueError("论文编号无效")
    root = vault / "html_reader" / arxiv_id
    document = paper_html.get_document(root, arxiv_id, load_config(vault).get("html_proxy", ""))
    pdf = vault / "papers_pdf" / f"{arxiv_id}.pdf"
    legacy = (paper_reader.cache_for(pdf, vault / "reader" / arxiv_id) / "highlights.json") if pdf.is_file() else None
    return root, document, legacy


def serve_ui(vault: Path, open_browser: bool = False) -> None:
    page = UI_PAGE.encode("utf-8")
    app_page = APP_PAGE.encode("utf-8")
    ball_page = BALL_PAGE.encode("utf-8")
    icon_png = b""
    try:
        import io as _io

        import app_shell as _sh

        _im = _sh._app_icon_image()
        if _im is not None:
            _buf = _io.BytesIO()
            _im.save(_buf, "PNG")
            icon_png = _buf.getvalue()
    except Exception:
        icon_png = b""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype == "image/svg+xml":
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _body_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            limit = 1048576 if self.path.split("?", 1)[0] == "/api/reading-highlights" else 131072
            if not 0 <= n <= limit:
                raise ValueError("请求内容过大或长度无效")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError) as e:
                raise ValueError("无法读取请求：需要有效的 JSON") from e
            if not isinstance(data, dict):
                raise ValueError("请求必须是 JSON 对象")
            return data

        def _local_request(self, mutation=False) -> bool:
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            hosts = {f"127.0.0.1:{UI_PORT}", f"localhost:{UI_PORT}"}
            if UI_PORT == 80:
                hosts.update({"127.0.0.1", "localhost"})
            if self.headers.get("Host", "").lower() not in hosts:
                return False
            if mutation:
                origin = self.headers.get("Origin")
                if origin is not None and origin not in {f"http://{host}" for host in hosts}:
                    return False
                if self.headers.get("Content-Length", "0") != "0" and self.headers.get_content_type() != "application/json":
                    return False
            return True

        def do_GET(self) -> None:
            if not self._local_request():
                self._json({"ok": False, "msg": "只允许本地应用请求"}, 403)
                return
            try:
                self._get()
            except paper_html.HtmlUnavailable as e:
                self._json({"ok": False, "msg": str(e), "fallback": "pdf"}, 503)
            except (ValueError, TypeError, OSError) as e:
                self._json({"ok": False, "msg": str(e)[:300]}, 400)

        def do_POST(self) -> None:
            if not self._local_request(mutation=True):
                self._json({"ok": False, "msg": "请求来源或 JSON 类型无效"}, 403)
                return
            try:
                self._post()
            except (ValueError, TypeError, OSError) as e:
                self._json({"ok": False, "msg": str(e)[:300]}, 400)

        def _get(self) -> None:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif u.path == "/app":
                self._send(200, app_page, "text/html; charset=utf-8")
            elif u.path == "/ball":
                self._send(200, ball_page, "text/html; charset=utf-8")
            elif u.path == "/icon.png":
                if icon_png:
                    self._send(200, icon_png, "image/png")
                else:
                    self._send(404, b"no icon", "text/plain; charset=utf-8")
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
            elif u.path == "/api/storage":
                self._json(storage_state(vault))
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
            elif u.path in ("/api/paper", "/api/paper/anchor", "/api/paper/html-image", "/api/reading-highlights"):
                arxiv_id = q.get("id", [""])[0]
                if not paper_html.ID_RE.fullmatch(arxiv_id):
                    raise ValueError("论文编号无效")
                root = vault / "html_reader" / arxiv_id
                proxy = load_config(vault).get("html_proxy", "")
                if u.path == "/api/paper":
                    self._json(paper_html.get_page(root, arxiv_id, int(q.get("page", ["1"])[0]),
                                                   proxy, q.get("refresh", ["0"])[0] == "1"))
                elif u.path == "/api/paper/html-image":
                    data, kind = paper_html.get_image(root, arxiv_id, q.get("name", [""])[0], proxy)
                    self._send(200, data, kind)
                else:
                    root, document, legacy = html_reader_state(vault, arxiv_id)
                    if u.path == "/api/paper/anchor":
                        number = document["anchors"].get(q.get("name", [""])[0])
                        if number is None:
                            raise ValueError("原文链接位置不存在")
                        self._json({"page": number})
                    else:
                        self._json(paper_html.load_highlights(root, document, legacy))
            elif u.path == "/api/paper/image":
                arxiv_id = q.get("id", [""])[0]
                if not ARXIV_ID_RE.fullmatch(arxiv_id):
                    raise ValueError("论文编号无效")
                image = paper_reader.image_path(get_pdf_path(vault, arxiv_id),
                                                vault / "reader" / arxiv_id, q.get("name", [""])[0])
                self._send(200, image.read_bytes(), "image/png")
            elif u.path == "/api/highlights":
                arxiv_id = q.get("id", [""])[0]
                if not ARXIV_ID_RE.fullmatch(arxiv_id or ""):
                    self._send(400, b"bad id", "text/plain; charset=utf-8")
                    return
                self._json(load_highlights(vault, arxiv_id))
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def _post(self) -> None:
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            body = self._body_json()
            if u.path == "/api/config":
                try:
                    self._json(save_config(vault, body))
                except (ValueError, TypeError, OSError) as e:
                    self._json({"ok": False, "msg": str(e)[:300]}, 400)
            elif u.path == "/api/reading-highlights":
                arxiv_id = q.get("id", [""])[0]
                if not ARXIV_ID_RE.fullmatch(arxiv_id):
                    raise ValueError("论文编号无效")
                root, document, legacy = html_reader_state(vault, arxiv_id)
                self._json(paper_html.save_highlights(root, document, body, legacy))
            elif u.path == "/api/storage":
                try:
                    self._json(select_vault(vault, str(body.get("path", ""))))
                except (ValueError, OSError) as e:
                    self._json({"ok": False, "msg": str(e)[:300]}, 400)
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
            elif u.path in ("/api/queue/delete", "/api/queue/retry"):
                try:
                    action = (delete_queue_item if u.path.endswith("/delete")
                              else retry_queue_item)
                    action(vault, body.get("id", ""))
                    self._json({"ok": True})
                except (ValueError, OSError) as e:
                    self._json({"ok": False, "msg": str(e)[:300]}, 400)
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
            elif u.path in ("/api/model/start", "/api/model/stop"):
                if not _batch_lock.acquire(blocking=False):
                    self._json({"ok": False, "msg": "队列正在使用模型，请等待处理完成"}, 409)
                    return
                try:
                    if u.path.endswith("/start"):
                        ok = start_server(vault, load_config(vault))
                        if not ok:
                            stop_server(vault)
                        self._json({"ok": ok,
                                    "msg": "" if ok else "启动失败，检查配置及 model-server.log"})
                    else:
                        stopped = stop_server(vault)
                        self._json({"ok": True, "stopped": stopped})
                finally:
                    _batch_lock.release()
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
    """Packaged-app mode (v4): butler + scheduler + web UI in threads;
    the desktop shell lives in app_shell.py - a native layered-window
    ball (truly transparent), a pystray tray icon that owns exit, a
    single-instance mutex with wake-up, and the main panel docked
    flush to the right edge of the desktop (v4.2: fixed position,
    appears instantly with no slide animation; the ball - which
    itself only slides up and down that same edge and wears the app
    icon - hides while the panel is open), driven by an
    AppController command queue. Falls back to headless if
    pywebview is missing."""
    _ensure_vault(vault)
    log(vault, f"START app mode vault={vault}")
    cfg = load_config(vault)
    global LLAMA_BASE
    LLAMA_BASE = f"http://127.0.0.1:{int(cfg['port'])}"

    import app_shell

    if not app_shell.acquire_single_instance():
        app_shell.signal_existing_instance()
        print("LazyScholar is already running; woke its panel instead.",
              flush=True)
        return 0

    _shutting_down.clear()
    recover_queue(vault)
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

    wa_l, wa_t, wa_r, wa_b = app_shell.work_area()
    wa_h = wa_b - wa_t
    # Panel geometry is kept in physical pixels (same space as the native
    # ball window); the design sizes below are logical px, scaled here so a
    # 150% display still gets the intended proportions.
    scale = app_shell.dpi_scale() if app_shell.WIN else 1.0
    panel_w = round(404 * scale)
    panel_h = min(round(app_shell.AppController.TAB_HEIGHTS["queue"] * scale),
                  wa_h - round(24 * scale))
    panel_x = wa_r - panel_w               # flush to the right edge
    # the panel slides along the edge; remember where it was left
    panel_y = int(cfg.get("panel_y") or (wa_t + round(12 * scale)))
    panel_y = max(wa_t, min(panel_y, wa_b - panel_h))

    icon_path = None
    for cand in (Path(getattr(sys, "_MEIPASS", "")) / "lazyscholar.ico",
                 Path(__file__).resolve().parent / "lazyscholar.ico"):
        if cand.exists():
            icon_path = str(cand)
            break

    holder: dict = {}

    class AppApi:
        def toggle_main(self):
            holder["ctl"].toggle_panel()

        def hide_main(self):
            holder["ctl"].hide_panel()

        def quit_app(self):
            holder["ctl"].quit()

        def set_panel_tab(self, name):
            holder["ctl"].set_tab(str(name))

        def pick_vault(self):
            dialog = getattr(webview, "FileDialog", None)
            folder = dialog.FOLDER if dialog is not None else webview.FOLDER_DIALOG
            res = holder["panel"].create_file_dialog(folder, directory=str(vault))
            return res[0] if res else None

        def open_vault(self):
            os.startfile(str(vault))

        def pick_pdf(self):
            dialog = getattr(webview, "FileDialog", None)
            open_const = dialog.OPEN if dialog is not None else webview.OPEN_DIALOG
            res = holder["panel"].create_file_dialog(
                open_const, file_types=("PDF Files (*.pdf)",))
            return res[0] if res else None

    api = AppApi()
    panel = webview.create_window(
        "LazyScholar · 懒人学者", f"http://127.0.0.1:{UI_PORT}/app",
        js_api=api, width=panel_w, height=panel_h,
        x=panel_x, y=panel_y,
        frameless=True, easy_drag=False, text_select=True,
        on_top=True, hidden=True,
        shadow=True, background_color="#F8F6F0")
    if panel is None:
        raise RuntimeError("无法创建应用窗口")
    holder["panel"] = panel

    def _run_queue_now():
        process_queue(vault)

    def _open_vault():
        try:
            os.startfile(str(vault))  # Windows only; guarded by caller
        except Exception as e:
            log(vault, f"OPEN vault failed: {e}")

    def _panel_moved(y):
        fresh = load_config(vault)
        fresh["panel_y"] = y
        save_config(vault, fresh)

    ctl = app_shell.AppController(
        panel, (panel_x, panel_y),
        run_queue_now=_run_queue_now,
        open_vault=_open_vault if app_shell.WIN else None,
        panel_w=panel_w, max_h=wa_h - round(24 * scale),
        on_panel_moved=_panel_moved, y_bounds=(wa_t, wa_b), scale=scale)
    holder["ctl"] = ctl
    try:
        panel.events.moved += ctl.on_window_moved
    except Exception as e:
        log(vault, f"PANEL moved event unavailable: {e}")

    def _on_closing():
        # Closing the panel only tucks it away; exit belongs to the
        # tray menu and the ball's right-click (via the controller).
        if ctl.quitting:
            return True
        ctl.hide_panel()
        return False

    try:
        panel.events.closing += _on_closing
    except Exception as e:
        log(vault, f"PANEL closing event unavailable: {e}")

    # Floating ball: native layered window on Windows; the old
    # pywebview ball remains as a dev fallback elsewhere.
    if app_shell.WIN:
        try:
            ball_size = int(cfg.get("ball_size") or app_shell.BALL_SIZE)
        except Exception:
            ball_size = app_shell.BALL_SIZE
        ball_size = max(32, min(90, ball_size))
        ball_w, ball_h = app_shell.ball_window_size(ball_size)
        ball_x = wa_r - ball_w             # composite flush to the right edge
        ball_y = int(cfg.get("ball_y") or (wa_t + (wa_h - ball_h) // 2))
        ball_y = max(wa_t, min(ball_y, wa_b - ball_h))  # stay on the edge

        def _ball_moved(x, y):
            # only the height along the edge is worth remembering
            fresh = load_config(vault)
            fresh["ball_y"] = y
            fresh.pop("ball_x", None)
            save_config(vault, fresh)

        ctl.ball = app_shell.BallWindow(ctl, ball_x, ball_y, on_moved=_ball_moved,
                                        size=ball_size)
        ctl.ball.start()
    else:
        webview.create_window(
            "", f"http://127.0.0.1:{UI_PORT}/ball",
            js_api=api, width=68, height=68,
            x=max(0, wa_r - 76), y=max(0, wa_t + wa_h // 2 - 34),
            frameless=True, on_top=True)

    ctl.tray = app_shell.build_tray(ctl, icon_path, str(cfg.get("lang", "zh")))
    wake = app_shell.start_wake_listener(ctl.show_panel)
    try:
        try:
            webview.start(func=ctl.run, icon=icon_path)
        except TypeError:
            webview.start(func=ctl.run)
    finally:
        shutdown_model(vault)
    if wake is not None:
        try:
            wake.close()
        except Exception as e:
            log(vault, f"WAKE listener close failed: {e}")
    log(vault, "STOP app mode")
    return 0


def _default_vault() -> str:
    """Vault used when --vault is not given. Explorer launches have no
    arguments, so fall back to the machine's working vault instead of
    silently creating a second one under the user profile."""
    env = os.environ.get("LAZYSCHOLAR_VAULT")
    if env:
        return env
    preferred = _preferred_vault()
    if preferred:
        return preferred
    shared = Path("D:/LazyScholarVault")
    if shared.exists():
        return str(shared)
    return str(Path.home() / "LazyScholarVault")


def main() -> int:
    ap = argparse.ArgumentParser(description="LazyScholar unattended knowledge butler")
    ap.add_argument("--vault", default=None,
                    help="Obsidian vault path (default: shared vault, else ~/LazyScholarVault)")
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
    vault = Path(args.vault or _default_vault()).expanduser()
    # A packaged exe started from Explorer gets no arguments; open the
    # desktop shell instead of silently running as a headless daemon
    # (which looked like "double-clicking the exe does nothing").
    bare_launch = bool(getattr(sys, "frozen", False)) and not (
        args.ui or args.once or args.no_ui or args.open_ui)
    if args.app or bare_launch:
        return run_app(vault, args.interval)
    if args.ui:
        _ensure_vault(vault)
        try:
            serve_ui(vault, args.open_ui)
        except KeyboardInterrupt:
            return 0
        finally:
            shutdown_model(vault)
        return 0
    return run(vault, args.once, args.interval,
               ui=not args.no_ui, open_ui=args.open_ui)


if __name__ == "__main__":
    sys.exit(main())
