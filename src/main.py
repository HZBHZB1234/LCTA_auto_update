import os
import sys
import logging
import json
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

workdir = Path(__file__).parent
project_root = workdir.parent
print(project_root)
sys.path.insert(0, str(project_root))
os.chdir(workdir)

logger = logging.getLogger()

file_handler = logging.FileHandler('app.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG)

# 创建控制台处理器
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)

# 设置格式
formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s'
)
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# 添加处理器到logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)
logger.setLevel(logging.DEBUG)

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

print(f'环境变量FORCE_RUN为{os.getenv("FORCE_RUN")}')
FORCE_RUN = False if os.getenv("FORCE_RUN") == "False" else True
if FORCE_RUN:
    print('强制运行')

from webFunc import get_recent_commits, GitHubReleaseFetcher

llcCommit = get_recent_commits(
    owner="LocalizeLimbusCompany",
    repo="LocalizeLimbusCompany",
    max_commits=5,
    token=os.environ.get("LCTA_FETCHER")
)

if not llcCommit:
    logger.warning("未获取到任何commit信息，可能是请求失败或仓库无commit记录。")
    sys.exit(1)
for commit in llcCommit:
    if commit["message"].startswith("Auto RAW Update"):
        logger.info("检测到自动RAW更新提交。")
        break
else:
    logger.info("未检测到自动RAW更新提交，程序结束。")
    if not FORCE_RUN:
        sys.exit(0)
    
commit_time = commit['date']
commit_time = datetime.fromisoformat(commit_time.replace('Z', '+00:00'))

release_fetcher = GitHubReleaseFetcher(
    use_proxy=False,
    ignore_ssl=True
)

try:
    this_release = release_fetcher.get_latest_release(
        "HZBHZB1234", "LCTA_auto_update")
    record_time = datetime.fromisoformat(this_release.published_at.replace('Z', '+00:00'))
except:
    print("无法获取最新release信息。")
    sys.exit(1)

if commit_time <= record_time:
    logger.info("没有新的更新提交，程序结束。")
    if not FORCE_RUN:
        sys.exit(0)

release = release_fetcher.get_latest_release(
    "LocalizeLimbusCompany", "LocalizeLimbusCompany")
release_time = datetime.fromisoformat(release.published_at.replace('Z', '+00:00'))

if release_time >= commit_time:
    logger.info("没有新的更新提交，程序结束。")
    sys.exit(0)
    
import tarfile
import requests
import shutil
import tempfile
from translateFunc.translate_main import *
from translateFunc.translate_doc import *
from translatekit import *
from get_proper import fetch as fetch_proper

HAS_PREFIX = False

