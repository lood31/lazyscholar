"""Generation follows persisted UI language; existing notes are never translated."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import test_app_backend as backend
from test_fulltext import FakeChat, source

app = backend.app
engine = app.fulltext
ENGLISH_SECTIONS = ("One-sentence summary", "Reading guide", "Methods and contributions",
                    "Results and limitations", "Reading suggestions")


class LanguageChat(FakeChat):
    def __init__(self, language):
        super().__init__()
        self.language = language
        self.calls = []

    def __call__(self, system, user, max_tokens):
        self.calls.append((system, user))
        expected = "Output language: English." if self.language == "en" else "输出语言：中文。"
        if expected not in system:
            raise AssertionError("Persisted UI language did not reach a model call: " + expected)
        if self.language == "zh":
            return super().__call__(system, user, max_tokens)
        if "<source>" in user:
            self.source_inputs.append(user)
            return json.dumps({"summary": "The supplied passage describes a method with limitations.",
                               "evidence": [{"point": "The passage reports a limitation.", "passages": ["S001"]}]})
        refs = sorted(set(engine.CITATION.findall(user)))
        if '"sections"' in user:
            self.finals += 1
            return json.dumps({"sections": [{"heading": heading, "text": "The supplied text supports this reading point.",
                                             "sources": [refs[0][1:-1]]} for heading in ENGLISH_SECTIONS]})
        self.merges += 1
        return "The supplied passages report the method and its limitations. " + " ".join(refs)


class GenerationLanguageTests(unittest.TestCase):
    def setUp(self):
        self.vault = backend.new_vault(self._testMethodName)

    def test_new_fulltext_note_uses_english_but_preserves_existing_notes(self):
        existing = self.vault / "papers" / "existing.md"
        existing.write_text("# 已有中文导读\n\n不应被语言切换改写。", encoding="utf-8")
        original = existing.read_bytes()
        app.save_config(self.vault, {"lang": "en"})
        chat = LanguageChat("en")
        with patch.object(app, "llm_chat", side_effect=chat):
            note = app.write_full_text_note(self.vault, "2510.00001", source("A reported method and its limitation. " * 40))
        text = note.read_text(encoding="utf-8")
        for heading in ENGLISH_SECTIONS:
            self.assertIn("## " + heading, text)
        self.assertIn("## Reading scope and limitations", text)
        self.assertIn("## Source evidence", text)
        self.assertNotIn("## 中文导读", text)
        self.assertEqual(original, existing.read_bytes())
        ledger = json.loads(next((self.vault / "fulltext").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("en", ledger["language"])

    def test_english_is_used_in_chunk_merge_final_and_retry_calls(self):
        chat = LanguageChat("en")
        guide, ledger = engine.summarize(source("A method with clearly reported limitations. " * 2200), chat, 8192, language="en")
        self.assertGreater(chat.merges, 0)
        self.assertIn("## Reading guide", guide)
        self.assertEqual("en", ledger["language"])
        for _system, user in chat.calls:
            self.assertNotIn("中文摘要", user)
            self.assertNotIn("中文内容", user)
        retry = LanguageChat("en")
        seen = set()
        def reject_once(system, user, tokens):
            expected = "Output language: English."
            self.assertIn(expected, system)
            stage = "chunk" if "<source>" in user else "final"
            if stage not in seen:
                seen.add(stage)
                return "invalid JSON"
            return retry(system, user, tokens)
        engine.summarize(source("Reported method and limitations. " * 8), reject_once, 16384, language="en")
        self.assertEqual({"chunk", "final"}, seen)

    def test_next_task_observes_language_switch_but_active_task_keeps_snapshot(self):
        app.save_config(self.vault, {"lang": "en"})
        english = LanguageChat("en")
        def switch_during_generation(system, user, tokens):
            app.save_config(self.vault, {"lang": "zh"})
            return english(system, user, tokens)
        with patch.object(app, "llm_chat", side_effect=switch_during_generation):
            first = app.write_full_text_note(self.vault, "2510.00002", source("Reported method and limitations. " * 8))
        self.assertIn("## Reading guide", first.read_text(encoding="utf-8"))
        chinese = LanguageChat("zh")
        with patch.object(app, "llm_chat", side_effect=chinese):
            second = app.write_full_text_note(self.vault, "2510.00003", source("Reported method and limitations. " * 8))
        self.assertIn("## 中文导读", second.read_text(encoding="utf-8"))

    def test_legacy_abstract_and_discussion_generation_also_follow_language(self):
        app.save_config(self.vault, {"lang": "en"})
        meta = {"title": "Language fixture", "authors": ["Fixture Author"], "url": "https://arxiv.org/abs/2510.00004",
                "summary": "The abstract reports a method and a limitation."}
        def english(system, user, max_tokens):
            self.assertIn("Output language: English.", system)
            self.assertNotIn("中文导读", user)
            return "An English guide."
        discussion = self.vault / "discussion.txt"
        discussion.write_text("A discussion about an algorithm.", encoding="utf-8")
        with patch.object(app, "llm_chat", side_effect=english):
            app.write_paper_note(self.vault, meta, "2510.00004")
            app.write_discussion_note(self.vault, discussion)


if __name__ == "__main__":
    unittest.main()
