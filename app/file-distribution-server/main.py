#!/usr/bin/env python3
"""文件分发服务器。

监听指定目录的文件变化，生成完整的文件清单（files.manifest），
并为订阅者提供带 Basic Authentication 的 HTTP 下载服务，支持浏览器目录浏览。

启动示例（需求 1.7）::

    python main.py --user admin --pass secret --data-dir D:\\workspace\\Qoder\\nginx\\db --port 8234 --interval 60

多个数据目录以逗号分隔（需求 1.9，半角 , 与全角 ， 均可），可用 --dir-alias 指定各目录的挂载名::

    python main.py --user admin --pass secret --data-dir D:\\data1,E:\\data2 --port 8234
    python main.py --user admin --pass secret --data-dir D:\\data1,E:\\data2 --dir-alias db,backup

单目录且未指定别名时清单位于 http://host:port/files.manifest；多目录或指定别名时
每个目录挂载到 /<挂载名>/ 下，各自提供 /<挂载名>/files.manifest，根路径为挂载
索引页。未指定别名时挂载名默认取目录名，重名自动追加序号。

清单格式（需求 1.3）::

    {"path": "/var/data", "files": [
        {"refpath": "a.bak", "modified": "2026-05-15 15:00:00", "hash": "xxxx", "size": 12345},
        {"refpath": "/sub/a.bak", "modified": "2026-05-15 14:00:00", "hash": "yyyy", "size": 678}]}
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import mimetypes
import os
import re
import sys
import threading
from datetime import datetime
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

MANIFEST_NAME = "files.manifest"
CHUNK_SIZE = 1024 * 1024
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
REALM = "FileDistributionServer"

log = logging.getLogger("filesvr")


def format_local_time(timestamp: float) -> str:
    """把 mtime 格式化为清单使用的本地时间字符串。"""
    return datetime.fromtimestamp(timestamp).strftime(TIME_FORMAT)


def human_size(num: float) -> str:
    """目录浏览用的可读大小。"""
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class ManifestBuilder:
    """扫描数据目录并维护 files.manifest。

    扫描时排除数据目录根下的清单文件自身（需求 1.2）；
    hash 使用 md5（需求 1.4），并以 (mtime, size) 为键缓存，重扫时只对变化文件重算。
    """

    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.manifest_path = os.path.join(self.data_dir, MANIFEST_NAME)
        self._io_lock = threading.Lock()
        self._hash_cache: dict[str, tuple[int, int, str]] = {}
        self.file_count = 0

    def scan(self) -> dict:
        """遍历数据目录，返回清单字典（不写文件）。"""
        files: list[dict] = []
        new_cache: dict[str, tuple[int, int, str]] = {}
        for root, dirs, names in os.walk(self.data_dir):
            dirs.sort()
            for name in sorted(names):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, self.data_dir)
                if rel == MANIFEST_NAME:
                    continue
                try:
                    st = os.stat(full)
                except OSError as exc:
                    log.warning("跳过无法访问的文件: %s (%s)", rel, exc)
                    continue
                digest = self._md5(rel, full, st, new_cache)
                if digest is None:
                    continue
                refpath = rel.replace(os.sep, "/")
                if "/" in refpath:
                    # 参照需求 1.3 示例：子目录内文件以 / 开头
                    refpath = "/" + refpath
                files.append(
                    {
                        "refpath": refpath,
                        "modified": format_local_time(st.st_mtime),
                        "hash": digest,
                        # 记录文件大小：供下载工具预判是否需要分片，避免多余的 HEAD 请求
                        "size": st.st_size,
                    }
                )
        files.sort(key=lambda item: item["refpath"])
        self._hash_cache = new_cache
        self.file_count = len(files)
        return {"path": self.data_dir, "files": files}

    def _md5(self, key: str, full: str, st: os.stat_result, new_cache: dict) -> str | None:
        cached = self._hash_cache.get(key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            new_cache[key] = cached
            return cached[2]
        digest = hashlib.md5()
        try:
            with open(full, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            log.warning("跳过无法读取的文件: %s (%s)", key, exc)
            return None
        value = digest.hexdigest()
        new_cache[key] = (st.st_mtime_ns, st.st_size, value)
        return value

    def refresh(self) -> bool:
        """重新扫描并写入清单；返回清单内容是否发生变化。"""
        text = json.dumps(self.scan(), ensure_ascii=False, indent=2) + "\n"
        with self._io_lock:
            current = None
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as fh:
                    current = fh.read()
            except (OSError, UnicodeDecodeError):
                current = None
            if current == text:
                return False
            with open(self.manifest_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return True

    def read_manifest(self) -> tuple[bytes, float] | None:
        """在锁保护下读取清单文件，返回 (内容, mtime)。"""
        with self._io_lock:
            try:
                with open(self.manifest_path, "rb") as fh:
                    data = fh.read()
                    mtime = os.fstat(fh.fileno()).st_mtime
                return data, mtime
            except OSError:
                return None


class FileRequestHandler(BaseHTTPRequestHandler):
    """处理订阅者的下载/浏览请求。"""

    server: "FileDistributionServer"
    server_version = "FileDistributionServer/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 60
    # 小响应（响应头、清单）立即发出，避免 Nagle 合包延迟
    disable_nagle_algorithm = True

    # ---------- 入口 ----------

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            if not self._authorized():
                self._send_auth_challenge()
                return
            url_path = self._request_path()
            if "\x00" in url_path:
                self._send_plain(HTTPStatus.BAD_REQUEST, "路径中包含非法字符")
                return
            parts = [p for p in url_path.split("/") if p and p != "."]
            if ".." in parts:
                self._send_plain(HTTPStatus.FORBIDDEN, "禁止访问数据目录之外的路径")
                return
            server = self.server
            if server.single_root is not None:
                # 单目录：挂在根路径，保持 http://host/files.manifest 的既有契约
                root, builder = server.single_root
                rel_parts = parts
            else:
                # 多目录（需求 1.9）：首个路径段是挂载点，根路径列出全部挂载
                if not parts:
                    self._mount_index_page()
                    return
                entry = server.mount_map.get(parts[0])
                if entry is None:
                    self._send_plain(HTTPStatus.NOT_FOUND, f"挂载点不存在: /{parts[0]}/")
                    return
                root, builder = entry
                rel_parts = parts[1:]
            fs_path = self._safe_join(root, rel_parts)
            if fs_path is None:
                self._send_plain(HTTPStatus.FORBIDDEN, "禁止访问数据目录之外的路径")
                return
            if fs_path == root or os.path.isdir(fs_path):
                self._directory_listing(fs_path, parts)
            elif os.path.isfile(fs_path):
                self._serve_file(fs_path, builder)
            else:
                self._send_plain(HTTPStatus.NOT_FOUND, "文件或目录不存在")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            log.exception("处理请求 %s 时发生内部错误", self.path)
            try:
                self._send_plain(HTTPStatus.INTERNAL_SERVER_ERROR, "服务器内部错误")
            except Exception:
                pass

    # ---------- 认证（需求 1.1） ----------

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "basic" or not value.strip():
            return False
        try:
            raw = base64.b64decode(value.strip(), validate=True)
        except ValueError:
            return False
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw.decode("latin-1", "replace")
        expected = f"{self.server.username}:{self.server.password}".encode("utf-8")
        return hmac.compare_digest(decoded.encode("utf-8"), expected)

    def _send_auth_challenge(self) -> None:
        self._send_bytes(
            HTTPStatus.UNAUTHORIZED,
            "401 Unauthorized: 访问需要 Basic Authentication 认证".encode("utf-8"),
            "text/plain; charset=utf-8",
            extra=[("WWW-Authenticate", f'Basic realm="{REALM}", charset="UTF-8"')],
        )

    # ---------- 路径解析（需求 1.8，含 URL 解码） ----------

    def _request_path(self) -> str:
        raw = urlsplit(self.path).path
        # 请求行按 latin-1 解码：还原原始字节后按 UTF-8 解码，
        # 兼容客户端未做百分号编码、直接发送多字节字符的情况
        try:
            raw = raw.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        return unquote(raw)

    @staticmethod
    def _safe_join(root: str, rel_parts: list[str]) -> str | None:
        """把挂载目录内的相对路径映射为文件系统路径，越界返回 None。"""
        fs_path = os.path.normpath(os.path.join(root, *rel_parts))
        if fs_path != root and not fs_path.startswith(root + os.sep):
            return None
        # 追加一层符号链接解析，防止目录内链接指向数据目录之外
        real_fs = os.path.realpath(fs_path)
        real_root = os.path.realpath(root)
        if real_fs != real_root and not real_fs.startswith(real_root + os.sep):
            return None
        return fs_path

    # ---------- 目录浏览（需求 1.6） ----------

    _LISTING_CSS = """
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 2rem 4%; max-width: 56rem; }
h1 { font-size: 1.15rem; border-bottom: 2px solid #e8e8e8; padding-bottom: .5rem; }
table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #f0f0f0; word-break: break-all; }
th { color: #666; font-weight: 600; }
td.num { text-align: right; white-space: nowrap; }
a { color: #0a58ca; text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: none; border-top: 1px solid #e8e8e8; margin: 1.5rem 0 .5rem; }
footer { color: #888; font-size: .8rem; }
"""

    def _directory_listing(self, fs_path: str, parts: list[str]) -> None:
        shown = "/" + "/".join(parts)
        if not shown.endswith("/"):
            shown += "/"
        try:
            names = os.listdir(fs_path)
        except OSError as exc:
            self._send_plain(HTTPStatus.FORBIDDEN, f"无法读取目录: {exc}")
            return
        dirs, file_names = [], []
        for name in names:
            if os.path.isdir(os.path.join(fs_path, name)):
                dirs.append(name)
            else:
                file_names.append(name)
        dirs.sort(key=str.casefold)
        file_names.sort(key=str.casefold)

        rows = []
        if parts:
            parent = "/" + "/".join(parts[:-1])
            if not parent.endswith("/"):
                parent += "/"
            rows.append(
                f'<tr><td><a href="{html.escape(quote(parent, safe="/"))}">../</a></td>'
                '<td class="num">-</td><td>-</td></tr>'
            )
        for name in dirs:
            href = quote(shown + name, safe="/") + "/"
            mtime = self._try_mtime(os.path.join(fs_path, name))
            rows.append(
                f'<tr><td><a href="{html.escape(href)}">{html.escape(name)}/</a></td>'
                f'<td class="num">-</td><td>{mtime}</td></tr>'
            )
        for name in file_names:
            full = os.path.join(fs_path, name)
            size, mtime = "-", self._try_mtime(full)
            try:
                st = os.stat(full)
                size = human_size(st.st_size)
            except OSError:
                pass
            href = quote(shown + name, safe="/")
            rows.append(
                f'<tr><td><a href="{html.escape(href)}">{html.escape(name)}</a></td>'
                f'<td class="num">{size}</td><td>{mtime}</td></tr>'
            )

        title = html.escape(shown)
        page = (
            "<!DOCTYPE html>\n"
            '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            f"<title>文件分发服务器 - {title}</title>\n<style>{self._LISTING_CSS}</style>\n"
            "</head>\n<body>\n"
            f"<h1>目录: {title}</h1>\n"
            "<table>\n<thead><tr><th>名称</th><th>大小</th><th>修改时间</th></tr></thead>\n"
            f"<tbody>\n{''.join(rows)}</tbody>\n</table>\n<hr>\n"
            f"<footer>{self.server_version}</footer>\n</body>\n</html>\n"
        )
        self._send_bytes(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

    def _mount_index_page(self) -> None:
        """多目录模式下的根路径页面：列出全部挂载点（需求 1.9）。"""
        rows = []
        for mount, data_dir, builder in self.server.mounts:
            href = quote("/" + mount, safe="/") + "/"
            rows.append(
                f'<tr><td><a href="{html.escape(href)}">{html.escape(mount)}/</a></td>'
                f'<td class="num">{builder.file_count}</td><td>{html.escape(data_dir)}</td></tr>'
            )
        page = (
            "<!DOCTYPE html>\n"
            '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            "<title>文件分发服务器 - 数据目录</title>\n"
            f"<style>{self._LISTING_CSS}</style>\n</head>\n<body>\n"
            "<h1>数据目录</h1>\n"
            "<table>\n<thead><tr><th>挂载点</th><th>文件数</th><th>数据目录</th></tr></thead>\n"
            f"<tbody>\n{''.join(rows)}</tbody>\n</table>\n<hr>\n"
            f"<footer>{self.server_version}</footer>\n</body>\n</html>\n"
        )
        self._send_bytes(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")

    @staticmethod
    def _try_mtime(fs_path: str) -> str:
        try:
            return format_local_time(os.stat(fs_path).st_mtime)
        except OSError:
            return "-"

    # ---------- 文件下载 ----------

    def _serve_file(self, fs_path: str, builder: ManifestBuilder) -> None:
        manifest_path = builder.manifest_path
        if os.path.normcase(os.path.abspath(fs_path)) == os.path.normcase(manifest_path):
            snapshot = builder.read_manifest()
            if snapshot is None:
                self._send_plain(HTTPStatus.NOT_FOUND, "清单文件尚未生成")
                return
            data, mtime = snapshot
            self._send_content(io.BytesIO(data), len(data), mtime, "application/json; charset=utf-8")
            return
        try:
            fh = open(fs_path, "rb")
        except (OSError, ValueError):
            self._send_plain(HTTPStatus.NOT_FOUND, "文件不存在或无法读取")
            return
        with fh:
            st = os.fstat(fh.fileno())
            self._send_content(fh, st.st_size, st.st_mtime, self._guess_type(fs_path))

    def _send_content(self, source, size: int, mtime: float, content_type: str) -> None:
        """流式发送文件内容，支持单区间 Range 请求（供下载工具断点续传）。

        以 1MB 大块读写循环传输：200MB 回环实测比 64KB 小块快约 70%，且比
        Windows 上 CPython socket.sendfile（TransmitFile 分块同步等待）快约
        3 倍，故统一走大块用户态拷贝。
        """
        start, length, status = 0, size, HTTPStatus.OK
        extra: list[tuple[str, str]] = [("Accept-Ranges", "bytes")]
        range_header = self.headers.get("Range")
        if range_header:
            parsed = self._parse_range(range_header, size)
            if parsed is False:
                self._send_bytes(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "416 Requested Range Not Satisfiable\n".encode("utf-8"),
                    "text/plain; charset=utf-8",
                    extra=[("Content-Range", f"bytes */{size}")],
                )
                return
            if parsed is not None:
                start, end = parsed
                length = end - start + 1
                status = HTTPStatus.PARTIAL_CONTENT
                extra.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Last-Modified", formatdate(mtime, usegmt=True))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            source.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            log.debug("客户端中断下载: %s", exc)

    @staticmethod
    def _parse_range(header: str, size: int):
        """解析单区间 Range 头。返回 (start, end)、None（忽略按 200 全量）或 False（不可满足）。"""
        match = re.fullmatch(r"bytes\s*=\s*(\d*)\s*-\s*(\d*)", header.strip())
        if not match:
            return None
        first, last = match.group(1), match.group(2)
        if first == "" and last == "":
            return None
        if first == "":
            suffix = int(last)
            if suffix == 0 or size == 0:
                return False
            return max(0, size - suffix), size - 1
        start = int(first)
        if start >= size:
            return False
        if last == "":
            return start, size - 1
        end = int(last)
        if end < start:
            return None
        return start, min(end, size - 1)

    @staticmethod
    def _guess_type(fs_path: str) -> str:
        name = os.path.basename(fs_path)
        if name == MANIFEST_NAME or name.endswith(".json"):
            return "application/json; charset=utf-8"
        ctype, _ = mimetypes.guess_type(name)
        if not ctype:
            return "application/octet-stream"
        if ctype.startswith("text/"):
            return ctype + "; charset=utf-8"
        return ctype

    # ---------- 响应辅助 ----------

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra or []:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def _send_plain(self, status: HTTPStatus, message: str) -> None:
        body = f"{status.value} {status.phrase}: {message}\n".encode("utf-8")
        self._send_bytes(status, body, "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.client_address[0], fmt % args)


class FileDistributionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        mounts: list[tuple[str, str, ManifestBuilder]],
        username: str,
        password: str,
    ) -> None:
        super().__init__(address, FileRequestHandler)
        self.mounts = mounts
        self.mount_map = {name: (data_dir, builder) for name, data_dir, builder in mounts}
        # 单目录挂载名为空串（挂在根路径），保持 /files.manifest 的既有契约
        self.single_root = self.mount_map.get("") if len(mounts) == 1 and mounts[0][0] == "" else None
        self.username = username
        self.password = password

    def handle_error(self, request, client_address) -> None:
        log.warning("连接 %s 出错: %s", client_address, sys.exc_info()[1])


def split_comma_list(value: str) -> list[str]:
    """按逗号拆分参数值（需求 1.9），兼容半角 , 与全角 ，，去除空白段。"""
    return [item.strip() for item in re.split(r"[,，]", value) if item.strip()]


def split_data_dirs(value: str) -> list[str]:
    """拆分 --data-dir 的多个数据目录，并按 normcase 去重。"""
    dirs: list[str] = []
    seen: set[str] = set()
    for item in split_comma_list(value):
        key = os.path.normcase(os.path.abspath(item))
        if key in seen:
            log.warning("忽略重复的数据目录: %s", item)
            continue
        seen.add(key)
        dirs.append(item)
    return dirs


def _mount_name(data_dir: str) -> str:
    """用目录名作为 URL 挂载名；盘符根等无名目录回退为盘符或 data。"""
    data_dir = os.path.abspath(data_dir)
    name = os.path.basename(os.path.normpath(data_dir))
    if not name:
        drive = os.path.splitdrive(data_dir)[0]
        name = drive[:-1] if len(drive) == 2 and drive[1] == ":" else "data"
    return name


def _validate_alias(alias: str, index: int) -> str:
    """校验用户指定的挂载别名（去除首尾空白），非法时抛 ValueError。"""
    alias = alias.strip()
    if not alias:
        raise ValueError(f"第 {index + 1} 个别名不能为空")
    if alias in (".", ".."):
        raise ValueError(f"别名不能为 '.' 或 '..': {alias}")
    for ch in ("/", "\\", "\x00"):
        if ch in alias:
            raise ValueError(f"别名不能包含路径分隔符: {alias}")
    return alias


def build_mounts(
    data_dirs: list[str], aliases: list[str] | None = None
) -> list[tuple[str, str, ManifestBuilder]]:
    """为数据目录分配挂载点并创建各自的清单构建器。

    未指定别名时：单目录挂载到根路径（兼容需求 1.7 的 /files.manifest 访问方式）；
    多目录按目录名挂载到 /<目录名>/，重名自动追加 -2、-3 序号（需求 1.9）。

    指定别名（--dir-alias，与 --data-dir 按位置一一对应）时：每个目录挂载到
    /<别名>/（含单目录），别名重复或非法视为配置错误，直接报错。
    """
    if aliases is not None:
        if len(aliases) != len(data_dirs):
            raise ValueError(
                f"--dir-alias 数量({len(aliases)})与 --data-dir 数量({len(data_dirs)})不一致"
            )
        normalized: list[str] = []
        seen: set[str] = set()
        for index, alias in enumerate(aliases):
            alias = _validate_alias(alias, index)
            if alias in seen:
                raise ValueError(f"别名重复: {alias}")
            seen.add(alias)
            normalized.append(alias)
        return [
            (alias, os.path.abspath(data_dir), ManifestBuilder(data_dir))
            for alias, data_dir in zip(normalized, data_dirs)
        ]

    if len(data_dirs) == 1:
        data_dir = os.path.abspath(data_dirs[0])
        return [("", data_dir, ManifestBuilder(data_dir))]

    mounts: list[tuple[str, str, ManifestBuilder]] = []
    used: set[str] = set()
    for data_dir in data_dirs:
        data_dir = os.path.abspath(data_dir)
        name = _mount_name(data_dir)
        candidate, serial = name, 2
        while candidate in used:
            candidate = f"{name}-{serial}"
            serial += 1
        if candidate != name:
            log.warning("挂载名 %s 已被占用, %s 改用 /%s/", name, data_dir, candidate)
        used.add(candidate)
        mounts.append((candidate, data_dir, ManifestBuilder(data_dir)))
    return mounts


def create_server(
    address: tuple[str, int],
    data_dirs: list[str],
    username: str,
    password: str,
    aliases: list[str] | None = None,
) -> FileDistributionServer:
    """按数据目录列表构建服务器实例（每个目录一个 ManifestBuilder）。"""
    return FileDistributionServer(address, build_mounts(data_dirs, aliases), username, password)


def monitor_loop(builders: list[ManifestBuilder], interval: float, stop: threading.Event) -> None:
    """周期性重扫全部数据目录，检测文件变化并更新清单。"""
    while not stop.wait(interval):
        for builder in builders:
            try:
                if builder.refresh():
                    log.info(
                        "检测到目录变化，清单已更新: %s（当前 %d 个文件）",
                        builder.data_dir,
                        builder.file_count,
                    )
            except Exception:
                log.exception("刷新文件清单失败: %s", builder.manifest_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "文件分发服务器：监听目录变化生成 files.manifest 清单，"
            "并为订阅者提供带 Basic 认证的 HTTP 下载与目录浏览服务"
        ),
        epilog=(
            "示例: python main.py --user admin --pass secret "
            "--data-dir D:\\workspace\\Qoder\\nginx\\db --port 8234 --interval 60\n"
            "多目录: python main.py --user admin --pass secret "
            "--data-dir D:\\data1,E:\\data2 --port 8234\n"
            "指定挂载名: python main.py --user admin --pass secret "
            "--data-dir D:\\data1,E:\\data2 --dir-alias db,backup"
        ),
    )
    parser.add_argument("--user", required=True, help="Basic Authentication 用户名")
    parser.add_argument("--pass", dest="password", required=True, help="Basic Authentication 密码")
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        required=True,
        help="要分发文件的数据目录，多个目录以逗号分隔（半角 , 或全角 ， 均可）",
    )
    parser.add_argument(
        "--dir-alias",
        dest="dir_alias",
        default=None,
        help="各目录的挂载名，多个以逗号分隔（半角或全角均可），与 --data-dir 按位置一一对应（可选，"
        "默认取目录名，重名自动加序号）",
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP 监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8234, help="HTTP 监听端口（默认 8234）")
    parser.add_argument("--interval", type=float, default=60.0, help="目录重扫间隔秒数（默认 60）")
    return parser.parse_args(argv)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt=TIME_FORMAT,
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)
    data_dirs = split_data_dirs(args.data_dir)
    if not data_dirs:
        log.error("--data-dir 为空，多个目录请用逗号分隔，例如: --data-dir D:\\dir1,D:\\dir2")
        return 2
    for data_dir in data_dirs:
        if not os.path.isdir(data_dir):
            log.error("数据目录不存在: %s", os.path.abspath(data_dir))
            return 2

    interval = args.interval
    if interval <= 0:
        log.warning("--interval 必须大于 0，已改为仅在启动时扫描一次")
        interval = None

    aliases = None
    if args.dir_alias and args.dir_alias.strip():
        aliases = split_comma_list(args.dir_alias)

    try:
        mounts = build_mounts(data_dirs, aliases)
    except ValueError as exc:
        log.error("挂载配置错误: %s", exc)
        return 2
    builders = [builder for _, _, builder in mounts]
    for _, _, builder in mounts:
        try:
            builder.refresh()
        except OSError as exc:
            log.error("生成文件清单失败: %s (%s)", builder.manifest_path, exc)
            return 2
        log.info("文件清单已生成: %d 个文件, 输出: %s", builder.file_count, builder.manifest_path)

    try:
        server = FileDistributionServer((args.host, args.port), mounts, args.user, args.password)
    except OSError as exc:
        log.error("无法监听 %s:%d: %s", args.host, args.port, exc)
        return 2

    stop = threading.Event()
    if interval is not None:
        worker = threading.Thread(
            target=monitor_loop, args=(builders, interval, stop), daemon=True, name="manifest-monitor"
        )
        worker.start()
        log.info("目录监控已启动, 每 %.1f 秒扫描一次（%d 个目录）", interval, len(builders))
    for name, data_dir, _ in mounts:
        if name:
            log.info("%s:%s", name, data_dir)
    log.info("HTTP 服务已启动: http://%s:%d/ (认证用户: %s)", args.host, args.port, args.user)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C, 服务退出")
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