with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    with requests.get(commit['download_links']['tar.gz'],
                    stream=True, verify=False) as r:
        r.raise_for_status()
        with open(tmp / "latest_release.tar.gz", "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    
    # shutil.copy(project_root / '_dev' / 'test.tar.gz',
    #             tmp / "latest_release.tar.gz")
    
    with tarfile.open(tmp / "latest_release.tar.gz") as tar:
        tar.extractall(path=tmp / "llc")
        
    target_dir = project_root / "LLc-CN-LCTA"
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir()
        
    is_text = config.get("use_text", True)
    translate_config = TranslationConfig(
        api_setting={
            "api_key": os.getenv('DEEPSEEK'),
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat",
            "temperature": 1.0,
            "max_tokens": 8000,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "system_prompt": TEXT_SYSTEM_PROMPT if is_text else JSON_SYSTEM_PROMPT,
            "response_format": "text" if is_text else "json_object"
        },
        debug_mode=True,
        enable_cache=True,
        enable_metrics=True,
        text_max_length=20000
    )
    
    translator: TranslatorBase = LLMGeneralTranslator(
        config=translate_config
    )
    
    tmp_base_path = list((tmp / "llc").iterdir())[0]
    
    base_path_config = PathConfig(
        target_path=target_dir,
        llc_base_path=tmp_base_path / "LLC_zh-CN",
        KR_base_path=tmp_base_path / "KR",
        JP_base_path=tmp_base_path / "JP",
        EN_base_path=tmp_base_path / "EN"
    )
    
    request_config = RequestConfig(
        enable_proper=True,
        enable_role=True,
        enable_skill=True,
        is_text_format=is_text,
        is_llm=True,
        translator=translator,
        from_lang='EN',
        save_result=True
    )
    
    base_path_config.create_need_dirs()
    target_files = list(base_path_config.KR_base_path.rglob("*.json"))
    logger.info(f"找到 {len(target_files)} 个文件。")
    if HAS_PREFIX:
        model_file = base_path_config.KR_base_path / \
            "KR_ScenarioModelCodes-AutoCreated.json"
        keyword_file = base_path_config.KR_base_path / \
            "KR_BattleKeywords.json"
    else:
        model_file = base_path_config.KR_base_path / \
            "ScenarioModelCodes-AutoCreated.json"
        keyword_file = base_path_config.KR_base_path / \
            "BattleKeywords.json"
    target_files.remove(model_file)
    target_files.remove(keyword_file)
    target_files.insert(0, model_file)
    target_files.insert(0, keyword_file)
    
    matcher = MatcherOrganizer()
    
    matcher.update_proper(fetch_proper())
    
    for file in target_files:
        file_path_config = FilePathConfig(
            KR_path=file,
            _PathConfig=base_path_config,
            has_prefix=HAS_PREFIX
        )
        
        processer = FileProcessor(
            path_config=file_path_config,
            matcher=matcher,
            request_config=request_config,
            logger=logger
        )
        
        try:
            try:
                processer.process_file()
            except ProcesserExit as e:
                logger.info(f"文件{file_path_config.rel_path}处理完毕，退出码{e.exit_type} ")
                if e.exit_type == 'translation_length_error':
                    raise
        except Exception as e:
            logger.error(f"文件{file_path_config.rel_path}处理出错，错误信息：{e}")
            logger.exception(e)
            try:
                logger.info('尝试切换请求格式')
                translator.update_config(
                    system_prompt=JSON_SYSTEM_PROMPT if is_text else TEXT_SYSTEM_PROMPT,
                    response_format="json_object" if is_text else "text")
                cache_request_config = deepcopy(request_config)
                cache_request_config.is_text_format = not is_text
                processer = FileProcessor(
                    path_config=file_path_config,
                    matcher=matcher,
                    request_config=cache_request_config,
                    logger=logger
                )
                try:
                    processer.process_file()
                except ProcesserExit as e:
                    logger.info(f"文件{file_path_config.rel_path}处理完毕，退出码{e.exit_type} ")
            except Exception as e:
                logger.error(f"文件{file_path_config.rel_path}处理出错，切换后任然失败。错误信息：{e}")
                logger.exception(e)
            finally:
                translator.update_config(
                    system_prompt=TEXT_SYSTEM_PROMPT if is_text else JSON_SYSTEM_PROMPT,
                    response_format="text" if is_text else "json_object")
        if file_path_config.KR_path == model_file:
            matcher.update_models(kr_role=json.loads(
                    file_path_config.KR_path.read_text(encoding='utf-8-sig')),
                cn_role=json.loads(
                    file_path_config.target_file.read_text(encoding='utf-8-sig')))
            
        if file_path_config.KR_path == keyword_file:
            matcher.update_efects(KRaffect=json.loads(
                    file_path_config.KR_path.read_text(encoding='utf-8-sig')),
                CNaffect=json.loads(
                    file_path_config.target_file.read_text(encoding='utf-8-sig')))

    today = datetime.now()
    current_date = today.strftime("%Y%m%d")  # 格式：YYYYMMDD

    previous_version = this_release.tag_name

    try:
        # 提取上一个版本号的日期部分和序号部分
        prev_date = previous_version[:8]  # 前8位是日期
        prev_sequence = int(previous_version[8:])  # 后2位是序号
        
        if prev_date == current_date:
            # 如果是同一天，序号加1
            new_sequence = prev_sequence + 1
            # 确保序号是两位数（01-99）
            if new_sequence > 99:
                raise ValueError("当日版本序号已超过99，请使用新的日期")
            VERSION = f"{current_date}{new_sequence:02d}"
        else:
            # 如果是新的一天，从01开始
            VERSION = f"{current_date}01"
    except (ValueError, IndexError):
        print(f"警告: 上一个版本号'{previous_version}'格式不正确，将重置为今天的新版本")
        VERSION = f"{current_date}01"

    try:
        Info_path = target_dir / "Info"
        Info_path.mkdir(parents=True, exist_ok=True)
        license_project = tmp_base_path / "LICENSE"
        license_target = Info_path / "LICENSE"
        shutil.copy(license_project, license_target)
        version_target = Info_path / "version.json"
        version_target.write_text(json.dumps(
            {"version": VERSION, "notice": "本次文本更新没有提示。"},
            ensure_ascii=False, indent=4))
    except Exception as e:
        logger.error('创建版本信息失败')
        logger.exception(e)
        
    try:
        font_project = tmp_base_path / 'Fonts' / 'ChineseFont.ttf'
        font_target = target_dir / 'Fonts' / 'Context'
        font_target.mkdir(parents=True, exist_ok=True)
        font_target = font_target / 'ChineseFont.ttf'
        shutil.copy(font_project, font_target)
    except Exception as e:
        logger.error('复制字体文件失败')
        logger.exception(e)

usage = translator.get_performance_metrics()

RELEASE_NOTE = f"""
{VERSION}版资源更新
{VERSION} version Resource Update

本次更新开销:
请求数量: {usage.get('request_count', '获取失败')}
请求字符数: {usage.get('chars_translated', '获取失败')}

使用LCTA工具箱-启动器以自动进行版本更新
[项目链接](https://github.com/HZBHZB1234/LCTA-Limbus-company-transfer-auto)

感觉有用就给个star吧
"""

UPDATE_MARKDOWN = project_root / 'update.md'
UPDATE_MARKDOWN.write_text(RELEASE_NOTE, encoding='utf-8')