#!/usr/bin/env python3
"""Hive 数据服务查询工具 - 提交 Hive 查询并下载结果。

使用方法:
    python hive_query.py --env uat -u USER -p PASS -c CODE
    python hive_query.py --env prod -u USER -p PASS -c CODE --params '{"key":"val"}'

环境变量:
    HIVE_USERNAME  大数据平台用户名
    HIVE_PASSWORD  大数据平台密码
"""

import argparse
import hashlib
import json
import os
import time
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# 环境配置
# ---------------------------------------------------------------------------
ENVIRONMENTS = {
    "prod": {
        "name": "生产环境（内网）",
        "auth": "http://bigdata-api.anta.com/portal-server",
        "svc": "http://bigdata-api.anta.com/svc-server",
    },
    "uat": {
        "name": "UAT 环境",
        "auth": "http://bigdata-uat.anta.com/portal-server",
        "svc": "http://bigdata-uat.anta.com/svc-server",
    },
    "outer": {
        "name": "生产环境（公网）",
        "auth": "https://bigdata-outer.anta.com/portal-server",
        "svc": "https://bigdata-outer.anta.com/svc-server",
    },
    "office": {
        "name": "办公网络",
        "auth": "http://bigdata.anta.com/portal-server",
        "svc": "http://bigdata.anta.com/svc-server",
    },
}


# ---------------------------------------------------------------------------
# API 方法
# ---------------------------------------------------------------------------
def get_token(auth_url, username, password):
    """获取认证 token，有效期 2 天。"""
    resp = requests.post(
        f"{auth_url}/rest/auth",
        json={"username": username, "password": password},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"认证失败: {body.get('msg', '未知错误')}")
    return body["data"]


