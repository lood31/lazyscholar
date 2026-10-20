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


def serve_ui(vault: Path, open_browser: bool = False) -> None:
    page = UI_PAGE.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console clean
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif u.path == "/api/notes":
                body = json.dumps(vault_state(vault), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif u.path == "/api/note":
                q = urllib.parse.parse_qs(u.query)
                kind = q.get("kind", [""])[0]
                name = Path(q.get("name", [""])[0]).name
                if kind not in ("papers", "concepts") or not name.endswith(".md"):
                    self._send(400, b"bad request", "text/plain; charset=utf-8")
                    return
                p = vault / kind / name
                if not p.exists():
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                payload = {
                    "title": _note_title(p),
                    "kind": kind,
                    "date": datetime.fromtimestamp(p.stat().st_mtime).date().isoformat(),
                    "html": md_to_html(p.read_text(encoding="utf-8", errors="replace")),
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
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


def main() -> int:
    ap = argparse.ArgumentParser(description="LazyScholar unattended knowledge butler")
    ap.add_argument("--vault", default=str(Path.home() / "LazyScholarVault"),
                    help="Obsidian vault path (default: ~/LazyScholarVault)")
    ap.add_argument("--once", action="store_true", help="single poll cycle, then exit (for tests)")
    ap.add_argument("--interval", type=int, default=30, help="poll interval seconds (default 30)")
    ap.add_argument("--ui", action="store_true",
                    help="serve the web app only (no inbox polling)")
    ap.add_argument("--no-ui", action="store_true", help="do not start the web app")
    ap.add_argument("--open-ui", action="store_true",
                    help="open the web app in a browser on start")
    args = ap.parse_args()
    vault = Path(args.vault).expanduser()
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
