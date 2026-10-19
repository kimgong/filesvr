#!/usr/bin/env python3
"""文件下载工具（cptool）。

根据指定的文件清单下载文件：先与本地清单比较版本（以修改时间作为版本标识），
仅下载有变化的文件。下载先写入临时文件（xx.bak.tmp），完整下载并通过 MD5
校验后才重命名为目标文件；大于 50MB 的文件失败重试时支持断点续传；
大于 100MB 的文件启用分片下载（每片 100M，分片失败最多尝试 100 次）；
仅当清单记录的文件大小超过 100MB（或清单未提供大小）时才发 HEAD 请求
获取准确大小，其余文件直接下载，省去每文件一次 HEAD 往返。

命令格式::

    python cptool.py http://localhost:8234/files.manifest [username] [password]

- username/password 为可选参数，用于生成 HTTP 请求的 Basic Authorization 认证。
- 清单地址支持 http(s):// 与 file:// 协议。
- --local-dir 指定本地保存目录（默认当前目录）。

多清单同步（同时从多个清单下载到不同目标目录）::

    python cptool.py http://host:8234/alpha/files.manifest,http://host:8234/beta/files.manifest \
        kim Kd2025 --local-dir D:\\bak\\alpha,D:\\bak\\beta

多个清单地址以英文逗号分隔（与服务器的 --data-dir 约定一致），本地目录按顺序
一一对应（--local-dir 也可重复提供）；多个清单并行下载，各自维护本地清单。

执行流程（每个清单独立执行）:
1. 下载文件清单，临时保存为 file.manifest.tmp（保持 JSON 格式，仅把 path
   改为本地实际目录）。
2. 与本地 file.manifest 比较文件版本（修改时间），并按本地清单记录的 size
   简单核验本地文件大小，生成有变化的文件集合；本地清单不存在、为空、
   记录缺失、本地文件缺失或大小与记录不符时，对应文件视为有变化。
   本地清单会为每条记录附上下载完成后的实际大小（size），供下次核验。
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

# 分片下载（需求：大于 100M 的文件启用分片，每片 100M，失败最多尝试 100 次）
PIECE_THRESHOLD = 100 * 1024 * 1024   # 超过该大小的 HTTP 文件启用分片下载
PIECE_SIZE = 100 * 1024 * 1024        # 每个分片的数据大小
PIECE_MAX_ATTEMPTS = 100              # 单个分片失败后的最多尝试次数
PIECE_CONCURRENCY = 4                 # 分片并行下载数
PIECE_RETRY_DELAY_MAX = 10.0          # 分片重试退避上限（秒）


class DownloadError(Exception):
    """下载失败，message 携带失败原因。"""


# 多清单并行同步时的输出锁：避免不同任务的行输出交错撕裂
_output_lock = threading.Lock()


def report(prefix: str, message: str, stream=None) -> None:
    """线程安全的行输出；多清单并行时用 prefix 区分任务。"""
    with _output_lock:
        print(f"{prefix}{message}", file=stream or sys.stdout)


def split_csv(value: str) -> list[str]:
    """按英文逗号拆分（与服务器 --data-dir 的约定一致），去除空白段。"""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


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
            report("", f"提示: file:// 地址中的主机 “{netloc}” 已被忽略")
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


def compute_pieces(total: int) -> list[tuple[int, int]]:
    """把 [0, total) 按 PIECE_SIZE 切分为 (start, end) 闭区间列表（分片下载用）。"""
    return [(start, min(start + PIECE_SIZE, total) - 1) for start in range(0, total, PIECE_SIZE)]


def _download_piece_once(url: str, part_path: str, start: int, end: int, have: int,
                         auth: tuple[str, str] | None) -> None:
    """单次分片下载尝试：写入独立分片临时文件，支持片内断点续传。"""
    piece_len = end - start + 1
    headers = {"Range": f"bytes={start + have}-{end}"}
    resp = http_open(url, auth, headers=headers)
    with resp:
        append = getattr(resp, "status", 200) == 206 and re.match(
            rf"bytes\s+{start + have}\s*-", resp.headers.get("Content-Range", "")
        )
        # 服务器忽略 Range 返回 200 全量时：覆盖重写本片，并先丢弃分片之前的字节
        skip = 0 if append else start
        to_write = (piece_len - have) if append else piece_len
        received = 0
        try:
            with open(part_path, "ab" if append else "wb") as fh:
                while skip > 0:
                    chunk = resp.read(min(CHUNK_SIZE, skip))
                    if not chunk:
                        raise DownloadError("传输中断: 响应在分片起点之前结束")
                    skip -= len(chunk)
                while to_write > 0:
                    chunk = resp.read(min(CHUNK_SIZE, to_write))
                    if not chunk:
                        break
                    fh.write(chunk)
                    to_write -= len(chunk)
                    received += len(chunk)
        except (HTTPException, OSError) as exc:
            # 中断：保留分片临时文件供下次续传
            raise DownloadError(f"传输中断: {describe_url_error(exc)}") from exc
        if to_write > 0:
            # 连接中途断开时 read 只会提前返回空串，必须显式比较长度
            raise DownloadError(f"传输中断: 分片仅收到 {received}/{piece_len} 字节")


def _download_piece(url: str, part_path: str, start: int, end: int,
                    auth: tuple[str, str] | None, prefix: str = "") -> None:
    """下载单个分片：片内断点续传 + 失败最多尝试 PIECE_MAX_ATTEMPTS 次。"""
    piece_len = end - start + 1
    last_reason = ""
    for attempt in range(PIECE_MAX_ATTEMPTS):
        have = _existing_size(part_path)
        if have > piece_len:
            # 分片数据长度异常（例如服务器文件已变化），重新下载本片
            _remove_quiet(part_path)
            have = 0
        if have == piece_len:
            # 上次运行已完成该分片：直接跳过（分片级断点续传）
            return
        try:
            _download_piece_once(url, part_path, start, end, have, auth)
            return
        except HTTPError as exc:
            if exc.code == 416:
                # 本地分片长度已超出服务器文件，删除后重试
                exc.close()
                _remove_quiet(part_path)
                last_reason = "服务器文件长度与分片不匹配 (HTTP 416)"
            else:
                last_reason = describe_url_error(exc)
        except (DownloadError, URLError, OSError) as exc:
            last_reason = str(exc) if isinstance(exc, DownloadError) else describe_url_error(exc)
        if attempt + 1 < PIECE_MAX_ATTEMPTS:
            report(prefix, f"  分片重试 {attempt + 1}/{PIECE_MAX_ATTEMPTS} "
                           f"(bytes={start}-{end}): {last_reason}")
            time.sleep(min(RETRY_DELAY * (2 ** attempt), PIECE_RETRY_DELAY_MAX))
    raise DownloadError(
        f"分片 bytes={start}-{end} 尝试 {PIECE_MAX_ATTEMPTS} 次后仍失败: {last_reason}"
    )


def _download_http_pieces(url: str, tmp_path: str, auth: tuple[str, str] | None,
                          total: int, prefix: str = "") -> None:
    """分片下载大文件：每片独立临时文件（xx.tmp.partN），全部完成后合并。"""
    pieces = compute_pieces(total)
    report(prefix, f"  文件 {total} 字节超过 {PIECE_THRESHOLD}，启用分片下载: "
                   f"{len(pieces)} 片（每片最大 {PIECE_SIZE} 字节）")
    with ThreadPoolExecutor(max_workers=min(PIECE_CONCURRENCY, len(pieces)),
                            thread_name_prefix="cptool-piece") as pool:
        futures = [
            pool.submit(_download_piece, url, f"{tmp_path}.part{index}", start, end, auth, prefix)
            for index, (start, end) in enumerate(pieces)
        ]
        for future in futures:
            future.result()  # 片级失败（含尝试用尽）在此抛出
    # 全部分片完成：顺序合并为整体临时文件，并清理分片文件
    with open(tmp_path, "wb") as out:
        for index in range(len(pieces)):
            with open(f"{tmp_path}.part{index}", "rb") as fh:
                shutil.copyfileobj(fh, out, CHUNK_SIZE)
    for index in range(len(pieces)):
        _remove_quiet(f"{tmp_path}.part{index}")


def _download_file_once(src_path: str, tmp_path: str) -> None:
    """file:// 协议：从本地路径复制（覆盖式写入临时文件）。"""
    if not os.path.isfile(src_path):
        raise DownloadError(f"源文件不存在: {src_path}")
    try:
        shutil.copyfile(src_path, tmp_path)
    except OSError as exc:
        raise DownloadError(f"复制失败: {exc}") from exc


