#!/usr/bin/env python3
"""文件下载工具（cptool）。

根据指定的文件清单下载文件：先与本地清单比较版本（以修改时间作为版本标识），
仅下载有变化的文件。下载先写入临时文件（xx.bak.tmp），完整下载并通过 MD5
校验后才重命名为目标文件；大于 50MB 的文件失败重试时支持断点续传。

命令格式::

    python cptool.py http://localhost:8234/files.manifest [username] [password]

- username/password 为可选参数，用于生成 HTTP 请求的 Basic Authorization 认证。
- 清单地址支持 http(s):// 与 file:// 协议。
- --local-dir 指定本地保存目录（默认当前目录）。

执行流程:
1. 下载文件清单，临时保存为 file.manifest.tmp（保持 JSON 格式，仅把 path
   改为本地实际目录）。
2. 与本地 file.manifest 比较文件版本（修改时间），生成有变化的文件集合；
   本地清单不存在或为空时，所有文件都视为新版本。
3. 依次下载有变化的文件；单个文件失败会重试（至少 3 次）并打印原因，
   失败后继续处理后续文件，且本地清单不更新该文件的记录。
4. 全部处理完成后，用服务器清单完整替换本地清单，并删除临时清单文件。

退出码: 0=全部成功, 1=存在下载失败的文件, 2=参数或清单获取错误。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

# 本地清单固定文件名（需求：与服务器清单比较的“本地 file.manifest”）
LOCAL_MANIFEST_NAME = "file.manifest"
# 临时文件后缀：清单 → file.manifest.tmp，数据文件 → xx.bak.tmp
TEMP_SUFFIX = ".tmp"
# 超过 50MB 的文件在失败重试时启用断点续传
RESUME_THRESHOLD = 50 * 1024 * 1024
# 单个文件下载失败后的重试次数（至少 3 次）
MAX_RETRIES = 3
# 重试基础间隔（秒），逐次翻倍
RETRY_DELAY = 1.0
CHUNK_SIZE = 64 * 1024
HTTP_TIMEOUT = 60


class DownloadError(Exception):
    """下载失败，message 携带失败原因。"""


# ---------- 通用辅助 ----------


def _normalize_refpath(refpath) -> str:
    """统一清单 refpath：反斜杠替换为 /，去掉前导分隔符。"""
    text = str(refpath or "")
    return text.replace("\\", "/").lstrip("/")


def safe_local_path(local_dir: str, refpath) -> str | None:
    """把清单 refpath 映射为 local_dir 下的本地路径。

    需求：保存本地文件时，目录中的 / 要替换为当前系统的路径分隔符。
    返回 None 表示路径越界或非法（例如包含 ..）。
    """
    rel = _normalize_refpath(refpath)
    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts or ".." in parts:
        return None
    # Windows 下含 ":" 的组件形如盘符（C: / C:evil），跨盘拼接会越界，一律拒绝
    if any(":" in part for part in parts):
        return None
    candidate = os.path.normpath(os.path.join(local_dir, *parts))
    if candidate != local_dir and not candidate.startswith(local_dir + os.sep):
        return None
    return candidate


def _existing_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _md5_hex(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def describe_url_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    if isinstance(exc, URLError):
        return f"连接失败: {exc.reason}"
    return str(exc) or exc.__class__.__name__


# ---------- 清单地址与下载源 ----------


def file_url_to_path(url: str) -> str:
    """把 file:// 清单地址转换为本地文件系统路径。"""
    parts = urlsplit(url)
    netloc, path = parts.netloc, parts.path
    if netloc and netloc != "localhost":
        if re.fullmatch(r"[A-Za-z]:", netloc):
            # file://D:/data 形式：盘符被解析成了主机，合并回路径
            path = f"/{netloc}{path}"
        else:
            # 需求示例 file://localhost:8234/files.manifest：忽略其中的主机与端口
            print(f"提示: file:// 地址中的主机 “{netloc}” 已被忽略")
    path = unquote(path) or "/"
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path)


