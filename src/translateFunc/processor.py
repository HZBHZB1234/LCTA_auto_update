"""
translateFunc/processor.py
FileProcessor —— 对单个文件执行完整的翻译管线处理。
返回 ProcessOutcome，不再抛出 ProcesserExit 异常。
"""
from __future__ import annotations
from copy import deepcopy
import json
import logging
import re
import shutil
import sys
import threading
import time
import traceback
import uuid

_logger = logging.getLogger("LCTA")  # 与 LogManager 一致的 logger，确保日志正确路由

from datetime import datetime
from translateFunc.enums import ProcessResult, FileType
from translateFunc.config import ProcessOutcome, TranslateConfig, FilePathConfig, _suppress_translatekit_log
from translateFunc.matcher.engine import MatcherEngine
from translateFunc.builder.request import RequestBuilder, EMPTY_TEXT, AVOID_PATH
from translateFunc.builder.stages import StageStrategy
from translateFunc.proper import flatten_dict_enhanced, update_dict_with_flattened
from translateFunc.validator import RuleBasedValidator
from translateFunc.recorder import TranslationRecorder
from translateFunc.diagnostics import (
    HttpResponseObserver,
    safe_json_value,
    serialize_exception,
)

EMPTY_DATA = [{"dataList": []}, {}, []]
EMPTY_DATA_LIST = [[], [{}]]
SUCCESS_CALL_STATUSES = {"success", "recovered"}

# 置信度排序：低于 min_confidence 的条目回退，并纳入降级重试
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

# 韩文（Hangul）码位：谚文音节 + 字母 + 兼容字母。
# 用于识别「模型把待翻译的韩文原文原样回填」这种最典型的漏翻。
_RE_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")

# 保护 processing_log.jsonl 的并发写入
_processing_log_lock = threading.Lock()


def _chunk_evenly(items: list, n: int) -> list[list]:
    """把 items 尽量均匀地分成 n 组，保持原有顺序。"""
    if not items:
        return []
    n = max(1, min(int(n), len(items)))
    size, remainder = divmod(len(items), n)
    groups: list[list] = []
    start = 0
    for i in range(n):
        end = start + size + (1 if i < remainder else 0)
        groups.append(list(items[start:end]))
        start = end
    return groups


def contains_hangul(text) -> bool:
    """文本中是否含韩文（Hangul）字符。"""
    return bool(isinstance(text, str) and _RE_HANGUL.search(text))


def is_untranslated_hangul(translation, source) -> bool:
    """译文是否为「源文本含韩文、译文仍含韩文」的未翻译结果。

    只有源(KR)含韩文时才判定：KR 为纯符号/数字/英文 ID（如 ``BGM_01``、``50%``）
    时译文与原文相同属于正常，不构成漏翻。
    """
    if not contains_hangul(source):
        return False
    if not isinstance(translation, str):
        return True
    return contains_hangul(translation)


