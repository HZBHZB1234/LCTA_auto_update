"""新专有名词回流链路的回归测试。

对应本次改造：模型在翻译响应里回传 glossary 未收录的专有名词，
经「过滤 + 计票 → 本地落盘 → 下次运行合并」后成为正式的术语资产；
同时支持在本轮运行内热更新术语表，让后续文件立刻命中。
"""
from __future__ import annotations

import json
import threading

from translateFunc.matcher.engine import MatcherEngine
from translateFunc.proper.new_terms import (
    NewTermCollector,
    load_learned,
    merge_terms,
    sanitize_entry,
)


# ============================================================
# 1. 单条候选的过滤
# ============================================================

class TestSanitizeEntry:
    def test_accepts_normal_term(self):
        result = sanitize_entry({"kr": "리나모비블", "cn": "莉娜莫维尔", "category": "person"})
        assert result == {"term": "리나모비블", "translation": "莉娜莫维尔", "category": "person"}

    def test_unknown_category_falls_back_to_other(self):
        result = sanitize_entry({"kr": "리나모비블", "cn": "莉娜莫维尔", "category": "胡说"})
        assert result["category"] == "other"

    def test_rejects_non_dict(self):
        assert sanitize_entry("리나모비블") is None
        assert sanitize_entry(None) is None

    def test_rejects_missing_side(self):
        assert sanitize_entry({"kr": "리나모비블", "cn": ""}) is None
        assert sanitize_entry({"kr": "", "cn": "莉娜莫维尔"}) is None

    def test_rejects_untranslated_target(self):
        # 译文与原形相同、或仍是韩文，都不是有效译名
        assert sanitize_entry({"kr": "리나모비블", "cn": "리나모비블"}) is None
        assert sanitize_entry({"kr": "리나모비블", "cn": "莉娜모비블"}) is None

    def test_rejects_too_short_or_too_long(self):
        assert sanitize_entry({"kr": "가", "cn": "甲"}) is None
        assert sanitize_entry({"kr": "가" * 40, "cn": "甲" * 3}) is None

    def test_rejects_structural_characters(self):
        # 占位符 / 富文本 / 引号都说明这不是一个名词原形
        assert sanitize_entry({"kr": "{0}", "cn": "占位符"}) is None
        assert sanitize_entry({"kr": "<color=red>", "cn": "红色"}) is None
        assert sanitize_entry({"kr": "[Bleed]", "cn": "出血"}) is None
        assert sanitize_entry({"kr": "리나\n모비", "cn": "莉娜"}) is None

    def test_rejects_oversized_translation(self):
        assert sanitize_entry({"kr": "리나모비블", "cn": "译" * 50}) is None


# ============================================================
# 2. 收集与计票
# ============================================================

