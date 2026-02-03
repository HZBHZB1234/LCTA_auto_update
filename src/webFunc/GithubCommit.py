import requests
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional

def get_recent_commits(
    owner: str,
    repo: str,
    days: Optional[int] = None,
    max_commits: Optional[int] = None,
    token: Optional[str] = None
) -> List[Dict[str, str]]:
    """
    获取指定GitHub仓库的近期commit并生成源码下载地址
    
    参数:
        owner: 仓库所有者/组织名
        repo: 仓库名称
        days: 获取最近多少天的commit（默认7天）
        max_commits: 最大返回commit数量（默认20个）
        token: GitHub个人访问令牌（可选，用于提高API限制）
    
    返回:
        List[Dict]: 包含commit信息和下载地址的字典列表
    """
    
    # GitHub API 端点
    base_url = "https://api.github.com"
    
    # 构造headers，如果有token就加入
    headers = {
        "Accept": "application/vnd.github.v3+json"
    }
    if token:
        headers["Authorization"] = f"token {token}"
    
    # API请求参数
    params = {
        "page": 1
    }
    
    if days:
        since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        params["since"] = since_date
        
    if max_commits:
        params["per_page"] = max_commits
    
    commits_info = []
    
    try:
        # 获取commit列表
        commits_url = f"{base_url}/repos/{owner}/{repo}/commits"
        response = requests.get(commits_url, headers=headers,
                                params=params, verify=False)
        
        # 检查请求是否成功
        if response.status_code != 200:
            error_msg = response.json().get("message", "未知错误")
            print(f"请求失败: {response.status_code} - {error_msg}")
            
            # 尝试获取更详细的错误信息
            if response.status_code == 404:
                print(f"仓库 {owner}/{repo} 不存在或无权访问")
            elif response.status_code == 403:
                print("API限制，请稍后重试或使用token")
            
            return []
        
        commits = response.json()
        
        # 如果返回的是空列表或非列表格式
        if not isinstance(commits, list):
            print("返回数据格式异常")
            return []
        
        print(f"找到 {len(commits)} 个最近 {days} 天的commit")
        
        for commit in commits:
            commit_data = commit.get("commit", {})
            sha = commit.get("sha", "")
            author_info = commit_data.get("author", {})
            
            # 构造commit信息
            commit_info = {
                "sha": sha[:8],  # 短SHA
                "full_sha": sha,
                "message": commit_data.get("message", "").split("\n")[0],  # 只取第一行
                "author": author_info.get("name", ""),
                "email": author_info.get("email", ""),
                "date": author_info.get("date", ""),
                "commit_url": commit.get("html_url", ""),
                # 源码下载地址
                "download_links": {
                    "zip": f"https://github.com/{owner}/{repo}/archive/{sha}.zip",
                    "tar.gz": f"https://github.com/{owner}/{repo}/archive/{sha}.tar.gz"
                }
            }
            
            # 添加更多详细信息
            if "author" in commit and commit["author"]:
                commit_info["github_author"] = commit["author"].get("login", "")
                commit_info["author_avatar"] = commit["author"].get("avatar_url", "")
            
            commits_info.append(commit_info)
        
        return commits_info
        
    except requests.exceptions.RequestException as e:
        print(f"网络请求错误: {e}")
        return []
    except json.JSONDecodeError as e:
        print(f"JSON解析错误: {e}")
        return []
    except Exception as e:
        print(f"未知错误: {e}")
        return []