class ManifestSource:
    """解析清单地址，提供清单内容获取入口与单个文件的下载源。"""

    def __init__(self, url: str):
        self.url = url
        parts = urlsplit(url)
        self.scheme = (parts.scheme or "").lower()
        if self.scheme in ("http", "https"):
            self.kind = "http"
            path = parts.path or "/"
            cut = path.rfind("/")
            self.base_url = f"{parts.scheme}://{parts.netloc}" + (path[:cut] if cut > 0 else "")
        elif self.scheme == "file":
            self.kind = "file"
            self.manifest_fs_path = file_url_to_path(url)
        else:
            raise ValueError(f"不支持的清单地址 {url!r}: 仅支持 http(s):// 与 file:// 协议")

    def file_source_url(self, entry: dict) -> str:
        # 需求：清单路径中的 "\" 在拼下载 URL 时需要替换为 "/"
        refpath = _normalize_refpath(entry.get("refpath"))
        return self.base_url.rstrip("/") + "/" + quote(refpath, safe="/")

    def file_source_path(self, entry: dict) -> str:
        # file:// 协议：文件相对清单文件所在目录定位
        refpath = _normalize_refpath(entry.get("refpath"))
        base_dir = os.path.dirname(self.manifest_fs_path)
        return os.path.join(base_dir, *refpath.split("/"))


def http_open(url: str, auth: tuple[str, str] | None, headers: dict | None = None, method: str = "GET"):
    """发起 HTTP(S) 请求，auth 提供时附加 Basic Authorization 头。"""
    request = Request(url, method=method, headers=dict(headers or {}))
    if auth is not None:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    return urlopen(request, timeout=HTTP_TIMEOUT)


def fetch_manifest_bytes(source: ManifestSource, auth: tuple[str, str] | None) -> bytes:
    """读取清单内容（http(s) 下载或 file:// 本地读取）。"""
    if source.kind == "http":
        try:
            with http_open(source.url, auth) as resp:
                return resp.read()
        except (HTTPError, URLError, OSError) as exc:
            raise DownloadError(describe_url_error(exc)) from exc
    path = source.manifest_fs_path
    if not os.path.isfile(path):
        raise DownloadError(f"清单文件不存在: {path}")
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        raise DownloadError(f"读取清单失败: {exc}") from exc


# ---------- 下载实现 ----------


def _http_content_length(url: str, auth: tuple[str, str] | None) -> int | None:
    """通过 HEAD 请求获取文件总大小；失败返回 None。"""
    try:
        with http_open(url, auth, method="HEAD") as resp:
            value = resp.headers.get("Content-Length")
            return int(value) if value and value.strip().isdigit() else None
    except (HTTPError, URLError, OSError, ValueError):
        return None


def _download_http_once(url: str, tmp_path: str, auth: tuple[str, str] | None) -> None:
    """一次下载尝试，尽量利用已存在的临时文件断点续传（仅大文件）。

    传输中断时保留临时文件并抛出 DownloadError，供重试时续传。
    """
    tmp_size = _existing_size(tmp_path)
    headers: dict[str, str] = {}
    if tmp_size > 0:
        total = _http_content_length(url, auth)
        if total is not None:
            if tmp_size >= total:
                # 临时文件不少于服务器文件，交给 MD5 校验判断（失败会删除重来）
                return
            if total > RESUME_THRESHOLD:
                headers["Range"] = f"bytes={tmp_size}-"
        elif tmp_size > RESUME_THRESHOLD:
            headers["Range"] = f"bytes={tmp_size}-"
    try:
        resp = http_open(url, auth, headers=headers)
    except HTTPError as exc:
        if exc.code == 416 and "Range" in headers:
            # 本地临时文件长度已超出服务器文件，改为完整下载
            exc.close()
            headers.pop("Range")
            resp = http_open(url, auth)
        else:
            raise
    with resp:
        append = "Range" in headers and getattr(resp, "status", 200) == 206
        if append:
            # 续传起点必须与本地临时文件长度一致，否则服务器内容已变化，完整重下
            content_range = resp.headers.get("Content-Range", "")
            if not re.match(rf"bytes\s+{tmp_size}\s*-", content_range):
                append = False
        # 本次响应声明的字节数（206 时为剩余字节数），用于检测短读
        expected_raw = resp.headers.get("Content-Length")
        expected = int(expected_raw) if expected_raw and expected_raw.strip().isdigit() else None
        received = 0
        try:
            with open(tmp_path, "ab" if append else "wb") as fh:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
        except (HTTPException, OSError) as exc:
            # 中断：保留临时文件供下次断点续传
            raise DownloadError(f"传输中断: {describe_url_error(exc)}") from exc
        if expected is not None and received < expected:
            # 连接在传输中途断开时 read 只会提前返回空串，必须显式比较长度
            raise DownloadError(f"传输中断: 仅收到 {received}/{expected} 字节")