def submit_query(svc_url, token, code, params):
    """提交 Hive 查询，返回 queryId。"""
    ts = int(time.time() * 1000)
    sign = hashlib.md5(f"{code}{ts}".encode()).hexdigest()

    resp = requests.post(
        f"{svc_url}/rest/svc/sql/{code}",
        json={"params": params, "ts": ts, "sign": sign},
        headers={"Accept": "application/json", "Content-Type": "application/json", "token": token},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    code_val = body.get("code")
    if code_val != 0:
        msg = body.get("msg", "未知错误")
        if code_val == 1002:
            raise RuntimeError(f"验证失败: {msg}")
        if code_val == 500:
            raise RuntimeError(f"请求被限制: {msg}")
        if code_val == 410:
            raise RuntimeError(f"数据未准备好: {msg}")
        raise RuntimeError(f"提交查询失败 [{code_val}]: {msg}")
    return body["data"]["queryId"]


def get_status(svc_url, token, code, query_id):
    """获取查询状态。state: INIT / RUNNING / SUCCESS / FAILURE"""
    resp = requests.post(
        f"{svc_url}/rest/svc/sql/{code}/{query_id}",
        json={},
        headers={"Accept": "application/json", "Content-Type": "application/json", "token": token},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    # 返回 data 字段，与文档中其他接口的 {code, msg, data} 结构保持一致
    return body.get("data", body)


def list_files(svc_url, token, code, path):
    """获取 HDFS 路径下的文件列表。"""
    resp = requests.get(
        f"{svc_url}/rest/hdfs/file/list",
        params={"path": path, "code": code},
        headers={"Accept": "application/json", "token": token},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", body)


def download_file(svc_url, token, code, remote_path, local_path):
    """下载单个 HDFS 文件到本地。"""
    resp = requests.get(
        f"{svc_url}/rest/hdfs/file/download",
        params={"path": remote_path, "code": code},
        headers={"Accept": "application/json", "Content-Type": "application/json", "token": token},
        timeout=300,
        stream=True,
    )
    resp.raise_for_status()
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


# ---------------------------------------------------------------------------
# 可复用下载流程
# ---------------------------------------------------------------------------
def run_query_and_download(
    *,
    env_key: str,
    username: str,
    password: str,
    code: str,
    params: Optional[dict] = None,
    output_dir: str = "./output",
    poll_interval: int = 300,
    timeout: int = 3600,
    verbose: bool = True,
) -> list[str]:
    """提交 Hive 查询并下载结果文件，返回本地文件路径列表。"""
    if env_key not in ENVIRONMENTS:
        raise ValueError(f"未知环境: {env_key}")
    if not username or not password:
        raise ValueError("username 与 password 不能为空")

    params = params or {}
    env = ENVIRONMENTS[env_key]
    auth_url, svc_url = env["auth"], env["svc"]

    def _log(msg: str) -> None:
        if verbose:
            print(msg)

    _log(f"环境: {env['name']}")
    _log("获取 token ...")
    token = get_token(auth_url, username, password)
    _log("认证成功")

    _log(f"提交查询 (code={code}) ...")
    query_id = submit_query(svc_url, token, code, params)
    _log(f"queryId: {query_id}")

    _log(
        f"等待查询完成 (轮询间隔 {poll_interval}s, 超时 {timeout}s) ..."
    )
    elapsed = 0
    hdfs_path = ""
    while elapsed < timeout:
        status = get_status(svc_url, token, code, query_id)
        state = status.get("state")
        progress = status.get("progress", 0)
        _log(f"  状态: {state}, 进度: {progress:.0%}")
        if state == "SUCCESS":
            hdfs_path = status.get("hdfsPath", "")
            break
        if state == "FAILURE":
            raise RuntimeError(f"查询失败: queryId={query_id}")
        if state not in ("INIT", "RUNNING"):
            raise RuntimeError(f"未知状态: {state}")
        time.sleep(poll_interval)
        elapsed += poll_interval
    else:
        raise TimeoutError(f"查询超时 ({timeout}s)")

    _log(f"查询完成, hdfsPath: {hdfs_path}")
    if not hdfs_path:
        _log("结果存储在 OBS，请到 OBS 获取数据。")
        return []

    _log(f"获取文件列表: {hdfs_path}")
    files = list_files(svc_url, token, code, hdfs_path)
    if not isinstance(files, list):
        _log("文件列表为空")
        return []

    download_tasks = [f for f in files if not f.get("isdir")]
    if not download_tasks:
        _log("没有可下载的文件")
        return []

    local_paths: list[str] = []
    _log(f"下载 {len(download_tasks)} 个文件到 {output_dir}")
    for f in download_tasks:
        name = f["path"]
        remote = f"{hdfs_path}/{name}" if not name.startswith("/") else name
        local = os.path.join(output_dir, name)
        _log(f"  {remote} -> {local}")
        download_file(svc_url, token, code, remote, local)
        local_paths.append(local)

    _log("完成。")
    return local_paths


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Hive 数据服务查询工具")
    parser.add_argument("--env", choices=list(ENVIRONMENTS), default="uat", help="运行环境 (默认: uat)")
    parser.add_argument("-u", "--username", default=os.environ.get("HIVE_USERNAME"), help="用户名 (或环境变量 HIVE_USERNAME)")
    parser.add_argument("-p", "--password", default=os.environ.get("HIVE_PASSWORD"), help="密码 (或环境变量 HIVE_PASSWORD)")
    parser.add_argument("-c", "--code", required=True, help="数据服务编码")
    parser.add_argument("--params", default="{}", help="查询参数, JSON 格式 (默认: {})")
    parser.add_argument("-o", "--output-dir", default="./output", help="下载目录 (默认: ./output)")
    parser.add_argument("--poll-interval", type=int, default=300, help="轮询间隔/秒 (默认: 300)")
    parser.add_argument("--timeout", type=int, default=3600, help="超时/秒 (默认: 3600)")
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error("请提供 -u/--username 和 -p/--password，或设置环境变量 HIVE_USERNAME / HIVE_PASSWORD")

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as e:
        parser.error(f"params JSON 解析错误: {e}")

    run_query_and_download(
        env_key=args.env,
        username=args.username,
        password=args.password,
        code=args.code,
        params=params,
        output_dir=args.output_dir,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
        verbose=True,
    )


if __name__ == "__main__":
    main()