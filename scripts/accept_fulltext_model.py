"""Opt-in real local-model acceptance over one saved webpage, isolated under qa/."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

import test_app_backend as backend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Saved HTML/MHTML; no network acquisition")
    args = parser.parse_args()
    app = backend.app
    vault = backend.new_vault("real-fulltext-model")
    item = app.add_queue_item(vault, "html", str(args.source.resolve()))
    app.LLM_TIMEOUT = 90
    original_chat, original_update = app.llm_chat, app.update_queue_item
    calls = []

    def traced_chat(system, user, max_tokens=1400):
        start = time.monotonic()
        raw = original_chat(system, user, max_tokens)
        calls.append(round(time.monotonic() - start, 2))
        (vault / f"model-response-{len(calls):02d}.txt").write_text(raw, encoding="utf-8")
        print(json.dumps({"model_call": len(calls), "seconds": calls[-1]}), flush=True)
        return raw

    def traced_update(path, item_id, **changes):
        if changes.get("progress"):
            print(json.dumps({"progress": changes["progress"]}, ensure_ascii=True), flush=True)
        return original_update(path, item_id, **changes)

    with patch.object(app, "llm_chat", side_effect=traced_chat), patch.object(app, "update_queue_item", side_effect=traced_update):
        result = app.process_queue(vault)
    queue = app.load_queue(vault)[0]
    report = {"result": result, "status": queue["status"], "error": queue.get("error"),
              "model_calls": len(calls), "vault": str(vault), "paper_id": item["paper_id"]}
    if queue.get("note"):
        report["note"] = str(vault / "papers" / queue["note"])
        try:
            evidence = next((vault / "fulltext").glob("*.json"))
            record = json.loads(evidence.read_text(encoding="utf-8"))
        except (OSError, ValueError, StopIteration) as exc:
            raise RuntimeError("生成结果缺少可读取的原文依据，验收不通过") from exc
        report["processed_chunks"] = len(record["chunks"])
        report["last_part"] = record["chunks"][-1]["part"]
        report["last_text_tail"] = record["chunks"][-1]["text"][-180:]
    (vault / "real-model-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    if queue["status"] != "done":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