def _download_file_once(src_path: str, tmp_path: str) -> None:
    """file:// 协议：从本地路径复制（覆盖式写入临时文件）。"""
    if not os.path.isfile(src_path):
        raise DownloadError(f"源文件不存在: {src_path}")
    try:
        shutil.copyfile(src_path, tmp_path)
    except OSError as exc:
        raise DownloadError(f"复制失败: {exc}") from exc


def update_file(entry: dict, local_path: str, source: ManifestSource, auth: tuple[str, str] | None) -> bool:
    """下载并校验单个文件。

    成功：临时文件已重命名为目标文件并返回 True；失败（含重试用尽）：返回 False。
    """
    tmp_path = local_path + TEMP_SUFFIX
    refpath = str(entry.get("refpath", ""))
    expected_hash = str(entry.get("hash") or "").strip().lower()
    if not expected_hash:
        print(f"  提示: 清单未提供 {refpath} 的 hash，跳过 MD5 校验")
    os.makedirs(os.path.dirname(local_path) or local_path, exist_ok=True)

    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            print(f"  重试 {attempt}/{MAX_RETRIES}: {refpath}")
            if RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
        try:
            if source.kind == "http":
                _download_http_once(source.file_source_url(entry), tmp_path, auth)
            else:
                _download_file_once(source.file_source_path(entry), tmp_path)
            if expected_hash:
                actual = _md5_hex(tmp_path)
                if actual != expected_hash:
                    # 校验失败：删除临时文件，重试时必须完整重新下载
                    _remove_quiet(tmp_path)
                    raise DownloadError(f"MD5 校验失败（期望 {expected_hash}，实际 {actual}）")
            # 需求：文件完全下载并验证过 hash 后，才将临时文件重命名为目标文件
            os.replace(tmp_path, local_path)
            print(f"  已更新: {refpath} ({_existing_size(local_path)} 字节)")
            return True
        except (DownloadError, OSError) as exc:
            reason = str(exc) if isinstance(exc, DownloadError) else describe_url_error(exc)
            label = "放弃" if attempt == MAX_RETRIES else "下载失败"
            print(f"  {label}: {refpath}: {reason}")
    return False


# ---------- 清单比较与合并 ----------


