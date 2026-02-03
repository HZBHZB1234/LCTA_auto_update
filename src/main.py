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

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
with open("record.json", "r", encoding="utf-8") as f:
    record = json.load(f)

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
    sys.exit(0)
    
commit_time = commit['date']
commit_time = datetime.fromisoformat(commit_time.replace('Z', '+00:00'))

record_time = datetime.fromisoformat(record["last_update_time"])

if commit_time <= record_time:
    logger.info("没有新的更新提交，程序结束。")
    sys.exit(0)
    
release_fetcher = GitHubReleaseFetcher(
    use_proxy=False,
    ignore_ssl=True
)

release = release_fetcher.get_latest_release(
    "LocalizeLimbusCompany", "LocalizeLimbusCompany")
release_time = datetime.strptime(release.tag_name[:8], "%Y%m%d")

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

with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    with requests.get(commit['download_links']['tar.gz'],
                    stream=True, verify=False) as r:
        r.raise_for_status()
        with open(tmp / "latest_release.tar.gz", "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    
    with tarfile.open(tmp / "latest_release.tar.gz") as tar:
        tar.extractall(path=tmp / "llc")
        
    target_dir = project_root / "LLc-CN-LCTA"
    if target_dir.exists():
        shutil.rmtree(target_dir)
        
    is_text = config.get("use_text", True)
    translator = TranslationConfig(
        api_setting={
            "api_key": os.getenv('DEEPSEEK'),
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat",
            "temperature": 1.0,
            "max_tokens": 6000,
            "top_p": 1.0,
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
            "system_prompt": TEXT_SYSTEM_PROMPT if is_text else JSON_SYSTEM_PROMPT,
            "response_format": "text" if is_text else "json_object"
        },
        debug_mode=True,
        enable_cache=True,
        enable_metrics=True
    )
    
    base_path_config = PathConfig(
        target_path=target_dir,
        llc_base_path=tmp / "llc" / "LLC_zh-CN-",
        KR_base_path=tmp / "llc" / "KR",
        JP_base_path=tmp / "llc" / "JP",
        EN_base_path=tmp / "llc" / "EN"
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
    model_file = base_path_config.KR_base_path / \
        "KR_ScenarioModelCodes-AutoCreated.json"
    keyword_file = base_path_config.KR_base_path / \
        "KR_BattleKeywords.json"
    target_files.remove(model_file)
    target_files.remove(keyword_file)
    target_files.insert(0, model_file)
    target_files.insert(0, keyword_file)
    
    matcher_data = MatcherData(
        proper_data=fetch_proper()
    )
    
    matcher = TextMatcher(
        proper_matcher=SimpleMatcher()
    )
    
    for file in target_files:
        file_path_config = FilePathConfig(
            KR_path=file,
            _PathConfig=base_path_config
        )
        
        processer = FileProcessor(
            path_config=file_path_config,
            request_config=request_config
        )