class FileProcessor:
    """对单个翻译文件进行端到端处理。

    控制流：每个退出路径都返回 ProcessOutcome。
    不使用异常进行正常控制流。
    """

    def __init__(
        self,
        path_config: FilePathConfig,
        engine: MatcherEngine,
        translate_config: TranslateConfig,
        translator,  # translatekit TranslatorBase 实例
        recorder: "TranslationRecorder" = None,
        new_term_collector=None,  # proper.new_terms.NewTermCollector，可为 None
    ):
        self.path_config = path_config
        self._engine = engine
        self._config = translate_config
        self._translator = translator
        self._recorder = recorder
        self._new_term_collector = new_term_collector

        self._api_calls: list[dict] = []
        self._input_text_blocks: list[dict] = []
        self._input_reference: dict = {}
        self._last_failed_call: dict | None = None
        self._http_observer = HttpResponseObserver(translator)

        # 内部状态（在 process() 中填充）
        self.kr_json: dict = {}
        self.en_json: dict = {}
        self.jp_json: dict = {}
        self.llc_json: dict = {}
        self.kr_data: list = []
        self.en_data: list = []
        self.jp_data: list = []
        self.llc_data: list = []
        self.kr_index: dict = {}
        self.en_index: dict = {}
        self.jp_index: dict = {}
        self.llc_index: dict = {}
        self.is_story: bool = False
        self.is_skill: bool = False
        self.translating_list: list = []
        # {条目键: [待翻译字段路径]}，与 translating_list 同步由 _get_translating 填充
        self.translating_fields: dict = {}
        self._base_index: dict = {}

    @property
    def file_name(self) -> str:
        return self.path_config.real_name

    @property
    def file_type(self) -> FileType:
        if self.is_story:
            return FileType.STORY
        if self.is_skill:
            return FileType.SKILL
        # UI 文件的启发式判断
        if "UI" in str(self.path_config.rel_path).upper():
            return FileType.UI
        return FileType.OTHER

    # ========== 主处理流程 ==========

    def process(self) -> ProcessOutcome:
        """执行完整的翻译处理。返回 ProcessOutcome。"""
        start_time = time.perf_counter()
        llm_calls = 0
        text_blocks_count = 0
        format_used = None
        formats_tried: list[str] = []
        outcome = None

        try:
            # 1. 加载 JSON 文件
            outcome = self._load_jsons()
            if outcome:
                self._write_processing_log(outcome, start_time)
                return outcome

            # 2. 检查空文件
            outcome = self._check_empty()
            if outcome:
                self._write_processing_log(outcome, start_time)
                return outcome

            # 3. 初始化基础数据
            self._init_base_data()

            # 4. 构建数据索引
            self._make_data_index()

            # 5. 检查是否已翻译
            try:
                outcome = self._check_translated()
                if outcome:
                    self._write_processing_log(outcome, start_time)
                    return outcome
            except Exception as e:
                _logger.exception(f"[{self.file_name}] _check_translated 异常: {e}")
                self._save_except()
                outcome = ProcessOutcome(
                    ProcessResult.SAVE_ERROR,
                    self.file_name,
                    {"reason": f"_check_translated 失败: {e}", "exception_type": type(e).__name__, "traceback": traceback.format_exc()},
                )
                self._write_processing_log(outcome, start_time)
                return outcome

            # 6. 获取待翻译列表
            self._get_translating()
            if not self.translating_list:
                # KR 每条均已被 LLC 覆盖，视为已翻译；若此处 LLC 文件存在则落盘，
                # 避免出现"判为已翻译却未生成产出文件"的静默丢失。
                if self.path_config.LLC_path.exists():
                    self._save_llc()
                outcome = ProcessOutcome(ProcessResult.ALREADY_TRANSLATED, self.file_name)
                self._write_processing_log(outcome, start_time)
                return outcome

            # 7. 构建请求文本
            request_text = {
                "kr": self._get_translating_text("kr"),
                "jp": self._get_translating_text("jp"),
                "en": self._get_translating_text("en"),
            }

            # 8. 构建并翻译
            try:
                translated_data, had_fallback = self._translate(request_text)
            except ValueError:
                _logger.exception(f"[{self.file_name}] 翻译数量不匹配异常")
                self._save_except()
                outcome = ProcessOutcome(
                    ProcessResult.TRANSLATION_MISMATCH,
                    self.file_name,
                    {"reason": "译文数量与原文不匹配", "traceback": traceback.format_exc()},
                )
                self._write_processing_log(outcome, start_time)
                return outcome
            except Exception as e:
                _logger.exception(f"[{self.file_name}] 翻译处理异常: {e}")
                self._save_except()
                outcome = ProcessOutcome(
                    ProcessResult.SAVE_ERROR,
                    self.file_name,
                    {"reason": str(e), "exception_type": type(e).__name__, "traceback": traceback.format_exc()},
                )
                self._write_processing_log(outcome, start_time)
                return outcome

            # 9. 重建并保存
            self._de_get_translating_text(translated_data)
            result = self._de_get_translating()

            try:
                self._save_result(result)
            except Exception as e:
                _logger.exception(f"[{self.file_name}] 保存结果异常: {e}")
                outcome = ProcessOutcome(
                    ProcessResult.SAVE_ERROR,
                    self.file_name,
                    {"reason": str(e), "exception_type": type(e).__name__, "traceback": traceback.format_exc()},
                )
                self._write_processing_log(outcome, start_time)
                return outcome

            if had_fallback:
                outcome = ProcessOutcome(
                    ProcessResult.FALLBACK_TO_ORIGINAL,
                    self.file_name,
                    {"fallback_parts": "部分文本块回退为 KR 原文"},
                )
            else:
                outcome = ProcessOutcome(ProcessResult.SUCCESS_SAVED, self.file_name)

            self._write_processing_log(outcome, start_time)
            return outcome
        finally:
            if self._recorder is not None:
                active_exception = sys.exc_info()[1]
                try:
                    self._recorder.write_record({
                        "schema_version": 2,
                        "timestamp": datetime.now().isoformat(),
                        "file_name": self.file_name,
                        # file_name 只是 basename，排查漏翻时无法定位；补相对路径
                        "relative_path": self._relative_path(),
                        "text_blocks": self._input_text_blocks,
                        "reference": self._input_reference,
                        "api_calls": self._api_calls,
                        "outcome": outcome.result.name if outcome else "INTERNAL_ERROR",
                        "outcome_extra": outcome.extra if outcome else None,
                        "exception": serialize_exception(active_exception),
                        "call_summary": {
                            "total": len(self._api_calls),
                            "failed": sum(
                                1 for call in self._api_calls
                                if call.get("status") not in SUCCESS_CALL_STATUSES
                            ),
                        },
                        "elapsed_seconds": round(time.perf_counter() - start_time, 3),
                    })
                except Exception:
                    _logger.exception(
                        f"[{self.file_name}] 翻译 dump 写入失败: {self._recorder.file_path}"
                    )

    def _relative_path(self) -> str:
        """KR 文件相对生肉根目录的路径（统一为正斜杠），用于 dump 定位。"""
        try:
            return str(self.path_config.rel_path).replace("\\", "/")
        except Exception:
            return ""

    def _write_processing_log(self, outcome: ProcessOutcome, start_time: float) -> None:
        """将单文件处理结果追加写入 JSONL 日志文件。"""
        try:
            elapsed = time.perf_counter() - start_time
            extra = dict(outcome.extra or {})
            extra["elapsed_seconds"] = round(elapsed, 3)
            if self._last_failed_call is not None:
                extra.setdefault("last_failed_call", self._last_failed_call)
            outcome.extra = extra

            log_entry = {
                "file_name": outcome.file_name,
                "result": outcome.result.name,
                "elapsed_seconds": extra["elapsed_seconds"],
                "extra": safe_json_value(extra),
            }
            log_dir = self.path_config._PathConfig.target_path
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "processing_log.jsonl"
            line = json.dumps(log_entry, ensure_ascii=False) + "\n"
            with _processing_log_lock:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            _logger.exception(f"处理日志写入失败 ({outcome.file_name})，但不影响主流程")

    def _call_ai(
        self,
        *,
        stage: str,
        system_prompt: str,
        user_prompt: str,
        response_format: str,
        timeout: int,
        parser=None,
        parse_error_provider=None,
        prompt_format: str = "",
        part: int | None = None,
        attempt: int | None = None,
        metadata: dict | None = None,
    ) -> tuple[object, object, dict]:
        """执行一次 AI 调用，并完整记录请求、响应、HTTP 尝试和异常链。"""
        started_at = datetime.now()
        started_perf = time.perf_counter()
        record = {
            "call_id": uuid.uuid4().hex[:16],
            "stage": stage,
            "part": part,
            "attempt": attempt,
            "format": prompt_format,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response_format": response_format,
            "timeout": timeout,
            "raw_response": None,
            "parsed_response": None,
            "parse_errors": [],
            "validation_errors": [],
            "http_attempts": [],
            "exception": None,
            "status": "internal_error",
            "failure_kind": None,
            "metadata": metadata or {},
            "started_at": started_at.isoformat(),
        }
        raw_response = None
        parsed_response = None
        caught_exception = None
        self._http_observer.begin()

        try:
            raw_response = self._translator.translate(user_prompt, timeout=timeout)
            record["raw_response"] = str(raw_response)
            parsed_response = parser(raw_response) if parser is not None else raw_response
            record["parsed_response"] = parsed_response
            if parser is not None and not parsed_response:
                record["status"] = "parse_error"
                record["failure_kind"] = "empty_parsed_response"
                parse_errors = (
                    parse_error_provider()
                    if parse_error_provider is not None else []
                )
                record["parse_errors"] = parse_errors or [{
                    "type": "EmptyParseResult",
                    "message": "解析结果为空或响应格式无效",
                }]
            else:
                record["status"] = "success"
            return raw_response, parsed_response, record
        except Exception as exc:
            caught_exception = exc
            record["exception"] = serialize_exception(exc)
            if raw_response is None:
                record["status"] = "api_error"
                record["failure_kind"] = "translator_exception"
            else:
                record["status"] = "parse_error"
                record["failure_kind"] = "parser_exception"
                parse_errors = (
                    parse_error_provider()
                    if parse_error_provider is not None else []
                )
                record["parse_errors"] = parse_errors or [{
                    "type": type(exc).__name__,
                    "message": str(exc),
                }]
            raise
        finally:
            record["http_attempts"] = self._http_observer.finish()
            record["finished_at"] = datetime.now().isoformat()
            record["elapsed_seconds"] = round(time.perf_counter() - started_perf, 3)
            if self._recorder is not None:
                self._api_calls.append(record)
            if record["status"] not in SUCCESS_CALL_STATUSES:
                self._remember_failed_call(record)
                self._log_call_failure(record, caught_exception)

    def _remember_failed_call(self, record: dict) -> None:
        """保存适合 processing_log 的最近失败调用摘要。"""
        http_attempts = record.get("http_attempts") or []
        last_http = http_attempts[-1] if http_attempts else {}
        raw_response = record.get("raw_response") or last_http.get("body") or ""
        self._last_failed_call = safe_json_value({
            "call_id": record.get("call_id"),
            "stage": record.get("stage"),
            "part": record.get("part"),
            "attempt": record.get("attempt"),
            "format": record.get("format"),
            "status": record.get("status"),
            "failure_kind": record.get("failure_kind"),
            "http_status": last_http.get("status_code"),
            "response_excerpt": str(raw_response)[:2000],
            "parse_errors": record.get("parse_errors", []),
            "validation_errors": record.get("validation_errors", []),
            "exception": record.get("exception"),
        })

    def _log_call_failure(self, record: dict, exc: Exception | None = None) -> None:
        """将可读错误摘要写入 app.log；完整内容保存在 dump。"""
        http_attempts = record.get("http_attempts") or []
        last_http = http_attempts[-1] if http_attempts else {}
        response = record.get("raw_response") or last_http.get("body") or ""
        response_excerpt = str(response)[:2000]
        message = (
            f"[{self.file_name}] AI 调用失败 "
            f"call_id={record.get('call_id')} stage={record.get('stage')} "
            f"part={record.get('part')} attempt={record.get('attempt')} "
            f"format={record.get('format')} status={record.get('status')} "
            f"failure={record.get('failure_kind')} "
            f"http_status={last_http.get('status_code')} "
            f"response_excerpt={response_excerpt!r}"
        )
        if exc is not None:
            _logger.error(
                message,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            _logger.warning(message)

    def _mark_call_failure(
        self,
        record: dict,
        *,
        status: str,
        failure_kind: str,
        validation_errors: list | None = None,
        parse_errors: list | None = None,
    ) -> None:
        """在调用成功但后续校验失败时更新诊断状态。"""
        record["status"] = status
        record["failure_kind"] = failure_kind
        if validation_errors is not None:
            record.setdefault("validation_errors", []).extend(validation_errors)
        if parse_errors is not None:
            record.setdefault("parse_errors", []).extend(parse_errors)
        self._remember_failed_call(record)
        self._log_call_failure(record)

    def _mark_call_recovered(
        self,
        record: dict | None,
        *,
        recovery_kind: str,
        recovered_by: dict | None = None,
    ) -> None:
        """将已被后续步骤完整恢复的调用从失败状态改为 recovered。"""
        if not record or record.get("status") in SUCCESS_CALL_STATUSES:
            return

        metadata = record.setdefault("metadata", {})
        metadata.setdefault("recovered_status", record.get("status"))
        metadata.setdefault("recovered_failure_kind", record.get("failure_kind"))
        metadata["recovery_kind"] = recovery_kind
        if recovered_by is not None:
            metadata["recovered_by_call_id"] = recovered_by.get("call_id")
        record["status"] = "recovered"
        record["failure_kind"] = None
        self._refresh_last_failed_call()

    def _refresh_last_failed_call(self) -> None:
        """重新计算最近一个尚未恢复的失败调用。"""
        self._last_failed_call = None
        for call in reversed(self._api_calls):
            if call.get("status") not in SUCCESS_CALL_STATUSES:
                self._remember_failed_call(call)
                return

    def _record_diagnostic_event(
        self,
        *,
        stage: str,
        status: str,
        failure_kind: str | None = None,
        prompt_format: str = "",
        part: int | None = None,
        parsed_response=None,
        validation_errors: list | None = None,
        exc: Exception | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """记录不直接发起 HTTP 请求的管线诊断事件。"""
        now = datetime.now().isoformat()
        record = {
            "call_id": uuid.uuid4().hex[:16],
            "stage": stage,
            "part": part,
            "attempt": None,
            "format": prompt_format,
            "system_prompt": "",
            "user_prompt": "",
            "response_format": "",
            "timeout": 0,
            "raw_response": None,
            "parsed_response": parsed_response,
            "parse_errors": [],
            "validation_errors": validation_errors or [],
            "http_attempts": [],
            "exception": serialize_exception(exc),
            "status": status,
            "failure_kind": failure_kind,
            "metadata": metadata or {},
            "started_at": now,
            "finished_at": now,
            "elapsed_seconds": 0,
        }
        if self._recorder is not None:
            self._api_calls.append(record)
        if status not in SUCCESS_CALL_STATUSES:
            self._remember_failed_call(record)
        return record

    # ========== 翻译执行 ==========

    def _translate(self, request_text: dict) -> tuple[dict, bool]:
        """通过配置的管线阶段执行翻译，支持格式回退。

        Returns:
            (翻译结果字典, had_fallback) — had_fallback=True 表示至少一个
            part 的全部格式失败，已回退为 KR 原文。
        """
        # 构建请求
        builder = RequestBuilder(
            request_text,
            self._engine,
            is_story=self.is_story,
            is_skill=self.is_skill,
            max_length=20000,
            file_type=self.file_type,
        )

        if self._config.is_llm:
            builder.build(prompt_format=self._config.prompt_format)
            stage_strategy = StageStrategy(self._config)

            self._api_calls = []
            self._input_text_blocks = builder.unified_request.get("text_blocks", [])
            self._input_reference = builder.unified_request.get("reference", {})

            # ====== 阶段 0：消歧（仅主格式） ======
            user_format = self._config.prompt_format
            if stage_strategy.needs_disambiguation():
                _logger.debug(f"[{self.file_name}] 阶段 0: 术语消歧 (mode={self._config.disambiguation_mode})")
                ambiguous_terms = self._collect_ambiguous_terms(builder)
                if ambiguous_terms:
                    try:
                        s0_system = stage_strategy.build_stage_0_prompt(prompt_format=user_format)
                        self._update_translator_prompt(s0_system, self._format_to_response_format(user_format))
                        stage_0_parts = stage_strategy.split_stage_0_inputs(
                            ambiguous_terms,
                            builder.unified_request.get("text_blocks", []),
                            prompt_format=user_format,
                            max_length=builder.max_length,
                        )
                        for part_idx, stage_0_part in enumerate(stage_0_parts):
                            s0_call_started = False
                            try:
                                s0_user = stage_strategy.build_stage_0_user_prompt(
                                    stage_0_part["candidate_terms"],
                                    stage_0_part["text_blocks"],
                                    prompt_format=user_format,
                                )
                                s0_call_started = True
                                _, disambiguated, _ = self._call_ai(
                                    stage="stage_0",
                                    system_prompt=s0_system,
                                    user_prompt=s0_user,
                                    response_format=self._format_to_response_format(user_format),
                                    timeout=60,
                                    parser=lambda response: stage_strategy.parse_stage_0_result(
                                        response, prompt_format=user_format,
                                    ),
                                    parse_error_provider=stage_strategy.consume_parse_errors,
                                    prompt_format=user_format,
                                    part=part_idx + 1,
                                    attempt=1,
                                    metadata={
                                        "total_parts": len(stage_0_parts),
                                        "candidate_terms": len(stage_0_part["candidate_terms"]),
                                    },
                                )
                                if disambiguated:
                                    _logger.debug(
                                        f"[{self.file_name}] 阶段 0 消歧 "
                                        f"{part_idx + 1}/{len(stage_0_parts)}："
                                        f"{len(disambiguated)} 个术语被评估"
                                    )
                                    self._apply_disambiguation(builder, disambiguated)
                                else:
                                    _logger.debug(
                                        f"[{self.file_name}] 阶段 0 消歧 "
                                        f"{part_idx + 1}/{len(stage_0_parts)}：解析结果为空"
                                    )
                            except Exception as e:
                                if not s0_call_started:
                                    self._record_diagnostic_event(
                                        stage="stage_0",
                                        status="internal_error",
                                        failure_kind="prompt_or_config_error",
                                        prompt_format=user_format,
                                        part=part_idx + 1,
                                        exc=e,
                                        metadata={"total_parts": len(stage_0_parts)},
                                    )
                                _logger.exception(
                                    f"[{self.file_name}] 阶段 0 消歧 "
                                    f"{part_idx + 1}/{len(stage_0_parts)} 异常 ({e})，跳过该分片"
                                )
                        builder._split_by_length(prompt_format=user_format)
                    except Exception as e:
                        self._record_diagnostic_event(
                            stage="stage_0",
                            status="internal_error",
                            failure_kind="prompt_or_config_error",
                            prompt_format=user_format,
                            exc=e,
                        )
                        _logger.exception(f"[{self.file_name}] 阶段 0 消歧异常 ({e})，使用原始术语表继续")

            # 确定格式回退链
            formats_chain = self._build_format_chain()
            if len(formats_chain) > 1:
                _logger.info(
                    f"[{self.file_name}] 阶段 1: 主翻译 "
                    f"(格式链: {' → '.join(formats_chain)})"
                )
            else:
                _logger.debug(f"[{self.file_name}] 阶段 1: 主翻译 ({formats_chain[0]})")

            result: list[str] = []
            had_fallback = False
            for i, request_part in enumerate(builder.split_requests if builder.split_requests else [builder.unified_request]):
                if builder.split_requests:
                    part_data = request_part
                else:
                    part_data = builder.unified_request
                if part_data is None:
                    continue

                part_result = None
                tried_formats: list[str] = []
                selected_call_record: dict | None = None
                failed_format_calls: list[dict] = []
                # 本 part 内由格式循环判定出的未解决文本块：{索引: 原因}
                cycle_unresolved: dict[int, str] = {}

                for fmt_idx, fmt in enumerate(formats_chain):
                    call_record = None
                    tried_formats.append(fmt)
                    # 按当前格式构建 system prompt
                    system_prompt = stage_strategy.build_stage_1_prompt(
                        self.file_type,
                        prompt_format=fmt,
                    )

                    # 按当前格式构建 user prompt
                    user_prompt = builder.get_request_text(prompt_format=fmt)
                    user_text = user_prompt[i] if i < len(user_prompt) else user_prompt[0]

                    # 自适应超时：基于实际请求长度 + 预期输出长度
                    input_len = len(json.dumps(request_part, ensure_ascii=False))
                    timeout = max(input_len * 3 // 400 + 40, 60)

                    # P0-3: LLM 调用前预检查分片大小，记录详细诊断数据
                    _rendered_len = len(user_text)
                    text_blocks_for_part = part_data.get("text_blocks", [])
                    ref_for_part = part_data.get("reference", {})
                    if _rendered_len > 20000:
                        _logger.warning(
                            f"[{self.file_name}] [{fmt}] 第 {i + 1}/{len(builder.split_requests)} 部分 "
                            f"超限: 渲染长度={_rendered_len} > 限制=20000 | "
                            f"text_blocks={len(text_blocks_for_part)} | "
                            f"proper_terms={len(ref_for_part.get('proper_terms', []))} | "
                            f"affects={len(ref_for_part.get('affects', []))} | "
                            f"models={len(ref_for_part.get('models', []))} | "
                            f"model_docs={len(ref_for_part.get('model_docs', []))} | "
                            f"skill_doc_len={len(ref_for_part.get('skill_doc', ''))}"
                        )

                    # 更新线程本地 translator 的 system_prompt 和 response_format
                    # 放在 try 外：配置更新失败不应被当作解析失败
                    try:
                        self._update_translator_prompt(system_prompt, self._format_to_response_format(fmt))
                    except Exception as exc:
                        self._record_diagnostic_event(
                            stage="stage_1",
                            status="internal_error",
                            failure_kind="translator_config_error",
                            prompt_format=fmt,
                            part=i + 1,
                            exc=exc,
                        )
                        raise

                    # 每次格式回退前清缓存：缓存键只含 user_text hash，不区分
                    # system_prompt / response_format，跨格式复用可能拿到别格式的
                    # 结果。此前只覆盖 xml_json ↔ xml_xml（两者 user_text 相同），
                    # json_json 回退同样存在串味风险，故统一在回退时清空。
                    if fmt_idx > 0:
                        self._translator.clear_cache()

                    try:
                        _, parsed, call_record = self._call_ai(
                            stage="stage_1",
                            system_prompt=system_prompt,
                            user_prompt=user_text,
                            response_format=self._format_to_response_format(fmt),
                            timeout=timeout,
                            parser=lambda response, current_format=fmt: (
                                stage_strategy.parse_stage_1_result(
                                    response, prompt_format=current_format,
                                )
                            ),
                            parse_error_provider=stage_strategy.consume_parse_errors,
                            prompt_format=fmt,
                            part=i + 1,
                            attempt=fmt_idx + 1,
                            metadata={
                                "rendered_length": _rendered_len,
                                "text_blocks": len(text_blocks_for_part),
                            },
                        )

                        if not parsed:
                            raise ValueError(f"{fmt}: 解析结果为空")

                        # 顺带收集模型回传的新专有名词（收集失败不影响主流程）
                        self._harvest_new_terms(stage_strategy, stage="stage_1", part_idx=i)

                        # 按 id 对齐解析结果与文本块（解决 LLM 跳过/重排条目导致的错位）
                        text_blocks = part_data.get("text_blocks", [])
                        expected_count = len(text_blocks)

                        # 构建 id → parsed_item 映射
                        parsed_by_id: dict[int, dict] = {}
                        for t in parsed:
                            if isinstance(t, dict):
                                try:
                                    tid = int(t.get("id", 0))
                                    if tid:
                                        parsed_by_id[tid] = t
                                except (ValueError, TypeError):
                                    continue

                        # 置信度检查准备
                        threshold = _CONFIDENCE_ORDER.get(self._config.min_confidence, 1)
                        low_conf_count = 0
                        low_confidence_ids: list[int] = []
                        missing_ids: list[int] = []

                        # 按 text_block 顺序（1-based id）提取翻译
                        part_result: list[str] = []
                        for idx, block in enumerate(text_blocks):
                            expected_id = idx + 1
                            t = parsed_by_id.get(expected_id)
                            if t is None and idx < len(parsed):
                                # id 未匹配，尝试按顺序回退（LLM 可能未输出 id）
                                fallback_t = parsed[idx]
                                if isinstance(fallback_t, dict):
                                    t = fallback_t

                            if t is not None and isinstance(t, dict):
                                translation = t.get("translation", "")
                                # 置信度检查：低于 min_confidence 的条目回退为 KR 原文
                                conf = str(t.get("confidence", "medium")).lower()
                                if _CONFIDENCE_ORDER.get(conf, 1) < threshold:
                                    reasoning = t.get("reasoning", "")
                                    _logger.warning(
                                        f"[{self.file_name}] [{fmt}] 低置信度条目 #{expected_id}: "
                                        f"confidence={conf}, reasoning={reasoning[:200]}"
                                    )
                                    low_conf_count += 1
                                    low_confidence_ids.append(expected_id)
                                    translation = block.get("kr", "")
                                part_result.append(translation)
                            else:
                                part_result.append(block.get("kr", ""))
                                missing_ids.append(expected_id)

                        # P1-1: 缺失条目时若还有剩余格式则尝试下一格式
                        if missing_ids:
                            if fmt_idx + 1 < len(formats_chain):
                                self._mark_call_failure(
                                    call_record,
                                    status="validation_error",
                                    failure_kind="missing_translation_ids",
                                    validation_errors=[{
                                        "missing_ids": missing_ids,
                                        "expected_count": expected_count,
                                        "action": "try_next_format",
                                    }],
                                )
                                _logger.warning(
                                    f"[{self.file_name}] [{fmt}] {len(missing_ids)} 个文本块缺失翻译 "
                                    f"(id: {missing_ids[:10]}...)，尝试下一格式"
                                )
                                failed_format_calls.append(call_record)
                                continue
                            self._mark_call_failure(
                                call_record,
                                status="fallback",
                                failure_kind="missing_translation_ids",
                                validation_errors=[{
                                    "missing_ids": missing_ids,
                                    "expected_count": expected_count,
                                    "action": "fallback_to_source",
                                }],
                            )
                            _logger.warning(
                                f"[{self.file_name}] [{fmt}] {len(missing_ids)} 个文本块缺失翻译 "
                                f"(id: {missing_ids[:10]}...)，已回退为 KR 原文"
                            )
                        if low_conf_count > 0:
                            self._mark_call_failure(
                                call_record,
                                status="fallback",
                                failure_kind="low_confidence",
                                validation_errors=[{
                                    "count": low_conf_count,
                                    "ids": low_confidence_ids,
                                    "minimum_confidence": self._config.min_confidence,
                                }],
                            )
                            _logger.info(
                                f"[{self.file_name}] [{fmt}] {low_conf_count} 条翻译因低置信度"
                                f" (min={self._config.min_confidence}) 回退为 KR 原文"
                            )

                        for expected_id in missing_ids:
                            cycle_unresolved.setdefault(expected_id - 1, "missing_translation")
                        for expected_id in low_confidence_ids:
                            cycle_unresolved.setdefault(expected_id - 1, "low_confidence")

                        selected_call_record = call_record
                        break  # 翻译完整，退出格式回退循环

                    except (json.JSONDecodeError, ValueError) as e:
                        if call_record is not None:
                            failed_format_calls.append(call_record)
                        _logger.warning(
                            f"[{self.file_name}] [{fmt}] 解析失败 ({e})"
                        )
                        continue

                text_blocks = part_data.get("text_blocks", [])
                if part_result is None:
                    # 全部格式解析失败：不再直接回退 KR，先交给降级阶梯挽救
                    _logger.warning(
                        f"[{self.file_name}] 全部格式 ({', '.join(tried_formats)}) "
                        f"解析失败，第 {i + 1}/{len(builder.split_requests)} 部分进入降级重试"
                    )
                    part_result = [b.get("kr", "") for b in text_blocks]
                    for idx in range(len(text_blocks)):
                        cycle_unresolved.setdefault(idx, "all_formats_failed")

                # 统一未解决集合：格式循环判定的缺失/低置信度 + 韩文原样回填
                unresolved = self._collect_unresolved(
                    part_result, text_blocks, cycle_unresolved,
                )

                # 降级阶梯：L1 切更小块 → L2 精简提示词 → L3 剥离复杂响应规则
                fixed_by_level: dict[int, str] = {}
                if unresolved:
                    fixed_by_level, _ = self._escalate_retry(
                        builder, stage_strategy, part_data, part_result,
                        unresolved, i, user_format,
                    )

                remaining = {
                    idx: reason for idx, reason in unresolved.items()
                    if idx not in fixed_by_level
                }
                remaining_hangul = [
                    idx for idx, reason in remaining.items()
                    if reason == "untranslated_hangul"
                ]
                if remaining_hangul:
                    self._record_diagnostic_event(
                        stage="hangul_check",
                        status="fallback",
                        failure_kind="untranslated_hangul",
                        prompt_format=user_format,
                        part=i + 1,
                        validation_errors=[{
                            "count": len(remaining_hangul),
                            "ids": [idx + 1 for idx in remaining_hangul[:10]],
                            "action": "keep_model_output",
                        }],
                        metadata={
                            "hangul_detected": sum(
                                1 for reason in unresolved.values()
                                if reason == "untranslated_hangul"
                            ),
                            "fixed_by_retry": sum(
                                1 for idx in fixed_by_level
                                if unresolved.get(idx) == "untranslated_hangul"
                            ),
                            "samples": [
                                {
                                    "id": idx + 1,
                                    "source": str(text_blocks[idx].get("kr", ""))[:120],
                                    "translation": str(part_result[idx])[:120],
                                }
                                for idx in remaining_hangul[:5]
                            ],
                        },
                    )
                    _logger.warning(
                        f"[{self.file_name}] {len(remaining_hangul)} 条译文经降级重试后仍含韩文"
                        f"（id: {[idx + 1 for idx in remaining_hangul[:10]]}...），"
                        f"按兜底语义保留模型输出并写入 dump"
                    )

                if remaining:
                    had_fallback = True
                    _logger.warning(
                        f"[{self.file_name}] 第 {i + 1}/{len(builder.split_requests)} 部分"
                        f"仍有 {len(remaining)} 个文本块未被挽救，按既定兜底语义落盘"
                    )
                else:
                    self._mark_call_recovered(
                        selected_call_record,
                        recovery_kind="retry_escalation",
                    )

                for failed_call in failed_format_calls:
                    self._mark_call_recovered(
                        failed_call,
                        recovery_kind="format_fallback",
                        recovered_by=selected_call_record,
                    )

                result.extend(part_result)

            # ====== 富文本转义后处理（全文件类型） ======
            result = self._postprocess_richtext(result)

            # ====== 规则化后处理校验（技能文件专用） ======
            # 校验与修复拆成两步：此处只做「检查 + 确定性自动修复」并暂存待修清单，
            # 真正的发还修复放在阶段 2 之后（_repair_rule_violations）——阶段 2 会
            # 改写译文，若修复跑在它前面，刚修好的 [EffectID] 有概率被它又改回中文名。
            rule_validator: RuleBasedValidator | None = None
            rule_text_blocks: list[dict] = []
            pending_rule_repair: list[dict] = []
            if self.is_skill and self._config.enable_rule_validation:
                _logger.debug(f"[{self.file_name}] 规则化后处理校验")
                try:
                    reference = builder.unified_request.get("reference", {})
                    affects_data = reference.get("affects", [])
                    if affects_data:
                        rule_validator = RuleBasedValidator(affects_data)
                        rule_text_blocks = list(
                            builder.unified_request.get("text_blocks", [])
                        )
                        result, pending_rule_repair = self._run_rule_check(
                            result, rule_validator, rule_text_blocks,
                            prompt_format=user_format, phase="pre_stage_2",
                        )
                except Exception as e:
                    self._record_diagnostic_event(
                        stage="rule_validation",
                        status="internal_error",
                        failure_kind="validator_exception",
                        prompt_format=user_format,
                        exc=e,
                    )
                    _logger.exception(
                        f"[{self.file_name}] 规则化校验异常 ({e})，使用未校验的翻译结果"
                    )

            # ====== 阶段 2：自校验（仅主格式，阶段 1 全部成功时执行） ======
            if stage_strategy.needs_self_check() and not had_fallback:
                _logger.debug(f"[{self.file_name}] 阶段 2: 自校验")
                try:
                    original_blocks = builder.unified_request.get("text_blocks", [])
                    translations_for_check = [
                        {"id": i + 1, "translation": t}
                        for i, t in enumerate(result)
                    ]

                    s2_system = stage_strategy.build_stage_2_prompt(
                        self.file_type,
                        prompt_format=user_format,
                    )
                    self._update_translator_prompt(s2_system, self._format_to_response_format(user_format))
                    stage_2_parts = stage_strategy.split_stage_2_inputs(
                        original_blocks,
                        translations_for_check,
                        prompt_format=user_format,
                        reference=builder.unified_request.get("reference"),
                        max_length=builder.max_length,
                    )
                    for part_idx, stage_2_part in enumerate(stage_2_parts):
                        s2_call_started = False
                        try:
                            s2_user = stage_strategy.build_stage_2_user_prompt(
                                stage_2_part["original_blocks"],
                                stage_2_part["translations"],
                                prompt_format=user_format,
                                reference=stage_2_part["reference"],
                            )
                            s2_call_started = True
                            _, checked, _ = self._call_ai(
                                stage="stage_2",
                                system_prompt=s2_system,
                                user_prompt=s2_user,
                                response_format=self._format_to_response_format(user_format),
                                timeout=120,
                                parser=lambda response: stage_strategy.parse_stage_2_result(
                                    response, prompt_format=user_format,
                                ),
                                parse_error_provider=stage_strategy.consume_parse_errors,
                                prompt_format=user_format,
                                part=part_idx + 1,
                                attempt=1,
                                metadata={
                                    "total_parts": len(stage_2_parts),
                                    "offset": stage_2_part["offset"],
                                    "pair_count": len(stage_2_part["original_blocks"]),
                                },
                            )
                            if checked:
                                offset = stage_2_part["offset"]
                                global_checked = [
                                    {**item, "id": int(item.get("id", 0)) + offset}
                                    for item in checked
                                ]
                                result = self._apply_corrections(result, global_checked)
                            else:
                                _logger.debug(
                                    f"[{self.file_name}] 阶段 2 自校验 "
                                    f"{part_idx + 1}/{len(stage_2_parts)}：解析结果为空"
                                )
                        except Exception as e:
                            if not s2_call_started:
                                self._record_diagnostic_event(
                                    stage="stage_2",
                                    status="internal_error",
                                    failure_kind="prompt_or_config_error",
                                    prompt_format=user_format,
                                    part=part_idx + 1,
                                    exc=e,
                                    metadata={"total_parts": len(stage_2_parts)},
                                )
                            _logger.exception(
                                f"[{self.file_name}] 阶段 2 自校验 "
                                f"{part_idx + 1}/{len(stage_2_parts)} 异常 ({e})，跳过该分片"
                            )
                except Exception as e:
                    self._record_diagnostic_event(
                        stage="stage_2",
                        status="internal_error",
                        failure_kind="prompt_or_config_error",
                        prompt_format=user_format,
                        exc=e,
                    )
                    _logger.exception(
                        f"[{self.file_name}] 阶段 2 自校验异常 ({e})，使用未校验的翻译结果"
                    )

            # ====== 规则违规回流修复（阶段 2 之后，修复结果直接落盘） ======
            # 开关关闭时整块跳过：校验只跑阶段 2 之前那一次，行为与改动前一致。
            if rule_validator is not None and self._config.enable_rule_repair:
                # 阶段 2 改写过译文，按当前 result 重算待修清单（纯本地，无 API 调用）
                result, pending_rule_repair = self._run_rule_check(
                    result, rule_validator, rule_text_blocks,
                    prompt_format=user_format, phase="post_stage_2",
                )
                if pending_rule_repair:
                    result = self._repair_rule_violations(
                        builder, stage_strategy, result, rule_text_blocks,
                        pending_rule_repair, rule_validator, user_format,
                    )

            return builder.deBuild(result), had_fallback
        else:
            # 非 LLM 路径：不存在格式回退
            simple_builder = _SimpleRequestBuilder(request_text)
            simple_builder.build()
            request_texts = simple_builder.get_request_text(from_lang=self._config.from_lang)
            result = self._translator.translate(request_texts)
            return simple_builder.deBuild(result), False

    def _postprocess_richtext(self, result: list) -> list:
        """对所有文件类型执行富文本转义后处理。

        检测译文中被错误编码（HTML 实体 / URL 编码）的游戏富文本标签
        （如 &lt;color=…&gt;、%3Ccolor=…%3E），自动还原为原始尖括号 < >。
        该检查不依赖技能文件，故对所有文件生效。
        """
        try:
            validator = RuleBasedValidator([])
            violations = validator.validate_richtext_escapes(result)
            fixable = [v for v in violations if v.auto_fixable and v.fix_fn]
            if fixable:
                result = RuleBasedValidator.apply_auto_fixes(result, fixable)
                _logger.info(
                    f"[{self.file_name}] 富文本转义后处理修正了 {len(fixable)} 处问题"
                )
                self._record_diagnostic_event(
                    stage="richtext_postprocess",
                    status="success",
                    prompt_format=self._config.prompt_format,
                    validation_errors=[
                        {
                            "rule": v.rule,
                            "severity": v.severity,
                            "message": v.message,
                            "block_id": v.block_id,
                            "auto_fixable": v.auto_fixable,
                        }
                        for v in fixable
                    ],
                )
        except Exception as e:
            _logger.exception(
                f"[{self.file_name}] 富文本转义后处理异常 ({e})，使用未修正的翻译结果"
            )
        return result

    # ========== 规则违规回流修复 ==========

    # 修复用的提示词档位。**用 slim 而非 minimal**：minimal 档会把 SKILL 的
    # P0/P1 规则（禁止 [中文名]、Buff 名后带半角空格）一并丢掉，而那正是本次要修的
    # 两类违规所违反的规则，剥掉等于让模型盲修。slim 只去 P2 风格规则与 few-shot，
    # 保留 P0/P1，请求体量仍然很小（只带违规块）。
    _REPAIR_VERBOSITY = "slim"

    def _run_rule_check(
        self,
        result: list,
        validator: "RuleBasedValidator",
        text_blocks: list[dict],
        *,
        prompt_format: str,
        phase: str,
    ) -> tuple[list, list[dict]]:
        """跑一遍确定性规则校验：应用自动修复并记录诊断事件。

        纯本地计算，不产生 API 调用。阶段 2 前后各跑一次 —— 阶段 2 会改写译文，
        可能修好一些不合规、也可能引入新的，所以修复前必须按**当前** result 重算
        待修清单，沿用阶段 2 之前的旧清单会漏掉新引入的违规。

        Returns:
            ``(修正后的译文列表, 待修清单)``，待修清单只含 auto_fixable=False 的违规。
        """
        report = validator.run_all_checks(text_blocks, result)

        error_count = sum(
            1 for v in report.violations if v.severity == "error"
        )
        warn_count = report.warnings_remaining
        if error_count > 0 or warn_count > 0:
            _logger.info(
                f"[{self.file_name}] 规则校验({phase}): {error_count} 个错误, "
                f"{warn_count} 个警告"
            )

        # 应用自动修正（确定性规则，无需模型参与）
        if report.auto_fixes_applied > 0:
            result = validator.apply_auto_fixes(result, report.violations)
            _logger.info(
                f"[{self.file_name}] 规则校验({phase})自动修正了 "
                f"{report.auto_fixes_applied} 处问题"
            )

        violations = [
            {
                "rule": v.rule,
                "severity": v.severity,
                "message": v.message,
                "block_id": v.block_id,
                "auto_fixable": v.auto_fixable,
            }
            for v in report.violations
        ]
        pending = [v for v in violations if not v["auto_fixable"]]
        for v in pending:
            _logger.warning(
                f"[{self.file_name}] [规则校验警告] {v['rule']}: {v['message']} "
                f"(block #{v['block_id']})"
            )

        self._record_diagnostic_event(
            stage="rule_validation",
            status="validation_error" if pending else "success",
            failure_kind="rule_validation" if pending else None,
            prompt_format=prompt_format,
            parsed_response=violations,
            validation_errors=pending,
            metadata={
                "phase": phase,
                "auto_fixes_applied": report.auto_fixes_applied,
                "warnings_remaining": report.warnings_remaining,
                "repairable": len(pending),
                "repair_enabled": bool(self._config.enable_rule_repair),
            },
        )
        return result, pending

    def _repair_rule_violations(
        self,
        builder: "RequestBuilder",
        stage_strategy: "StageStrategy",
        result: list,
        text_blocks: list[dict],
        pending: list[dict],
        validator: "RuleBasedValidator",
        prompt_format: str,
    ) -> list:
        """对规则校验判定不合规的条目构造最小请求发还，尝试修复。

        只处理 ``auto_fixable=False`` 的违规（effect_ref / 未知 [中文名]）——
        确定性可修的违规在校验阶段已由 ``apply_auto_fixes`` 修好，没必要花调用。

        请求被压到最小：只带违规块、slim reference（只留块直接引用的 proper_terms
        与 affects，砍掉 models / model_docs / skill_doc）+ slim 提示词，并在 user
        prompt 末尾追加 ``<violation_report>`` 说明每块错在哪、该怎么改。

        回填前**必须复验**：对候选译文重跑同一校验器的对应规则，违规真的消失才覆盖
        ``result``；否则保留原译文。``_apply_retry_payload`` 只看非空/非韩文/置信度，
        不看规则是否修好，单靠它会把「换了个错法」当成救回来了。

        任何异常都只记录诊断事件并保留原译文，绝不阻断发布。

        Returns:
            修正后的译文列表（原地修改 ``result`` 并返回同一对象）。
        """
        if not self._config.enable_rule_repair:
            return result

        budget = max(0, int(self._config.rule_repair_max_calls))
        if budget <= 0:
            return result

        # 按块归并：{全局块索引(0-based): [违规, ...]}
        # 上界同时受 result 约束：极端情况下 result 可能短于 text_blocks
        # （某个 part 的 part_data 为 None 被跳过），越界索引一律丢弃。
        limit = min(len(text_blocks), len(result))
        by_block: dict[int, list[dict]] = {}
        for v in pending:
            try:
                block_id = int(v.get("block_id", 0))
            except (ValueError, TypeError):
                continue
            if 1 <= block_id <= limit:
                by_block.setdefault(block_id - 1, []).append(v)
        if not by_block:
            return result

        targets = sorted(by_block)
        chunk = max(1, int(self._config.retry_chunk_size))
        groups = _chunk_evenly(
            targets, max(1, (len(targets) + chunk - 1) // chunk),
        )

        _logger.info(
            f"[{self.file_name}] 规则违规回流修复: {len(targets)} 个不合规块 "
            f"(预算 {budget} 次调用) | 规则: {self._summarize_rules(pending)}"
        )

        attempts: list[dict] = []
        fixed: list[int] = []
        used = 0
        for group_idx, group in enumerate(groups):
            if used >= budget:
                break
            used += 1
            repaired, record = self._repair_group(
                builder, stage_strategy, result, text_blocks, group,
                by_block=by_block, validator=validator,
                prompt_format=prompt_format,
                group_idx=group_idx, group_count=len(groups),
            )
            attempts.append(record)
            for idx in repaired:
                if idx not in fixed:
                    fixed.append(idx)

        remaining = [idx for idx in targets if idx not in fixed]
        self._record_diagnostic_event(
            stage="rule_repair",
            status="success" if not remaining else "partial",
            failure_kind=None if not remaining else "rule_repair_incomplete",
            prompt_format=prompt_format,
            validation_errors=[
                {
                    "block_id": idx + 1,
                    "rules": sorted({
                        str(v.get("rule", "")) for v in by_block[idx]
                    }),
                    "action": "keep_original_translation",
                }
                for idx in remaining
            ],
            metadata={
                "repairable": len(targets),
                "repaired": len(fixed),
                "budget": budget,
                "used": used,
                "by_rule": self._summarize_rules(pending),
                "attempts": attempts,
            },
        )
        if remaining:
            _logger.warning(
                f"[{self.file_name}] 规则违规回流修复未全部修好: "
                f"已修 {len(fixed)}/{len(targets)}，剩余 "
                f"{[idx + 1 for idx in remaining][:10]}... 保留原译文"
            )
        else:
            _logger.info(
                f"[{self.file_name}] 规则违规回流修复成功: {len(fixed)} 个块"
                f"已修正并复验通过（{used} 次调用）"
            )
        return result

    def _repair_group(
        self,
        builder: "RequestBuilder",
        stage_strategy: "StageStrategy",
        result: list,
        text_blocks: list[dict],
        group: list[int],
        *,
        by_block: dict[int, list[dict]],
        validator: "RuleBasedValidator",
        prompt_format: str,
        group_idx: int,
        group_count: int,
    ) -> tuple[list[int], dict]:
        """对一组违规块发一次修复请求，返回 ``(被修好的索引, 尝试记录)``。"""
        blocks = [text_blocks[idx] for idx in group]
        # 最小请求：slim reference + 单独保留 affects（effect_ref 需要 id→中文名映射）
        request = self._build_retry_request(
            builder, blocks, slim=True, include_affects=True,
        )
        system_prompt = self._build_retry_prompt(
            stage_strategy, verbosity=self._REPAIR_VERBOSITY,
            prompt_format=prompt_format,
        )
        hint = self._build_repair_hint(group, by_block)
        user_text = self._render_retry_prompt(
            builder, request, prompt_format, repair_hint=hint,
        )

        record: dict = {
            "group": group_idx + 1,
            "group_count": group_count,
            "ids": [idx + 1 for idx in group],
            "rules": sorted({
                str(v.get("rule", ""))
                for idx in group for v in by_block.get(idx, [])
            }),
            "text_blocks": len(blocks),
            "rendered_length": len(user_text),
            "status": "pending",
            "fixed": [],
            "error": None,
        }

        started = False
        try:
            self._update_translator_prompt(
                system_prompt, self._format_to_response_format(prompt_format),
            )
            # 与降级重试同理：缓存键只含 user_text hash，不清空会命中旧结果
            clear_cache = getattr(self._translator, "clear_cache", None)
            if callable(clear_cache):
                clear_cache()
            timeout = max(len(user_text) * 3 // 400 + 40, 60)
            started = True
            _, parsed, call_record = self._call_ai(
                stage="rule_repair",
                system_prompt=system_prompt,
                user_prompt=user_text,
                response_format=self._format_to_response_format(prompt_format),
                timeout=timeout,
                parser=lambda response: stage_strategy.parse_stage_1_result(
                    response, prompt_format=prompt_format,
                ),
                parse_error_provider=stage_strategy.consume_parse_errors,
                prompt_format=prompt_format,
                part=group_idx + 1,
                attempt=1,
                metadata={
                    "rule_repair_group": group_idx + 1,
                    "rule_repair_group_count": group_count,
                    "target_ids": [idx + 1 for idx in group],
                    "rules": record["rules"],
                    "rendered_length": len(user_text),
                },
            )

            self._harvest_new_terms(
                stage_strategy, stage="rule_repair", part_idx=group_idx,
            )

            if not parsed:
                record.update({"status": "parse_error", "error": "解析结果为空"})
                return [], record

            repaired = self._apply_repair_payload(
                parsed, group, text_blocks, result, by_block, validator,
            )
            record.update({
                "status": "ok" if len(repaired) == len(group) else (
                    "partial" if repaired else "no_usable_entry"
                ),
                "fixed": [idx + 1 for idx in repaired],
                "call_id": call_record.get("call_id"),
            })
            if repaired and len(repaired) == len(group):
                self._mark_call_recovered(call_record, recovery_kind="rule_repair")
            return repaired, record
        except Exception as exc:  # noqa: BLE001 - 单组失败不应中断修复
            record.update({"status": "exception", "error": str(exc)[:300]})
            if not started:
                self._record_diagnostic_event(
                    stage="rule_repair",
                    status="internal_error",
                    failure_kind="prompt_or_config_error",
                    prompt_format=prompt_format,
                    part=group_idx + 1,
                    exc=exc,
                    metadata={"target_ids": [idx + 1 for idx in group]},
                )
            _logger.warning(
                f"[{self.file_name}] 规则违规回流修复 第 "
                f"{group_idx + 1}/{group_count} 组异常 ({exc})，保留原译文"
            )
            return [], record

    def _apply_repair_payload(
        self,
        parsed: list,
        group: list[int],
        text_blocks: list[dict],
        result: list,
        by_block: dict[int, list[dict]],
        validator: "RuleBasedValidator",
    ) -> list[int]:
        """把修复响应回填到 ``result``，返回真正修好的索引。

        采纳条件（缺一不可）：
        1. 译文非空；
        2. 不是韩文原样回填；
        3. 置信度达标（模型未给 confidence 时按 medium 计）；
        4. **复验通过** —— 原先不合规的规则在该块上不再报违规。
        """
        by_id: dict[int, dict] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                tid = int(item.get("id", 0))
            except (ValueError, TypeError):
                continue
            if tid and tid not in by_id:
                by_id[tid] = item
        if not by_id:
            # 模型未输出 id 时按顺序兜底
            by_id = {
                i + 1: item for i, item in enumerate(parsed) if isinstance(item, dict)
            }

        threshold = _CONFIDENCE_ORDER.get(self._config.min_confidence, 1)
        repaired: list[int] = []
        for local_idx, src_idx in enumerate(group):
            entry = by_id.get(local_idx + 1)
            if entry is None:
                continue
            translation = entry.get("translation", "") or ""
            if not isinstance(translation, str) or not translation.strip():
                continue
            source = text_blocks[src_idx].get("kr", "") if src_idx < len(text_blocks) else ""
            if is_untranslated_hangul(translation, source):
                continue
            confidence = str(entry.get("confidence", "medium")).lower()
            if _CONFIDENCE_ORDER.get(confidence, 1) < threshold:
                continue
            rules = {str(v.get("rule", "")) for v in by_block.get(src_idx, [])}
            accepted = self._revalidate_candidate(
                validator, text_blocks[src_idx], translation, rules,
            )
            if accepted is None:
                continue
            result[src_idx] = accepted
            repaired.append(src_idx)
        return repaired

    def _revalidate_candidate(
        self,
        validator: "RuleBasedValidator",
        block: dict,
        candidate: str,
        rules: set[str],
    ) -> str | None:
        """复验候选译文：原先不合规的规则不再报违规才采纳。

        先把候选过一遍确定性自动修复（Buff 名空格 / 已知 [中文名] / 富文本转义），
        再检查 ``rules`` 里的规则是否仍报违规——模型修对主要问题的同时引入一个
        确定性可修的小毛病，不该因此被整体否掉。

        Returns:
            可采纳的最终文本；仍不合规或复验自身异常时返回 None（一律不采纳）。
        """
        try:
            text = candidate
            checks = validator.run_all_checks([block], [text])
            fixable = [
                v for v in list(checks.violations)
                + list(validator.validate_richtext_escapes([text]))
                if v.auto_fixable and v.fix_fn
            ]
            if fixable:
                text = RuleBasedValidator.apply_auto_fixes([text], fixable)[0]
            remaining = [
                v for v in validator.run_all_checks([block], [text]).violations
                if v.rule in rules
            ]
            if remaining:
                return None
            return text
        except Exception as exc:  # noqa: BLE001 - 复验异常一律不采纳
            _logger.debug(
                f"[{self.file_name}] 规则修复复验异常 ({exc})，不采纳该候选译文"
            )
            return None

    def _build_repair_hint(
        self, group: list[int], by_block: dict[int, list[dict]],
    ) -> str:
        """构造 ``<violation_report>`` 段：逐块说明违规原因与修正要求。

        块 id 用**组内序号**（1-based），与本次子请求里 text_blocks 的编号一致
        ——子请求渲染时会重新从 1 编号，用全局 id 会指错块。
        """
        lines = [
            "<violation_report>",
            "以下条目的译文经规则校验判定不合规，请只修正这些条目的译文，"
            "其余条目的译文保持不变：",
        ]
        for local_idx, src_idx in enumerate(group):
            for v in by_block.get(src_idx, []):
                lines.append(
                    f"block {local_idx + 1}: [{v.get('rule', '')}] "
                    f"{v.get('message', '')}"
                )
        lines.append("修正要求：")
        lines.append(
            "- 方括号 [] 内只能是英文引擎标识符（如 [Combustion]、"
            "[OnSucceedAttack]），严禁输出 [中文名] 形式。"
        )
        lines.append(
            "- 正文中的状态效果使用术语表给出的中文名称，名称后跟一个半角空格"
            "（如「燃烧 」），不要保留状态效果的英文 ID。"
        )
        lines.append("</violation_report>")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _summarize_rules(pending: list[dict]) -> str:
        """把待修违规按规则名汇总成 ``rule=count``，便于日志与 dump 统计。"""
        counts: dict[str, int] = {}
        for v in pending:
            rule = str(v.get("rule", "unknown"))
            counts[rule] = counts.get(rule, 0) + 1
        return ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))

    # ========== 失败降级阶梯 ==========

    # 级别定义：(标记, 提示词档位, 是否精简 reference)
    _RETRY_LEVELS: tuple[tuple[str, str, bool], ...] = (
        ("L1", "full", False),      # 切更小块
        ("L2", "slim", False),      # 精简提示词
        ("L3", "minimal", True),    # 剥离复杂响应规则 + 最小 reference
    )

    def _retry_missing_entries(
        self,
        builder: "RequestBuilder",
        stage_strategy: "StageStrategy",
        part_data: dict,
        part_result: list[str],
        kr_fallback_indices: list[int],
        tried_formats: list[str],
        part_idx: int,
    ) -> int:
        """对指定索引发起补充翻译，返回成功修复的条目数。

        保留旧签名（历史调用方与诊断用例依赖），内部走完整阶梯的前几级；
        需要自定义未解决原因时直接用 ``_escalate_retry``。
        """
        if not kr_fallback_indices:
            return 0
        primary_format = tried_formats[0] if tried_formats else self._config.prompt_format
        unresolved = {idx: "missing_translation" for idx in kr_fallback_indices}
        fixed, _ = self._escalate_retry(
            builder, stage_strategy, part_data, part_result,
            unresolved, part_idx, primary_format,
        )
        return len(fixed)

    def _collect_unresolved(
        self,
        part_result: list,
        text_blocks: list[dict],
        seeded: dict[int, str] | None = None,
    ) -> dict[int, str]:
        """汇总一个 part 内所有未解决的文本块：``{索引: 原因}``。

        合并三类来源：格式循环已判定出的缺失/低置信度，以及此处新增的
        「源含韩文而译文仍含韩文」漏翻检测。只有 KR 非空的块才会被判为未解决
        ——KR 本来就是空/纯符号的块，译文为空属正常。
        """
        unresolved = dict(seeded or {})
        for idx, block in enumerate(text_blocks):
            if idx in unresolved:
                continue
            translation = part_result[idx] if idx < len(part_result) else ""
            if is_untranslated_hangul(translation, block.get("kr", "")):
                unresolved[idx] = "untranslated_hangul"
            elif not isinstance(translation, str) or not translation.strip():
                kr = block.get("kr", "")
                if isinstance(kr, str) and kr.strip():
                    unresolved[idx] = "empty_translation"
        return unresolved

    def _escalate_retry(
        self,
        builder: "RequestBuilder",
        stage_strategy: "StageStrategy",
        part_data: dict,
        part_result: list,
        unresolved: dict[int, str],
        part_idx: int,
        primary_format: str,
    ) -> tuple[dict[int, str], list[dict]]:
        """对未解决的文本块执行降级阶梯挽救。

        L1 切更小块 → L2 精简提示词 → L3 剥离复杂响应规则。每一级只处理上一级
        仍未解决的索引，调用次数受 ``retry_max_calls_per_part`` 硬预算约束。
        预算耗尽或级别穷尽后，未修复的块交由调用方按既定兜底语义处理
        （缺失回退 KR、韩文回填保留模型输出）。

        Returns:
            ``({索引: 修复来源级别}, 每级尝试记录)``
        """
        fixed: dict[int, str] = {}
        attempts: list[dict] = []

        if not self._config.enable_retry_escalation or self._config.retry_max_level <= 0:
            return fixed, attempts

        text_blocks = part_data.get("text_blocks", [])
        targets = [idx for idx in sorted(unresolved) if 0 <= idx < len(text_blocks)]
        if not targets:
            return fixed, attempts

        budget = max(0, int(self._config.retry_max_calls_per_part))
        max_level = max(0, min(int(self._config.retry_max_level), len(self._RETRY_LEVELS)))
        if budget <= 0 or max_level <= 0:
            return fixed, attempts

        used = 0

        _logger.info(
            f"[{self.file_name}] 降级重试: 第 {part_idx + 1} 部分 "
            f"{len(targets)} 个未解决块 (预算 {budget} 次调用) | "
            f"原因: {self._summarize_reasons(unresolved, targets)}"
        )

        for level in range(1, max_level + 1):
            if not targets or used >= budget:
                break
            level_name, verbosity, slim_ref = self._RETRY_LEVELS[level - 1]
            groups = self._plan_retry_groups(targets, level, budget - used)
            for group_idx, group in enumerate(groups):
                if used >= budget:
                    break
                used += 1
                recovered, call_record = self._retry_group(
                    builder, stage_strategy, part_result, text_blocks, group,
                    level=level, level_name=level_name, verbosity=verbosity,
                    slim_ref=slim_ref, prompt_format=primary_format,
                    part_idx=part_idx, group_idx=group_idx,
                    group_count=len(groups), attempts=attempts,
                )
                for idx in recovered:
                    fixed.setdefault(idx, level_name)
                if len(recovered) == len(group):
                    # 本组全部修复：该次调用若出于失败状态，改判为已恢复
                    self._mark_call_recovered(
                        call_record, recovery_kind=f"retry_{level_name.lower()}",
                    )
            targets = [idx for idx in targets if idx not in fixed]
            if not targets:
                break

        if targets:
            self._record_diagnostic_event(
                stage="retry_exhausted",
                status="fallback",
                failure_kind="retry_escalation_exhausted",
                prompt_format=primary_format,
                part=part_idx + 1,
                validation_errors=[{
                    "unresolved_ids": [idx + 1 for idx in targets],
                    "reasons": {str(idx + 1): unresolved.get(idx, "") for idx in targets},
                    "action": "fallback_to_source",
                }],
                metadata={"attempts": attempts, "budget": budget, "used": used},
            )
            _logger.warning(
                f"[{self.file_name}] 降级重试已穷尽 ({used}/{budget} 次调用)，"
                f"仍有 {len(targets)} 个文本块未修复 "
                f"(id: {[idx + 1 for idx in targets][:10]}...)，转入既定兜底语义"
            )
        else:
            by_level: dict[str, int] = {}
            for level_name in fixed.values():
                by_level[level_name] = by_level.get(level_name, 0) + 1
            self._record_diagnostic_event(
                stage="retry_summary",
                status="success",
                prompt_format=primary_format,
                part=part_idx + 1,
                metadata={
                    "rescued": len(fixed),
                    "by_level": by_level,
                    "calls_used": used,
                    "attempts": attempts,
                },
            )
            _logger.info(
                f"[{self.file_name}] 降级重试成功: 挽救 {len(fixed)} 个文本块 "
                f"({', '.join(f'{k}={v}' for k, v in sorted(by_level.items()))}，"
                f"共 {used} 次调用)"
            )

        return fixed, attempts

    @staticmethod
    def _summarize_reasons(unresolved: dict[int, str], targets: list[int]) -> str:
        """把未解决原因汇总成 ``reason=count`` 形式，便于日志与 dump 统计。"""
        counts: dict[str, int] = {}
        for idx in targets:
            reason = unresolved.get(idx, "unknown")
            counts[reason] = counts.get(reason, 0) + 1
        return ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))

    def _plan_retry_groups(
        self, targets: list[int], level: int, budget_left: int,
    ) -> list[list[int]]:
        """按级别与剩余预算规划本次重试的分组。

        L1 是「切更小块」，尽量按 ``retry_chunk_size`` 切分，但会为后续级别各留
        至少一次调用机会——预算花光却只试过一种手段是最坏的结果。L2/L3 优先保证
        「换提示词档位」这一变量确实被验证过，因此切分后直接受剩余预算约束。
        """
        if not targets:
            return []
        budget_left = max(1, budget_left)
        chunk = max(1, int(self._config.retry_chunk_size))
        wanted = max(1, (len(targets) + chunk - 1) // chunk)
        if level == 1:
            wanted = min(wanted, max(1, budget_left - 2))
        else:
            wanted = min(wanted, budget_left)
        return _chunk_evenly(targets, wanted)

    def _retry_group(
        self,
        builder: "RequestBuilder",
        stage_strategy: "StageStrategy",
        part_result: list,
        text_blocks: list[dict],
        group: list[int],
        *,
        level: int,
        level_name: str,
        verbosity: str,
        slim_ref: bool,
        prompt_format: str,
        part_idx: int,
        group_idx: int,
        group_count: int,
        attempts: list[dict],
    ) -> tuple[list[int], dict | None]:
        """按指定档位重试一组文本块，返回 ``(被修复的索引, 调用记录)``。"""
        stage = f"retry_{level_name}"
        blocks = [text_blocks[idx] for idx in group]
        request = self._build_retry_request(builder, blocks, slim=slim_ref)
        system_prompt = self._build_retry_prompt(
            stage_strategy, verbosity=verbosity, prompt_format=prompt_format,
        )
        user_text = self._render_retry_prompt(builder, request, prompt_format)

        attempt_record = {
            "level": level_name,
            "group": group_idx + 1,
            "group_count": group_count,
            "ids": [idx + 1 for idx in group],
            "verbosity": verbosity,
            "slim_reference": slim_ref,
            "format": prompt_format,
            "status": "pending",
            "fixed": 0,
            "error": None,
        }
        attempts.append(attempt_record)

        started = False
        try:
            self._update_translator_prompt(
                system_prompt, self._format_to_response_format(prompt_format),
            )
            # 每次降级重试前清缓存：缓存键只含 user_text 的 hash，不清空会直接
            # 命中上一轮的同一份坏结果，阶梯等于白跑。
            clear_cache = getattr(self._translator, "clear_cache", None)
            if callable(clear_cache):
                clear_cache()
            timeout = max(len(user_text) * 3 // 400 + 40, 60)
            started = True
            _, parsed, call_record = self._call_ai(
                stage=stage,
                system_prompt=system_prompt,
                user_prompt=user_text,
                response_format=self._format_to_response_format(prompt_format),
                timeout=timeout,
                parser=lambda response: stage_strategy.parse_stage_1_result(
                    response, prompt_format=prompt_format,
                ),
                parse_error_provider=stage_strategy.consume_parse_errors,
                prompt_format=prompt_format,
                part=part_idx + 1,
                attempt=level,
                metadata={
                    "retry_level": level_name,
                    "retry_verbosity": verbosity,
                    "retry_group": group_idx + 1,
                    "retry_group_count": group_count,
                    "target_ids": [idx + 1 for idx in group],
                    "rendered_length": len(user_text),
                },
            )

            self._harvest_new_terms(stage_strategy, stage=stage, part_idx=part_idx)

            if not parsed:
                attempt_record.update({"status": "parse_error", "error": "解析结果为空"})
                return [], call_record

            recovered = self._apply_retry_payload(
                parsed, group, text_blocks, part_result,
            )
            attempt_record.update({
                "status": "ok" if recovered else "no_usable_entry",
                "fixed": len(recovered),
                "call_id": call_record.get("call_id"),
            })
            return recovered, call_record
        except Exception as exc:  # noqa: BLE001 - 单组失败不应中断阶梯
            attempt_record.update({"status": "exception", "error": str(exc)[:300]})
            if not started:
                self._record_diagnostic_event(
                    stage=stage,
                    status="internal_error",
                    failure_kind="prompt_or_config_error",
                    prompt_format=prompt_format,
                    part=part_idx + 1,
                    exc=exc,
                    metadata={
                        "retry_level": level_name,
                        "target_ids": [idx + 1 for idx in group],
                    },
                )
            _logger.warning(
                f"[{self.file_name}] 降级重试 {level_name} "
                f"第 {group_idx + 1}/{group_count} 组异常 ({exc})"
            )
            return [], None

    def _apply_retry_payload(
        self,
        parsed: list,
        group: list[int],
        text_blocks: list[dict],
        part_result: list,
    ) -> list[int]:
        """把重试响应按组内序号回填到 ``part_result``，返回被修复的索引。

        只有译文非空、不是韩文原样回填、且置信度达标才算修复——否则会把同一份
        坏结果当成"救回来了"写进产出。
        """
        by_id: dict[int, dict] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                tid = int(item.get("id", 0))
            except (ValueError, TypeError):
                continue
            if tid and tid not in by_id:
                by_id[tid] = item
        if not by_id:
            # 模型未输出 id 时按顺序兜底
            by_id = {
                i + 1: item for i, item in enumerate(parsed) if isinstance(item, dict)
            }

        threshold = _CONFIDENCE_ORDER.get(self._config.min_confidence, 1)
        recovered: list[int] = []
        for local_idx, src_idx in enumerate(group):
            entry = by_id.get(local_idx + 1)
            if entry is None:
                continue
            translation = entry.get("translation", "") or ""
            if not isinstance(translation, str) or not translation.strip():
                continue
            source = text_blocks[src_idx].get("kr", "") if src_idx < len(text_blocks) else ""
            if is_untranslated_hangul(translation, source):
                continue
            confidence = str(entry.get("confidence", "medium")).lower()
            if _CONFIDENCE_ORDER.get(confidence, 1) < threshold:
                continue
            part_result[src_idx] = translation
            recovered.append(src_idx)
        return recovered

    # ----- 重试路径的接口兼容层 -----

    @staticmethod
    def _build_retry_request(
        builder, blocks: list[dict], *, slim: bool,
        include_affects: bool | None = None,
    ) -> dict:
        """构造重试子请求，兼容只实现旧接口的 builder。

        ``include_affects`` 仅在规则违规修复时显式传入（None = 沿用 ``slim`` 语义）；
        旧 builder 不接受该参数时回退到旧调用，行为与改动前一致。
        """
        build_part = getattr(builder, "build_part_request", None)
        if callable(build_part):
            if include_affects is not None:
                try:
                    return build_part(
                        blocks, slim=slim, include_affects=include_affects,
                    )
                except TypeError:
                    pass
            return build_part(blocks, slim=slim)
        return {"metadata": {}, "reference": {}, "text_blocks": list(blocks)}

    def _build_retry_prompt(
        self, stage_strategy, *, verbosity: str, prompt_format: str,
    ) -> str:
        """构建重试用的 system prompt，兼容不接受 verbosity 的策略实现。"""
        try:
            return stage_strategy.build_stage_1_prompt(
                self.file_type, prompt_format=prompt_format, verbosity=verbosity,
            )
        except TypeError:
            return stage_strategy.build_stage_1_prompt(
                self.file_type, prompt_format=prompt_format,
            )

    @staticmethod
    def _render_retry_prompt(
        builder, request: dict, prompt_format: str, repair_hint: str = "",
    ) -> str:
        """渲染重试请求文本，兼容只提供公开 get_request_text 的 builder。

        ``repair_hint`` 非空时追加到渲染结果末尾（规则违规修复用）。缺省为空串，
        渲染结果与改动前逐字节一致。
        """
        render = getattr(builder, "_get_request_text", None)
        if callable(render):
            text = render(request, prompt_format)
        else:
            texts = builder.get_request_text(prompt_format) or []
            text = texts[0] if texts else ""
        if repair_hint:
            text = f"{text}\n{repair_hint}" if text else repair_hint
        return text

    # ========== 新专有名词收集 ==========

    def _harvest_new_terms(
        self, stage_strategy: "StageStrategy", *, stage: str, part_idx: int,
    ) -> None:
        """收集模型回传的新专有名词，必要时热更新术语表。

        这是纯粹的附加产出：任何环节出错都只记 debug 日志，绝不影响翻译主流程。
        """
        collector = getattr(self, "_new_term_collector", None)
        if collector is None:
            return
        try:
            items = stage_strategy.consume_new_terms()
            if not items:
                return
            engine = getattr(self, "_engine", None)
            known = set(engine.proper_terms) if engine is not None else set()
            accepted = collector.add(items, source=self.file_name, known_terms=known)
            if accepted:
                _logger.debug(
                    f"[{self.file_name}] 收集到 {accepted} 条新专有名词候选 "
                    f"(stage={stage}, part={part_idx + 1})"
                )
            self._maybe_hot_update_terms()
        except Exception as exc:  # noqa: BLE001 - 附加产出不影响主流程
            _logger.debug(f"[{self.file_name}] 新专有名词收集异常 ({exc})")

    def _maybe_hot_update_terms(self) -> None:
        """把累积到阈值的新词并入匹配引擎，供本轮后续文件使用。"""
        collector = getattr(self, "_new_term_collector", None)
        engine = getattr(self, "_engine", None)
        if collector is None or engine is None:
            return
        if not self._config.new_terms_hot_update:
            return
        batch = collector.take_hot_update_batch(
            max(1, int(self._config.new_terms_hot_update_batch))
        )
        if not batch:
            return
        added = engine.add_proper_terms(batch)
        if added:
            _logger.info(
                f"[{self.file_name}] 术语表热更新: 并入 {added} 条新专有名词"
                f"（本轮后续文件即可命中）"
            )

    def _build_format_chain(self) -> list[str]:
        """构建格式回退链：[用户选择] + fallback? [xml_json, json_json, xml_xml] : [].

        用户选择的格式排在最前，回退格式按 xml_json → json_json → xml_xml
        顺序追加（跳过重复）。当 fallback=False 时仅返回用户格式。
        """
        user_format = self._config.prompt_format
        chain = [user_format]
        if self._config.fallback:
            fallback_order = ["xml_json", "json_json", "xml_xml"]
            for f in fallback_order:
                if f not in chain:
                    chain.append(f)
        return chain

    @staticmethod
    def _format_to_response_format(prompt_format: str) -> str:
        """prompt_format → response_format 映射。"""
        return "text" if prompt_format == "xml_xml" else "json_object"

    def _update_translator_prompt(self, system_prompt: str, response_format: str):
        """更新线程本地 translator 的 system_prompt 和 response_format，抑制日志。"""
        with _suppress_translatekit_log(self._config.debug_mode):
            self._translator.update_config(
                system_prompt=system_prompt,
                response_format=response_format,
            )

    # ========== 阶段 0：消歧 ==========

    def _collect_ambiguous_terms(
        self, builder: "RequestBuilder"
    ) -> list[dict]:
        """收集需要 LLM 消歧的术语-文本块关联。

        遍历 unified_request["text_blocks"]，收集其中 proper_refs 引用的术语。
        disambiguation_mode="llm" 时全部匹配参与消歧；
        disambiguation_mode="hybrid" 时也收集全部（confidence 过滤依赖 ProperAnalyzer 集成）。

        Returns:
            [{term, cn, note, text_block_indices: [int, ...]}, ...]
        """
        text_blocks = builder.unified_request.get("text_blocks", [])
        proper_terms = {
            t.get("term", ""): t
            for t in builder.unified_request.get("reference", {}).get("proper_terms", [])
        }

        # term_key → 出现它的 text_block 索引列表
        term_block_map: dict[str, list[int]] = {}
        for i, block in enumerate(text_blocks):
            refs = block.get("proper_refs", [])
            for ref in refs:
                if ref not in term_block_map:
                    term_block_map[ref] = []
                term_block_map[ref].append(i)

        if not term_block_map:
            return []

        # disambiguation_mode 判断
        mode = self._config.disambiguation_mode
        if mode == "similarity":
            return []  # 不需要 LLM 消歧

        # llm / hybrid 模式：收集所有匹配术语
        # 注：hybrid 模式理想行为是仅收集 LOW/UNKNOWN 置信度术语，
        # 但 confidence 数据需要 ProperAnalyzer 集成，当前暂全部收集
        if mode == "hybrid":
            _logger.debug(
                f"[{self.file_name}] hybrid 消歧模式："
                f"confidence 过滤需要 ProperAnalyzer 集成，当前收集全部匹配术语"
            )

        result = []
        for term_key, block_indices in term_block_map.items():
            term_data = proper_terms.get(term_key, {"term": term_key, "translation": ""})
            result.append({
                "kr": term_data.get("term", term_key),
                "cn": term_data.get("translation", ""),
                "note": term_data.get("note", ""),
                "text_block_indices": block_indices,
            })

        return result

    def _apply_disambiguation(
        self, builder: "RequestBuilder", disambiguated: list[dict]
    ) -> None:
        """将消歧结果应用到 builder 的术语表。

        对 applies=false 的术语，从 unified_request["reference"]["proper_terms"] 中移除，
        并通过 unified_request["text_blocks"] 中对应的 proper_refs 清除引用。
        """
        if not disambiguated:
            return

        excluded_terms: set[str] = set()
        for item in disambiguated:
            if not item.get("applies", True):
                excluded_terms.add(item.get("term", ""))

        if not excluded_terms:
            return

        # 从 reference 中移除不适用的术语
        proper_terms = builder.unified_request.get("reference", {}).get("proper_terms", [])
        builder.unified_request["reference"]["proper_terms"] = [
            t for t in proper_terms
            if t.get("term", "") not in excluded_terms
        ]

        # 从 text_blocks 中清除对应引用
        text_blocks = builder.unified_request.get("text_blocks", [])
        for block in text_blocks:
            refs = block.get("proper_refs", [])
            if refs:
                block["proper_refs"] = [r for r in refs if r not in excluded_terms]
                if not block["proper_refs"]:
                    del block["proper_refs"]

        _logger.info(
            f"[{self.file_name}] 阶段 0 消歧：排除了 {len(excluded_terms)} 个不适用的术语: "
            f"{', '.join(sorted(excluded_terms))}"
        )

    # ========== 阶段 2：自校验 ==========

    def _apply_corrections(
        self, translations: list[str], checked: list[dict]
    ) -> list[str]:
        """应用阶段 2 自校验修正。

        仅对 checked 中 changed=true 的条目替换对应索引的翻译文本。
        checked 中的 id 字段为 1-based 序号，对应 translations 的索引。

        Args:
            translations: 阶段 1 的翻译文本列表
            checked: 阶段 2 的校验结果 [{id, translation, changed, change_reason}, ...]

        Returns:
            修正后的翻译文本列表
        """
        result = list(translations)  # 浅拷贝
        corrections = 0
        for item in checked:
            if item.get("changed", False):
                idx = int(item.get("id", 0)) - 1  # 1-based → 0-based
                if 0 <= idx < len(result):
                    result[idx] = item.get("translation", result[idx])
                    corrections += 1

        if corrections > 0:
            _logger.info(
                f"[{self.file_name}] 阶段 2 自校验：修正了 {corrections}/{len(checked)} 条翻译"
            )
        return result

    # ========== 加载与检查 ==========

    def _load_jsons(self) -> ProcessOutcome | None:
        """加载 KR/EN/JP/LLC JSON 文件。出错时返回 ProcessOutcome。"""
        try:
            with open(self.path_config.KR_path, "r", encoding="utf-8-sig") as f:
                self.kr_json = json.load(f)
            try:
                with open(self.path_config.EN_path, "r", encoding="utf-8-sig") as f:
                    self.en_json = json.load(f)
            except FileNotFoundError:
                _logger.debug(f"[{self.file_name}] EN 参考文件缺失: {self.path_config.EN_path}")
                self.en_json = deepcopy(self.kr_json)
            try:
                with open(self.path_config.JP_path, "r", encoding="utf-8-sig") as f:
                    self.jp_json = json.load(f)
            except FileNotFoundError:
                _logger.debug(f"[{self.file_name}] JP 参考文件缺失: {self.path_config.JP_path}")
                self.jp_json = deepcopy(self.kr_json)
            try:
                with open(self.path_config.LLC_path, "r", encoding="utf-8-sig") as f:
                    self.llc_json = json.load(f)
            except FileNotFoundError:
                _logger.debug(f"[{self.file_name}] LLC 参考文件缺失: {self.path_config.LLC_path}")
                self.llc_json = {}
        except json.JSONDecodeError as e:
            _logger.exception(f"[{self.file_name}] JSON 解析失败: {self.path_config.KR_path} (line {e.lineno}, col {e.colno})")
            self._save_except()
            return ProcessOutcome(
                ProcessResult.JSON_DECODE_ERROR,
                self.file_name,
                {"file_path": str(self.path_config.KR_path), "reason": f"line {e.lineno}, col {e.colno}: {e.msg}"},
            )
        return None

    def _check_empty(self) -> ProcessOutcome | None:
        """检查 KR 数据是否为空。为空时返回 ProcessOutcome。"""
        if self.kr_json in EMPTY_DATA or self.kr_json.get("dataList", []) in EMPTY_DATA_LIST:
            if self.path_config.LLC_path.exists():
                self._save_llc()
                return ProcessOutcome(ProcessResult.EMPTY_WITH_LLC, self.file_name)
            else:
                return ProcessOutcome(ProcessResult.EMPTY_SKIPPED, self.file_name)
        return None

    def _check_translated(self) -> ProcessOutcome | None:
        """检查整文件是否已被 LLC 完全覆盖。已完全覆盖时返回 ProcessOutcome。

        判定分两层，缺一不可：
          1. 条目级：KR 的每个条目键都在 LLC 中；
          2. 字段级：每个条目内部，KR 的每个文本字段都能在 LLC 同路径上取到值
             （见 ``_get_translating``）。只满足第 1 层就整文件拷贝旧熟肉，
             会让「旧 id 里新增的技能等级/新 coin 描述」等改动**直接从产出里消失**。

        注意：这里**绝不能**对 ``llc_index`` 做 ``_align``。历史上曾在
        jp/kr/en 长度不一致时用 KR 原文补齐 LLC 的缺失键，导致下面
        ``set(kr).issubset(set(llc))`` 恒为真，整文件被误判为已翻译，
        该文件本次的全部新条目被静默丢弃。jp/en 的参考缺失由
        ``_get_translating_text`` 自行兜底，不需要在这里对齐。
        """
        if not self.llc_index or not self.path_config.LLC_path.exists():
            return None
        if not set(self.kr_index).issubset(set(self.llc_index)):
            return None

        self._get_translating()
        if self.translating_list:
            return None

        self._save_llc()
        return ProcessOutcome(ProcessResult.ALREADY_TRANSLATED, self.file_name)

    # ========== 初始化 ==========

    def _init_base_data(self) -> None:
        self.en_data = self.en_json.get("dataList", [])
        self.kr_data = self.kr_json.get("dataList", [])
        self.jp_data = self.jp_json.get("dataList", [])
        self.llc_data = self.llc_json.get("dataList", [])
        self.is_story = (self.path_config.rel_path.parent.name == "StoryData")
        self.is_skill = self.path_config.real_name.startswith("Skills_")

    def _make_data_index(self) -> None:
        if self.is_story:
            self.en_index = {i: d for i, d in enumerate(self.en_data)}
            self.kr_index = {i: d for i, d in enumerate(self.kr_data)}
            self.jp_index = {i: d for i, d in enumerate(self.jp_data)}
            self.llc_index = {i: d for i, d in enumerate(self.llc_data)}
        else:
            # 防御：部分 JSON 的 dataList 元素缺少 "id" 键，回退为 enumerate 索引
            def _make_non_story_index(data: list) -> dict:
                if data and isinstance(data[0], dict) and "id" in data[0]:
                    return {i["id"]: i for i in data}
                return {idx: item for idx, item in enumerate(data)}

            self.en_index = _make_non_story_index(self.en_data)
            self.kr_index = _make_non_story_index(self.kr_data)
            self.jp_index = _make_non_story_index(self.jp_data)
            self.llc_index = _make_non_story_index(self.llc_data)

    @staticmethod
    def _flatten_texts(entry) -> dict:
        """把单个条目展开为 ``{字段路径: 文本}``，与请求构建口径完全一致。

        剔除 ``AVOID_PATH``（usage/id/model）对应的路径。
        """
        flat = flatten_dict_enhanced(entry, ignore_types=[None, int, float])
        for path in [p for p in flat if p and p[-1] in AVOID_PATH]:
            del flat[path]
        return flat

    @staticmethod
    def _field_covered(llc_value, kr_value) -> bool:
        """LLC 在某个字段路径上是否已经提供了可用译文。

        - LLC 该路径为空/空白：仅当 KR 也是空文本时才算覆盖（无需翻译）；
        - LLC 该路径是韩文原文的原样回填（LLC == KR 且含韩文）：视为**未覆盖**，
          下一轮会重新翻译，避免这种漏翻一劳永逸地留在熟肉里；
        - 其余情况（有值且不是原样回填）视为已覆盖。
        """
        if isinstance(llc_value, str) and not llc_value.strip():
            return isinstance(kr_value, str) and not kr_value.strip()
        if (
            isinstance(llc_value, str)
            and isinstance(kr_value, str)
            and llc_value.strip() == kr_value.strip()
            and contains_hangul(kr_value)
        ):
            return False
        return True

    def _get_translating(self) -> None:
        """计算待翻译集合，粒度是 ``(条目键, 字段路径)`` 而不是整条目。

        - 条目键不在 LLC：整条待翻译；
        - 条目键已在 LLC：只挑出 LLC 缺失/为空、或仍是韩文原样回填的字段。

        这样「旧 id 里新增的技能等级、新增的 coin 描述」等改动才会被送进请求，
        而不是因为 id 已存在就被整条跳过。
        """
        self.translating_list = []
        self.translating_fields = {}
        for key in self.kr_index:
            kr_flat = self._flatten_texts(self.kr_index[key])
            if key not in self.llc_index:
                pending = list(kr_flat)
            else:
                llc_flat = self._flatten_texts(self.llc_index[key])
                pending = [
                    path
                    for path, kr_value in kr_flat.items()
                    if path not in llc_flat
                    or not self._field_covered(llc_flat[path], kr_value)
                ]
            if not pending:
                continue
            self.translating_fields[key] = pending
            self.translating_list.append(key)

    # ========== 文本提取 / 重建 ==========

    def _get_translating_text(self, lang: str = "kr") -> dict:
        lang_index = {"kr": self.kr_index, "jp": self.jp_index, "en": self.en_index}[lang]
        translating_text = {}
        for key in self.translating_list:
            entry = lang_index.get(key)
            flat = self._flatten_texts(entry) if entry is not None else {}
            pending = self.translating_fields.get(key)
            if pending is not None:
                allowed = set(pending)
                flat = {path: value for path, value in flat.items() if path in allowed}
            translating_text[key] = flat
        return translating_text

    def _de_get_translating_text(self, translated_text: dict) -> dict:
        """组装产出：以 KR 结构为骨架，先回填 LLC 已翻译字段，再写入本次新译文。

        不能像旧实现那样「已覆盖条目整条用 LLC 替换」——那会把 KR 该条目
        内部的全部变化（新增字段、改写文本）一起丢掉。
        """
        base = deepcopy(self.kr_index)

        # 1) 字段级回填 LLC 已翻译的字段（未覆盖的字段保持 KR 原文，由第 2 步填）
        for key, entry in base.items():
            if key not in self.llc_index:
                continue
            kr_flat = self._flatten_texts(entry)
            llc_flat = self._flatten_texts(self.llc_index[key])
            updates = {
                path: llc_flat[path]
                for path, kr_value in kr_flat.items()
                if path in llc_flat and self._field_covered(llc_flat[path], kr_value)
            }
            if updates:
                update_dict_with_flattened(entry, updates)

        # 2) 写入本次新翻译；非空 KR 字段拿到空译文时回填 KR 原文，避免产出出现空洞
        for key in self.translating_list:
            if key not in base:
                continue
            kr_flat = self._flatten_texts(self.kr_index[key])
            updates = dict(translated_text.get(key) or {})
            for path, value in list(updates.items()):
                if (
                    isinstance(value, str)
                    and not value.strip()
                    and str(kr_flat.get(path, "")).strip()
                ):
                    updates[path] = kr_flat[path]
            if updates:
                update_dict_with_flattened(base[key], updates)

        self._base_index = base
        return self._base_index

    def _de_get_translating(self) -> dict:
        return {"dataList": [self._base_index[key] for key in self.kr_index]}

    # ========== 保存 ==========

    def _save_result(self, data: dict) -> None:
        if not self._config.save_result:
            return
        self.path_config.target_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path_config.target_file, "w", encoding="utf-8-sig") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _save_llc(self) -> None:
        self.path_config.target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path_config.LLC_path, self.path_config.target_file)

    def _save_except(self) -> None:
        """回退保存：依次尝试 LLC → EN → JP → KR。"""
        for path_attr in ("LLC_path", "EN_path", "JP_path", "KR_path"):
            try:
                src = getattr(self.path_config, path_attr)
                if src.exists():
                    shutil.copy2(src, self.path_config.target_file)
                    return
            except Exception:
                continue
        _logger.warning(f"[{self.file_name}] 所有回退路径均不可用，无法保存结果文件")


# ============================================================
# _SimpleRequestBuilder —— 非 LLM 翻译器使用
# ============================================================

class _SimpleRequestBuilder:
    """非 LLM 翻译器的轻量请求构建器（保留原有行为）。"""

    def __init__(self, request_text: dict):
        self.en_texts = request_text["en"]
        self.kr_texts = request_text["kr"]
        self.jp_texts = request_text.get("jp", {})

    def build(self) -> list:
        EN_result, KR_result, JP_result = [], [], []
        for idx in self.kr_texts:
            for text in self.kr_texts[idx].values():
                KR_result.append(text)
            for text in self.jp_texts.get(idx, {}).values():
                JP_result.append(text)
            for text in self.en_texts.get(idx, {}).values():
                EN_result.append(text)

        if not (len(KR_result) == len(EN_result) == len(JP_result)):
            raise ValueError(
                f"语言文本长度不一致: KR={len(KR_result)}, "
                f"EN={len(EN_result)}, JP={len(JP_result)}"
            )

        empty_idxs = {
            i for i, (kr, en, jp) in enumerate(zip(KR_result, EN_result, JP_result))
            if kr in EMPTY_TEXT and en in EMPTY_TEXT and jp in EMPTY_TEXT
        }
        self.KR_build = [t for i, t in enumerate(KR_result) if i not in empty_idxs]
        self.EN_build = [t for i, t in enumerate(EN_result) if i not in empty_idxs]
        self.JP_build = [t for i, t in enumerate(JP_result) if i not in empty_idxs]

    def get_request_text(self, from_lang: str = "KR") -> list[str]:
        return getattr(self, f"{from_lang}_build")

    def deBuild(self, translated_texts: list[str], from_lang: str = "kr") -> dict:
        """将扁平翻译文本列表还原为嵌套字典结构。

        当翻译数量与预期不符时，不再抛出异常：
        - 不足时用 KR 原文填充缺失条目
        - 多余时截断并警告
        """
        original = deepcopy(getattr(self, f"{from_lang}_texts"))

        # 先计算预期数量，同时收集 KR 原文用于可能的回退填充
        expected_count = 0
        kr_fallbacks: list[str] = []
        for idx in original:
            kr_item = self.kr_texts.get(idx, {})
            jp_item = self.jp_texts.get(idx, {})
            en_item = self.en_texts.get(idx, {})
            for path_tuple in kr_item:
                jp_val = jp_item.get(path_tuple, "")
                en_val = en_item.get(path_tuple, "")
                kr_val = kr_item[path_tuple]
                if not (jp_val in EMPTY_TEXT and en_val in EMPTY_TEXT and kr_val in EMPTY_TEXT):
                    expected_count += 1
                    kr_fallbacks.append(kr_val)

        # 韧性处理：数量不匹配时用 KR 原文补齐或截断
        actual_count = len(translated_texts)
        if actual_count < expected_count:
            shortfall = expected_count - actual_count
            _logger.warning(
                f"译文数量不足: 预期 {expected_count}, 实际 {actual_count}"
                f"（{shortfall} 个文本块回退为 KR 原文）"
            )
            translated_texts = list(translated_texts) + kr_fallbacks[-shortfall:]
        elif actual_count > expected_count:
            excess = actual_count - expected_count
            _logger.warning(
                f"译文数量多于预期: 预期 {expected_count}, 实际 {actual_count}"
                f"（截断多余 {excess} 个）"
            )
            translated_texts = translated_texts[:expected_count]

        it = iter(translated_texts)
        for idx in original:
            kr_item = self.kr_texts.get(idx, {})
            jp_item = self.jp_texts.get(idx, {})
            en_item = self.en_texts.get(idx, {})
            for path_tuple in kr_item:
                jp_val = jp_item.get(path_tuple, "")
                en_val = en_item.get(path_tuple, "")
                kr_val = kr_item[path_tuple]
                if not (jp_val in EMPTY_TEXT and en_val in EMPTY_TEXT and kr_val in EMPTY_TEXT):
                    original[idx][path_tuple] = next(it)
        return original