def update_file(entry: dict, local_path: str, source: ManifestSource, auth: tuple[str, str] | None,
                prefix: str = "") -> bool:
    """下载并校验单个文件。

    成功：临时文件已重命名为目标文件并返回 True；失败（含重试用尽）：返回 False。
    """
    tmp_path = local_path + TEMP_SUFFIX
    refpath = str(entry.get("refpath", ""))
    expected_hash = str(entry.get("hash") or "").strip().lower()
    if not expected_hash:
        report(prefix, f"  提示: 清单未提供 {refpath} 的 hash，跳过 MD5 校验")
    os.makedirs(os.path.dirname(local_path) or local_path, exist_ok=True)

    url = source.file_source_url(entry) if source.kind == "http" else None
    manifest_size = entry.get("size")
    # 需求：大于 100M 的 HTTP 文件启用分片下载（每片 100M，分片失败最多尝试 100 次）。
    # 性能：只有清单记录的文件大小超过阈值（或清单未提供 size）才发 HEAD 获取准确
    # 大小，其余文件直接普通下载，避免每文件一次 HEAD 往返
    total = None
    if url is not None and (not isinstance(manifest_size, int) or manifest_size > PIECE_THRESHOLD):
        total = _http_content_length(url, auth)
    pieced = total is not None and total > PIECE_THRESHOLD
    # 分片路径的重试预算在片级（PIECE_MAX_ATTEMPTS），文件级不再叠加尝试
    attempts = 1 if pieced else MAX_RETRIES + 1

    for attempt in range(attempts):
        if attempt:
            report(prefix, f"  重试 {attempt}/{MAX_RETRIES}: {refpath}")
            if RETRY_DELAY > 0:
                time.sleep(RETRY_DELAY * (2 ** (attempt - 1)))
        try:
            if pieced:
                _download_http_pieces(url, tmp_path, auth, total, prefix)
            elif url is not None:
                _download_http_once(url, tmp_path, auth)
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
            report(prefix, f"  已更新: {refpath} ({_existing_size(local_path)} 字节)")
            return True
        except (DownloadError, OSError) as exc:
            reason = str(exc) if isinstance(exc, DownloadError) else describe_url_error(exc)
            label = "放弃" if attempt == attempts - 1 else "下载失败"
            report(prefix, f"  {label}: {refpath}: {reason}")
    return False