class TestCollector:
    def test_counts_votes_and_picks_majority(self, tmp_path):
        collector = NewTermCollector(tmp_path / "learned.json")

        collector.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}], source="a.json")
        collector.add([{"kr": "리나모비블", "cn": "莉娜莫薇尔"}], source="b.json")
        collector.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}], source="c.json")

        snapshot = {item["term"]: item for item in collector.snapshot()}
        assert snapshot["리나모비블"]["translation"] == "莉娜莫维尔"
        assert snapshot["리나모비블"]["votes"] == 2
        assert snapshot["리나모비블"]["sources"] == ["a.json", "c.json"]

    def test_known_terms_are_skipped(self, tmp_path):
        collector = NewTermCollector(tmp_path / "learned.json")

        accepted = collector.add(
            [{"kr": "리나모비블", "cn": "莉娜莫维尔"}],
            source="a.json",
            known_terms={"리나모비블"},
        )

        assert accepted == 0
        assert collector.snapshot() == []

    def test_mark_known_suppresses_future_learning(self, tmp_path):
        collector = NewTermCollector(tmp_path / "learned.json")
        collector.mark_known(["리나모비블"])

        accepted = collector.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}])

        assert accepted == 0

    def test_min_votes_filters_singletons(self, tmp_path):
        collector = NewTermCollector(tmp_path / "learned.json", min_votes=2)

        collector.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}])
        assert collector.snapshot() == []

        collector.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}])
        assert len(collector.snapshot()) == 1

    def test_noise_is_dropped_silently(self, tmp_path):
        collector = NewTermCollector(tmp_path / "learned.json")

        accepted = collector.add([
            {"kr": "{0}", "cn": "占位"},
            {"kr": "가", "cn": "甲"},
            {"kr": "리나모비블", "cn": "莉娜莫维尔"},
            "垃圾数据",
        ])

        assert accepted == 1
        assert [item["term"] for item in collector.snapshot()] == ["리나모비블"]

    def test_concurrent_add_does_not_lose_votes(self, tmp_path):
        """翻译主流程跑在 WorkerPool 里，收集必须线程安全。"""
        collector = NewTermCollector(tmp_path / "learned.json")
        workers = 8
        per_worker = 25

        def worker(index: int) -> None:
            for _ in range(per_worker):
                collector.add(
                    [{"kr": "리나모비블", "cn": "莉娜莫维尔"}],
                    source=f"{index}.json",
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = collector.snapshot()
        assert len(snapshot) == 1
        assert snapshot[0]["votes"] == workers * per_worker


# ============================================================
# 3. 落盘与载入
# ============================================================

class TestPersistence:
    def test_flush_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "proper_learned.json"
        collector = NewTermCollector(path)
        collector.add(
            [{"kr": "리나모비블", "cn": "莉娜莫维尔", "category": "person"}],
            source="Skills_A.json",
        )

        assert collector.flush() == 1
        assert path.exists()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["terms"][0]["term"] == "리나모비블"
        # 落盘字段与 proper_path 的 schema 一致，可直接当术语表使用
        assert set(payload["terms"][0]) >= {"term", "translation", "note"}

        learned = load_learned(path)
        assert learned[0]["translation"] == "莉娜莫维尔"
        assert learned[0]["votes"] == 1

    def test_flush_without_terms_writes_nothing(self, tmp_path):
        path = tmp_path / "proper_learned.json"
        collector = NewTermCollector(path)

        assert collector.flush() == 0
        assert not path.exists()

    def test_already_learned_terms_are_not_relearned(self, tmp_path):
        """已落盘的词视为已知：下次运行不再重复收集，避免票数无意义膨胀。"""
        path = tmp_path / "proper_learned.json"
        first = NewTermCollector(path)
        first.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}])
        first.flush()

        second = NewTermCollector(path)
        accepted = second.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}])

        assert accepted == 0
        assert second.snapshot()[0]["votes"] == 1

    def test_new_term_in_second_run_is_still_learned(self, tmp_path):
        path = tmp_path / "proper_learned.json"
        first = NewTermCollector(path)
        first.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}])
        first.flush()

        second = NewTermCollector(path)
        accepted = second.add([{"kr": "새로운고유명사", "cn": "新专有名"}])

        assert accepted == 1
        assert {item["term"] for item in second.snapshot()} == {
            "리나모비블", "새로운고유명사",
        }

    def test_corrupted_file_is_tolerated(self, tmp_path):
        path = tmp_path / "proper_learned.json"
        path.write_text("{ 不是 json", encoding="utf-8")

        assert load_learned(path) == []
        collector = NewTermCollector(path)          # 不应抛异常
        assert collector.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}]) == 1

    def test_load_accepts_bare_list(self, tmp_path):
        path = tmp_path / "learned.json"
        path.write_text(
            json.dumps([{"term": "리나모비블", "translation": "莉娜莫维尔"}], ensure_ascii=False),
            encoding="utf-8",
        )

        assert len(load_learned(path)) == 1


# ============================================================
# 4. 合并策略：远程表优先
# ============================================================

class TestMergeTerms:
    def test_new_terms_are_appended(self):
        base = [{"term": "리나모비블", "translation": "莉娜莫维尔", "note": ""}]
        learned = [{"term": "새로운고유명사", "translation": "新专有名", "note": "llm-learned"}]

        merged, stats = merge_terms(base, learned)

        assert [t["term"] for t in merged] == ["리나모비블", "새로운고유명사"]
        assert stats == {"added": 1, "shadowed": 0}

    def test_remote_term_wins_over_learned(self):
        """人工维护的术语表一旦收录该词，本地建议必须让位。"""
        base = [{"term": "리나모비블", "translation": "人工译名", "note": ""}]
        learned = [{"term": "리나모비블", "translation": "机器译名", "note": "llm-learned"}]

        merged, stats = merge_terms(base, learned)

        assert merged[0]["translation"] == "人工译名"
        assert stats == {"added": 0, "shadowed": 1}

    def test_empty_translation_is_skipped(self):
        merged, stats = merge_terms([], [{"term": "리나모비블", "translation": "  "}])
        assert merged == []
        assert stats["added"] == 0

    def test_base_is_not_mutated(self):
        base = [{"term": "리나모비블", "translation": "莉娜莫维尔"}]
        merge_terms(base, [{"term": "새로운고유명사", "translation": "新专有名"}])
        assert len(base) == 1


