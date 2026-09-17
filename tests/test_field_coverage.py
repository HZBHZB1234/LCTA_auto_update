"""字段级覆盖判定 / 韩文回填检测的回归测试。

对应漏翻修复：
  1. 「已翻译」判定从条目 id 级下沉到 (条目键, 字段路径) 级；
  2. 删除对 llc_index 的 _align —— 否则 「KR ⊆ LLC」判定恒真、整文件被跳过；
  3. 产出以 KR 结构为骨架、用 LLC 已翻译字段回填，不再整条替换；
  4. 模型把韩文原文原样回填时能被识别。
"""
import json
from pathlib import Path

from translateFunc.config import FilePathConfig, PathConfig, TranslateConfig
from translateFunc.enums import ProcessResult
from translateFunc.processor import (
    FileProcessor,
    contains_hangul,
    is_untranslated_hangul,
)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8-sig")


def build_processor(
    tmp_path: Path,
    *,
    kr: dict,
    llc: dict | None = None,
    jp: dict | None = None,
    en: dict | None = None,
    story: bool = False,
    name: str = "Sample.json",
    file_name_prefix: str = "KR_",
) -> FileProcessor:
    """按生产目录布局（KR_/JP_/EN_ 前缀 + 无前缀 LLC）落盘并返回已建索引的处理器。"""
    kr_root = tmp_path / "kr"
    jp_root = tmp_path / "jp"
    en_root = tmp_path / "en"
    llc_root = tmp_path / "llc"
    rel_dir = Path("StoryData") if story else Path(".")

    kr_path = kr_root / rel_dir / f"{file_name_prefix}{name}"
    _write(kr_path, kr)
    if jp is not None:
        _write(jp_root / rel_dir / f"JP_{name}", jp)
    if en is not None:
        _write(en_root / rel_dir / f"EN_{name}", en)
    if llc is not None:
        _write(llc_root / rel_dir / name, llc)

    base = PathConfig(
        target_path=tmp_path / "out" / "LLc-CN-LCTA",
        llc_base_path=llc_root,
        KR_base_path=kr_root,
        JP_base_path=jp_root,
        EN_base_path=en_root,
    )
    processor = FileProcessor.__new__(FileProcessor)
    processor.path_config = FilePathConfig(
        KR_path=kr_path, _PathConfig=base, has_prefix=True
    )
    processor._config = TranslateConfig()
    processor._recorder = None
    processor._api_calls = []
    processor._last_failed_call = None
    processor._input_text_blocks = []
    processor._input_reference = {}
    processor.kr_json = {}
    processor.jp_json = {}
    processor.en_json = {}
    processor.llc_json = {}
    processor.translating_list = []
    processor.translating_fields = {}
    processor._base_index = {}
    processor._load_jsons()
    processor._init_base_data()
    processor._make_data_index()
    return processor


def _entry(processor, key):
    data = processor._de_get_translating()["dataList"]
    return data[list(processor.kr_index).index(key)]


# ============================================================
# 1. 条目内部新增字段不得被判为「已翻译」
# ============================================================