# ---------- 清单比较与合并 ----------


def load_local_manifest(path: str, prefix: str = "") -> list | None:
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
        report(prefix, f"警告: 本地清单 {path} 内容损坏，按全部文件需要更新处理")
        return None
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        return data["files"]
    return []


def collect_changes(server_files: list, local_files: list | None, local_dir: str) -> tuple[list, list]:
    """按修改时间比较服务器清单与本地清单，并核验本地文件大小。

    需求：只有“修改时间一致 且 本地文件存在 且 实际大小与本地清单记录的
    size 一致”才认定为无变化；本地清单无记录、无 size 记录、修改时间不同、
    本地文件缺失或大小不符（如被截断）的条目都视为有变化（重新下载）。
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
            # 需求：版本一致还不够，还要按本地清单记录的 size 简单核验本地文件
            expected_size = local_entry.get("size")
            local_path = safe_local_path(local_dir, refpath)
            if (
                isinstance(expected_size, int) and expected_size >= 0
                and local_path is not None
                and os.path.isfile(local_path)
                and _existing_size(local_path) == expected_size
            ):
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
        description="文件下载工具: 比对服务器清单与本地清单版本后增量下载文件（支持多清单并行同步）",
        epilog=(
            "示例: python cptool.py http://localhost:8234/files.manifest kim Kd2025 "
            "--local-dir D:\\workspace\\Qoder\\nginx\\bak20260607\n"
            "多清单: python cptool.py http://host:8234/a/files.manifest,http://host:8234/b/files.manifest "
            "kim Kd2025 --local-dir D:\\bak\\a,D:\\bak\\b"
        ),
    )
    parser.add_argument(
        "manifest_url",
        help="文件清单地址（多个以英文逗号分隔），支持 http(s):// 与 file://",
    )
    parser.add_argument("username", nargs="?", default=None, help="Basic Authentication 用户名（可选）")
    parser.add_argument("password", nargs="?", default=None, help="Basic Authentication 密码（可选）")
    parser.add_argument(
        "--local-dir",
        dest="local_dirs",
        action="append",
        help="本地保存目录，多个清单按顺序逗号分隔或重复本参数（默认当前目录）",
    )
    args = parser.parse_args(argv)
    # 位置参数保持“URL [user] [pass]”的既有格式：多个清单用逗号承载，
    # 避免 argparse 对多个可变位置参数的贪婪分配歧义
    args.manifest_urls = split_csv(args.manifest_url)
    args.local_dirs = [part for value in (args.local_dirs or []) for part in split_csv(value)]
    return args


def resolve_targets(manifest_urls: list[str], local_dirs: list[str]) -> list[tuple[str, str]]:
    """把清单 URL 列表与本地目录按顺序一一配对，数量不匹配时报错。"""
    if not manifest_urls:
        raise ValueError("至少需要一个清单地址")
    if not local_dirs:
        # 未指定目录时全部使用当前目录（与单清单的默认行为一致）
        return [(url, ".") for url in manifest_urls]
    if len(local_dirs) != len(manifest_urls):
        raise ValueError(
            f"清单数量({len(manifest_urls)})与本地目录数量({len(local_dirs)})不一致，"
            "请按顺序一一对应（目录可用英文逗号分隔或重复 --local-dir 提供）"
        )
    return list(zip(manifest_urls, local_dirs))


def sync_one(manifest_url: str, local_dir_value: str, auth: tuple[str, str] | None,
             prefix: str = "") -> int:
    """同步单个清单到指定本地目录，返回该任务的退出码（供多清单并行调用）。"""
    try:
        return _sync_one(manifest_url, local_dir_value, auth, prefix)
    except Exception as exc:  # 并行场景下单清单的意外异常不应中断其他任务
        report(prefix, f"错误: 处理清单时发生未预期异常: {exc!r}", stream=sys.stderr)
        return 2


def _sync_one(manifest_url: str, local_dir_value: str, auth: tuple[str, str] | None,
              prefix: str) -> int:
    local_dir = os.path.abspath(local_dir_value)
    try:
        source = ManifestSource(manifest_url)
    except ValueError as exc:
        report(prefix, f"错误: {exc}", stream=sys.stderr)
        return 2
    try:
        os.makedirs(local_dir, exist_ok=True)
    except OSError as exc:
        report(prefix, f"错误: 无法创建本地目录 {local_dir}: {exc}", stream=sys.stderr)
        return 2

    # 1. 下载清单并临时保存（保持 JSON 格式，path 改为本地实际路径）
    try:
        raw = fetch_manifest_bytes(source, auth)
        manifest = json.loads(raw.decode("utf-8"))
    except (DownloadError, UnicodeDecodeError, ValueError) as exc:
        report(prefix, f"错误: 获取或解析文件清单失败: {exc}", stream=sys.stderr)
        return 2
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        report(prefix, "错误: 清单格式不正确（缺少 files 数组）", stream=sys.stderr)
        return 2

    manifest["path"] = local_dir
    local_manifest_path = os.path.join(local_dir, LOCAL_MANIFEST_NAME)
    tmp_manifest_path = local_manifest_path + TEMP_SUFFIX
    write_manifest(tmp_manifest_path, manifest)

    # 2. 版本比较（以修改时间为版本标识）
    local_files = load_local_manifest(local_manifest_path, prefix) or []
    changed, unchanged = collect_changes(manifest["files"], local_files, local_dir)
    report(prefix, f"清单共 {len(manifest['files'])} 个文件: {len(changed)} 个需要更新, "
                   f"{len(unchanged)} 个无变化")

    # 3. 依次下载有变化的文件（失败不影响后续文件）
    failures: set = set()
    for index, entry in enumerate(changed, start=1):
        refpath = entry.get("refpath")
        if not isinstance(refpath, str) or not refpath:
            report(prefix, f"[{index}/{len(changed)}] 记录缺少有效 refpath，跳过: {refpath!r}")
            failures.add(refpath)
            continue
        local_path = safe_local_path(local_dir, refpath)
        if local_path is None:
            report(prefix, f"[{index}/{len(changed)}] {refpath}: 拒绝下载（路径越界或非法）")
            failures.add(refpath)
            continue
        report(prefix, f"[{index}/{len(changed)}] {refpath}")
        if not update_file(entry, local_path, source, auth, prefix):
            failures.add(refpath)

    # 4. 用服务器清单完整替换本地清单（失败文件保留旧记录），临时清单随之删除；
    #    每条记录附上本地文件实际大小（size），供下次运行做简单核验
    manifest["files"] = build_final_files(manifest["files"], failures, local_files)
    for entry in manifest["files"]:
        local_path = safe_local_path(local_dir, entry.get("refpath"))
        entry["size"] = _existing_size(local_path) if local_path is not None else None
    write_manifest(tmp_manifest_path, manifest)
    os.replace(tmp_manifest_path, local_manifest_path)

    if failures:
        report(prefix, f"完成: 更新 {len(changed) - len(failures)}/{len(changed)}, "
                       f"无变化 {len(unchanged)}, 失败 {len(failures)}（失败文件保留旧记录，下次运行将重试）")
        return 1
    report(prefix, f"完成: 更新 {len(changed)}/{len(changed)}, 无变化 {len(unchanged)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口：单清单直接同步，多清单并行同步，聚合退出码。"""
    args = parse_args(argv)
    if bool(args.username) != bool(args.password):
        print("错误: username 与 password 必须成对提供", file=sys.stderr)
        return 2
    auth = (args.username, args.password) if args.username else None

    try:
        targets = resolve_targets(args.manifest_urls, args.local_dirs)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    if len(targets) == 1:
        manifest_url, local_dir = targets[0]
        return sync_one(manifest_url, local_dir, auth)

    # 多个清单: 并行同步到各自的本地目录（同时下载）
    with ThreadPoolExecutor(max_workers=min(len(targets), 8), thread_name_prefix="cptool") as pool:
        futures = [
            pool.submit(sync_one, url, local_dir, auth, f"[{index}] ")
            for index, (url, local_dir) in enumerate(targets, start=1)
        ]
        codes = [future.result() for future in futures]

    ok = sum(1 for code in codes if code == 0)
    report("", f"汇总: {len(codes)} 个清单, 成功 {ok}, 失败 {len(codes) - ok}")
    return max(codes)


if __name__ == "__main__":
    sys.exit(main())