# ============================================================
# 5. 轮内热更新
# ============================================================

class TestHotUpdate:
    def test_batch_is_emitted_only_after_threshold(self, tmp_path):
        collector = NewTermCollector(tmp_path / "learned.json")
        collector.add([{"kr": "리나모비블", "cn": "莉娜莫维尔"}])

        assert collector.take_hot_update_batch(2) == []
        assert len(collector.take_hot_update_batch(1)) == 1
        assert collector.take_hot_update_batch(1) == []      # 取走后清空

    def test_engine_picks_up_new_terms_without_restart(self):
        engine = MatcherEngine()
        engine.build_proper([{"term": "리나모비블", "translation": "莉娜莫维尔"}])

        assert engine.match_proper("리나모비블 등장")
        assert engine.match_proper("새로운고유명사 등장") == []

        added = engine.add_proper_terms(
            [{"term": "새로운고유명사", "translation": "新专有名"}]
        )

        assert added == 1
        assert engine.match_proper("새로운고유명사 등장")

    def test_engine_hot_update_is_idempotent(self):
        engine = MatcherEngine()
        engine.build_proper([])

        assert engine.add_proper_terms([{"term": "새로운고유명사", "translation": "新"}]) == 1
        assert engine.add_proper_terms([{"term": "새로운고유명사", "translation": "新"}]) == 0

    def test_engine_proper_terms_exposes_snapshot(self):
        engine = MatcherEngine()
        engine.build_proper([{"term": "리나모비블", "translation": "莉娜莫维尔"}])

        snapshot = engine.proper_terms
        assert set(snapshot) == {"리나모비블"}
        snapshot.clear()
        assert set(engine.proper_terms) == {"리나모비블"}   # 快照不影响内部状态

    def test_engine_without_proper_data_is_searchable(self):
        """enable_proper=False 时 AC 自动机也应处于已构建状态。"""
        engine = MatcherEngine()
        assert engine.match_proper("任意文本") == []


# ============================================================
# 6. 管线接入：启动时合并本地新词
# ============================================================

class TestPipelineIntegration:
    def _pipeline(self, tmp_path):
        from translateFunc.config import TranslateConfig
        from translateFunc.pipeline import TranslationPipeline

        return TranslationPipeline(TranslateConfig(output_dir=tmp_path))

    def test_learned_terms_are_merged_into_glossary(self, tmp_path):
        pipeline = self._pipeline(tmp_path)
        collector = NewTermCollector(pipeline._new_terms_path())
        collector.add([{"kr": "새로운고유명사", "cn": "新专有名", "category": "item"}])
        collector.flush()

        merged = pipeline._merge_learned_terms(
            [{"term": "리나모비블", "translation": "莉娜莫维尔"}]
        )

        assert [item["term"] for item in merged] == ["리나모비블", "새로운고유명사"]

    def test_remote_glossary_wins_over_learned(self, tmp_path):
        pipeline = self._pipeline(tmp_path)
        collector = NewTermCollector(pipeline._new_terms_path())
        collector.add([{"kr": "새로운고유명사", "cn": "机器译名"}])
        collector.flush()

        merged = pipeline._merge_learned_terms(
            [{"term": "새로운고유명사", "translation": "人工译名"}]
        )

        assert merged == [{"term": "새로운고유명사", "translation": "人工译名"}]

    def test_missing_file_is_a_noop(self, tmp_path):
        pipeline = self._pipeline(tmp_path)
        base = [{"term": "리나모비블", "translation": "莉娜莫维尔"}]

        assert pipeline._merge_learned_terms(base) is base

    def test_switch_off_skips_merging(self, tmp_path):
        from translateFunc.config import TranslateConfig
        from translateFunc.pipeline import TranslationPipeline

        pipeline = TranslationPipeline(
            TranslateConfig(output_dir=tmp_path, enable_new_terms=False)
        )
        collector = NewTermCollector(pipeline._new_terms_path())
        collector.add([{"kr": "새로운고유명사", "cn": "新专有名"}])
        collector.flush()
        base = [{"term": "리나모비블", "translation": "莉娜莫维尔"}]

        assert pipeline._merge_learned_terms(base) is base