class TestFieldLevelCoverage:
    def test_new_field_inside_existing_entry_is_translated(self, tmp_path):
        """旧 id 里新增的技能等级必须进入待翻译集合（历史漏翻主因）。"""
        kr = {"dataList": [{
            "id": "2030411",
            "levelList": [
                {"name": "가", "desc": "가나다"},
                {"name": "나", "desc": "라마바"},
                {"name": "다", "desc": "사아자"},          # 本次新增的等级
            ],
        }]}
        llc = {"dataList": [{
            "id": "2030411",
            "levelList": [
                {"name": "甲", "desc": "甲乙丙"},
                {"name": "乙", "desc": "丁戊己"},
            ],
        }]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)

        assert p._check_translated() is None
        p._get_translating()
        assert p.translating_list == ["2030411"]
        assert p.translating_fields["2030411"] == [("levelList", 2, "name"),
                                                   ("levelList", 2, "desc")]

    def test_request_text_only_contains_pending_fields(self, tmp_path):
        """部分覆盖的条目只把未覆盖字段送进请求，已翻译的不重复送。"""
        kr = {"dataList": [{"id": "A", "name": "가", "desc": "새 설명"}]}
        llc = {"dataList": [{"id": "A", "name": "甲"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)
        p._get_translating()

        assert p._get_translating_text("kr") == {"A": {("desc",): "새 설명"}}

    def test_fully_covered_file_still_skipped(self, tmp_path):
        """真正全覆盖的文件仍走 ALREADY_TRANSLATED + 原样拷贝。"""
        kr = {"dataList": [{"id": "A", "name": "가", "desc": "가나"}]}
        llc = {"dataList": [{"id": "A", "name": "甲", "desc": "甲乙"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)

        outcome = p._check_translated()
        assert outcome is not None
        assert outcome.result == ProcessResult.ALREADY_TRANSLATED
        assert p.path_config.target_file.exists()

    def test_llc_shorter_than_kr_is_not_skipped(self, tmp_path):
        """回归：jp/kr/en 长度不一致时，旧 _align 曾让整文件被误判为已翻译。"""
        kr = {"dataList": [{"id": "1", "name": "가"}, {"id": "2", "name": "나"},
                           {"id": "3", "name": "다"}]}
        llc = {"dataList": [{"id": "1", "name": "甲"}, {"id": "2", "name": "乙"}]}
        # jp 比 kr 多一条 → 触发长度不一致分支
        jp = {"dataList": [{"id": "1", "name": "ア"}, {"id": "2", "name": "イ"},
                           {"id": "3", "name": "ウ"}, {"id": "4", "name": "エ"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=jp, en=kr)

        assert p._check_translated() is None
        p._get_translating()
        assert p.translating_list == ["3"]

    def test_story_tail_lines_are_translated(self, tmp_path):
        """剧情文件按位置索引：LLC 比 KR 短时，尾部新行必须被翻译。"""
        kr = {"dataList": ["一", "二", "三"]}
        llc = {"dataList": ["壹", "贰"]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr, story=True)

        assert p._check_translated() is None
        p._get_translating()
        assert p.translating_list == [2]

    def test_missing_reference_language_does_not_crash(self, tmp_path):
        """jp/en 缺少某条目时不应抛 KeyError（参考文本按空处理）。"""
        kr = {"dataList": [{"id": "A", "name": "가"}, {"id": "B", "name": "나"}]}
        llc = {"dataList": [{"id": "A", "name": "甲"}]}
        jp = {"dataList": [{"id": "A", "name": "ア"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=jp, en=jp)
        p._get_translating()

        assert p.translating_list == ["B"]
        assert p._get_translating_text("jp") == {"B": {}}
        assert p._get_translating_text("kr") == {"B": {("name",): "나"}}


# ============================================================
# 2. 产出组装：KR 骨架 + LLC 回填 + 新译文
# ============================================================

class TestOutputAssembly:
    def test_llc_values_are_merged_field_wise(self, tmp_path):
        kr = {"dataList": [{"id": "A", "name": "가", "desc": "새 설명", "usage": "x"}]}
        llc = {"dataList": [{"id": "A", "name": "甲", "usage": "old"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)
        p._get_translating()

        p._de_get_translating_text({"A": {("desc",): "新说明"}})
        entry = _entry(p, "A")

        assert entry["name"] == "甲"            # 已翻译字段从 LLC 回填
        assert entry["desc"] == "新说明"        # 新字段用本次译文
        assert entry["usage"] == "x"            # AVOID_PATH 字段以 KR 为准

    def test_kr_skeleton_keeps_new_nested_levels(self, tmp_path):
        kr = {"dataList": [{"id": "S", "levelList": [
            {"name": "가", "coinlist": [{"coindescs": [{"desc": "가나"}]}]},
            {"name": "나", "coinlist": [{"coindescs": [{"desc": "다라"}]}]},
        ]}]}
        llc = {"dataList": [{"id": "S", "levelList": [
            {"name": "甲", "coinlist": [{"coindescs": [{"desc": "甲乙"}]}]},
        ]}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)
        p._get_translating()

        p._de_get_translating_text({
            "S": {
                ("levelList", 1, "name"): "乙",
                ("levelList", 1, "coinlist", 0, "coindescs", 0, "desc"): "丙丁",
            }
        })
        entry = _entry(p, "S")

        assert len(entry["levelList"]) == 2                       # 新增等级没有被丢弃
        assert entry["levelList"][0]["name"] == "甲"              # 老等级沿用熟肉
        assert entry["levelList"][1]["name"] == "乙"              # 新等级用本次译文
        assert entry["levelList"][1]["coinlist"][0]["coindescs"][0]["desc"] == "丙丁"

    def test_empty_translation_falls_back_to_source(self, tmp_path):
        """非空 KR 字段拿到空译文时回填 KR 原文，避免产出出现空洞。"""
        kr = {"dataList": [{"id": "A", "name": "가"}]}
        llc = {"dataList": []}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)
        p._get_translating()

        p._de_get_translating_text({"A": {("name",): "   "}})
        assert _entry(p, "A")["name"] == "가"

    def test_stale_llc_only_key_is_dropped(self, tmp_path):
        """产出以 KR 结构为准，游戏已下架的 LLC 残留键不再带出。"""
        kr = {"dataList": [{"id": "A", "name": "가"}]}
        llc = {"dataList": [{"id": "A", "name": "甲", "removedKey": "旧值"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)
        p._get_translating()

        p._de_get_translating_text({})
        assert "removedKey" not in _entry(p, "A")


# ============================================================
# 3. 韩文回填检测
# ============================================================

class TestHangulDetection:
    def test_predicate(self):
        assert contains_hangul("리나모비블")
        assert not contains_hangul("莉娜莫维尔")

        # 源含韩文、译文仍含韩文 → 漏翻
        assert is_untranslated_hangul("리나모비블", "리나모비블")
        assert is_untranslated_hangul("莉娜莫비블", "리나모비블")
        # 源含韩文、译文已中文化 → 正常
        assert not is_untranslated_hangul("莉娜莫维尔", "리나모비블")
        # 源不含韩文（占位符/ID）时译文与原文相同属正常
        assert not is_untranslated_hangul("BGM_01", "BGM_01")
        assert not is_untranslated_hangul("…….", "…….")
        # 译文缺失交给 missing 分支处理，不算韩文回填
        assert not is_untranslated_hangul("", "리나모비블")
        # 译文非字符串时按漏翻处理
        assert is_untranslated_hangul(None, "리나모비블")

    def test_korean_passthrough_in_llc_is_not_covered(self, tmp_path):
        """熟肉里是韩文原样回填的字段，下一轮必须重新翻译。"""
        kr = {"dataList": [{"id": "A", "name": "리나모비블", "desc": "가나다"}]}
        llc = {"dataList": [{"id": "A", "name": "리나모비블", "desc": "甲乙丙"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)

        assert p._check_translated() is None
        p._get_translating()
        assert p.translating_list == ["A"]
        assert p.translating_fields["A"] == [("name",)]

    def test_translated_name_is_covered(self, tmp_path):
        """正常中文化的字段不应被反复重译（幂等性）。"""
        kr = {"dataList": [{"id": "A", "name": "리나모비블"}]}
        llc = {"dataList": [{"id": "A", "name": "莉娜莫维尔"}]}
        p = build_processor(tmp_path, kr=kr, llc=llc, jp=kr, en=kr)

        outcome = p._check_translated()
        assert outcome is not None
        assert outcome.result == ProcessResult.ALREADY_TRANSLATED