def load_local_manifest(path: str) -> list | None:
    """读取本地清单的 files 列表；不存在、为空或损坏时返回 None（全部视为新版本）。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        # 需求：本地清单文件为空时表示所有文档都是新版本
        return None
    try:
        data = json.loads(text)
    except ValueError:
        print(f"警告: 本地清单 {path} 内容损坏，按全部文件需要更新处理")
        return None
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        return data["files"]
    return []


def collect_changes(server_files: list, local_files: list | None, local_dir: str) -> tuple[list, list]:
    """按修改时间比较服务器清单与本地清单，返回 (有变化, 无变化) 两个集合。

    本地清单无记录、修改时间不同或本地文件缺失的条目都视为有变化。
    """
    changed: list[dict] = []
    unchanged: list[dict] = []
    local_index: dict = {}
    for entry in local_files or []:
        if isinstance(entry, dict) and entry.get("refpath") is not None:
            local_index[entry["refpath"]] = entry
    for entry in server_files:
        refpath = entry.get("refpath")
        local_entry = local_index.get(refpath)
        if local_entry is not None and local_entry.get("modified") == entry.get("modified"):
            local_path = safe_local_path(local_dir, refpath)
            if local_path and os.path.isfile(local_path):
                unchanged.append(entry)
                continue
        changed.append(entry)
    return changed, unchanged


def build_final_files(server_files: list, failures: set, local_files: list | None) -> list:
    """生成最终写入本地清单的文件记录。

    需求：下载失败的文件不更新本地清单中的记录——有旧记录时保留旧记录，
    新文件（本地无记录）则不写入，两者在下次运行时都会重新尝试更新。
    """
    if not failures:
        return server_files
    local_index: dict = {}
    for entry in local_files or []:
        if isinstance(entry, dict) and entry.get("refpath") is not None:
            local_index[entry["refpath"]] = entry
    result: list[dict] = []
    for entry in server_files:
        refpath = entry.get("refpath")
        if refpath in failures:
            old = local_index.get(refpath)
            if old is not None:
                result.append(old)
            continue
        result.append(entry)
    return result


def write_manifest(path: str, manifest: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---------- 命令行入口 ----------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cptool.py",
        description="文件下载工具: 比对服务器清单与本地清单版本后增量下载文件",
        epilog=(
            "示例: python cptool.py http://localhost:8234/files.manifest kim Kd2025 "
            "--local-dir D:\\workspace\\Qoder\\nginx\\bak20260607"
        ),
    )
    parser.add_argument("manifest_url", help="文件清单地址，支持 http(s):// 与 file://")
    parser.add_argument("username", nargs="?", default=None, help="Basic Authentication 用户名（可选）")
    parser.add_argument("password", nargs="?", default=None, help="Basic Authentication 密码（可选）")
    parser.add_argument("--local-dir", dest="local_dir", default=".", help="本地保存目录（默认当前目录）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.username) != bool(args.password):
        print("错误: username 与 password 必须成对提供", file=sys.stderr)
        return 2
    auth = (args.username, args.password) if args.username else None

    local_dir = os.path.abspath(args.local_dir)
    try:
        source = ManifestSource(args.manifest_url)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    try:
        os.makedirs(local_dir, exist_ok=True)
    except OSError as exc:
        print(f"错误: 无法创建本地目录 {local_dir}: {exc}", file=sys.stderr)
        return 2

    # 1. 下载文件清单并临时保存（保持 JSON 格式，path 改为本地实际路径）
    try:
        raw = fetch_manifest_bytes(source, auth)
        manifest = json.loads(raw.decode("utf-8"))
    except (DownloadError, UnicodeDecodeError, ValueError) as exc:
        print(f"错误: 获取或解析文件清单失败: {exc}", file=sys.stderr)
        return 2
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        print("错误: 清单格式不正确（缺少 files 数组）", file=sys.stderr)
        return 2

    manifest["path"] = local_dir
    local_manifest_path = os.path.join(local_dir, LOCAL_MANIFEST_NAME)
    tmp_manifest_path = local_manifest_path + TEMP_SUFFIX
    write_manifest(tmp_manifest_path, manifest)

    # 2. 版本比较（以修改时间作为版本标识）
    local_files = load_local_manifest(local_manifest_path) or []
    changed, unchanged = collect_changes(manifest["files"], local_files, local_dir)
    print(f"清单共 {len(manifest['files'])} 个文件: {len(changed)} 个需要更新, {len(unchanged)} 个无变化")

    # 3. 依次下载有变化的文件（失败不影响后续文件）
    failures: set = set()
    for index, entry in enumerate(changed, start=1):
        refpath = entry.get("refpath")
        if not isinstance(refpath, str) or not refpath:
            print(f"[{index}/{len(changed)}] 记录缺少有效 refpath，跳过: {refpath!r}")
            failures.add(refpath)
            continue
        local_path = safe_local_path(local_dir, refpath)
        if local_path is None:
            print(f"[{index}/{len(changed)}] {refpath}: 拒绝下载（路径越界或非法）")
            failures.add(refpath)
            continue
        print(f"[{index}/{len(changed)}] {refpath}")
        if not update_file(entry, local_path, source, auth):
            failures.add(refpath)

    # 4. 用服务器清单完整替换本地清单（失败文件保留旧记录），临时清单随之删除
    manifest["files"] = build_final_files(manifest["files"], failures, local_files)
    write_manifest(tmp_manifest_path, manifest)
    os.replace(tmp_manifest_path, local_manifest_path)

    if failures:
        print(
            f"完成: 更新 {len(changed) - len(failures)}/{len(changed)}, "
            f"无变化 {len(unchanged)}, 失败 {len(failures)}（失败文件保留旧记录，下次运行将重试）"
        )
        return 1
    print(f"完成: 更新 {len(changed)}/{len(changed)}, 无变化 {len(unchanged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
