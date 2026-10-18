"""文件下载工具（cptool）自动化测试。标准库 unittest，无第三方依赖。

运行方式::

    python -m unittest discover -s tests -v
    python tests/test_cptool.py

HTTP 集成测试复用同一工作区的 file-distribution-server（真实服务端），
不存在时自动跳过；断点续传/重试另用可控故障的独立测试服务器。
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import cptool  # noqa: E402

SERVER_APP_ROOT = os.path.join(os.path.dirname(PROJECT_ROOT), "file-distribution-server")
HAS_SERVER_APP = os.path.isfile(os.path.join(SERVER_APP_ROOT, "main.py"))

USER = "kim"
PASSWORD = "Kd2025"


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


class ToolRunMixin:
    """运行 cptool.main 并捕获输出。"""

    local_dir: str

    def run_tool(self, url, user=None, password=None, local_dir=None):
        argv = [url]
        if user is not None:
            argv.append(user)
        if password is not None:
            argv.append(password)
        argv.extend(["--local-dir", local_dir or self.local_dir])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = cptool.main(argv)
        return code, output.getvalue()


class ResetDefaultsMixin:
    """恢复被各用例调整过的模块常量。"""

    def reset_tool_defaults(self):
        cptool.MAX_RETRIES = 3
        cptool.RETRY_DELAY = 0
        cptool.RESUME_THRESHOLD = 50 * 1024 * 1024


# ---------- 单元测试 ----------


class HelperUnitTests(unittest.TestCase):
    def test_file_source_url_replaces_backslash(self):
        source = cptool.ManifestSource("http://localhost:8234/files.manifest")
        self.assertEqual(
            source.file_source_url({"refpath": "dir\\x.bin"}),
            "http://localhost:8234/dir/x.bin",
        )

    def test_file_source_url_quotes_special_chars(self):
        source = cptool.ManifestSource("http://localhost:8234/files.manifest")
        url = source.file_source_url({"refpath": "/测 试.txt"})
        self.assertEqual(url, "http://localhost:8234/" + quote("测 试.txt", safe="/"))

    def test_file_source_url_under_subpath(self):
        source = cptool.ManifestSource("http://host:9000/d/e/files.manifest")
        self.assertEqual(source.file_source_url({"refpath": "a.bak"}), "http://host:9000/d/e/a.bak")

    def test_safe_local_path(self):
        with tempfile.TemporaryDirectory(prefix="cptool-unit-") as tmp:
            local = os.path.abspath(tmp)
            self.assertEqual(cptool.safe_local_path(local, "a.bak"), os.path.join(local, "a.bak"))
            self.assertEqual(
                cptool.safe_local_path(local, "/sub/a.bak"), os.path.join(local, "sub", "a.bak")
            )
            self.assertEqual(
                cptool.safe_local_path(local, "sub\\a.bak"), os.path.join(local, "sub", "a.bak")
            )
            self.assertIsNone(cptool.safe_local_path(local, "../x"))
            self.assertIsNone(cptool.safe_local_path(local, "/a/../../x"))
            self.assertIsNone(cptool.safe_local_path(local, ""))
            self.assertIsNone(cptool.safe_local_path(local, None))
            if os.name == "nt":
                self.assertIsNone(cptool.safe_local_path(local, "C:/evil.txt"))
                self.assertIsNone(cptool.safe_local_path(local, "C:\\evil.txt"))

    def test_collect_changes(self):
        server = [
            {"refpath": "a", "modified": "t2"},
            {"refpath": "b", "modified": "t1"},
            {"refpath": "c", "modified": "t9"},
        ]
        local = [
            {"refpath": "a", "modified": "t1"},
            {"refpath": "b", "modified": "t1"},
        ]
        with tempfile.TemporaryDirectory(prefix="cptool-unit-") as tmp:
            local_dir = os.path.abspath(tmp)
            b_path = os.path.join(local_dir, "b")
            with open(b_path, "wb") as fh:
                fh.write(b"b")
            # b: 修改时间一致且本地文件存在 → 无变化; a: 修改时间不同; c: 本地无记录
            changed, unchanged = cptool.collect_changes(server, local, local_dir)
            self.assertEqual({e["refpath"] for e in changed}, {"a", "c"})
            self.assertEqual([e["refpath"] for e in unchanged], ["b"])
            # 本地文件丢失（清单记录仍在）→ 视为有变化
            os.remove(b_path)
            changed, unchanged = cptool.collect_changes(server, local, local_dir)
            self.assertEqual({e["refpath"] for e in changed}, {"a", "b", "c"})
            self.assertEqual(unchanged, [])

    def test_build_final_files(self):
        server = [
            {"refpath": "a", "modified": "t2", "hash": "ha2"},
            {"refpath": "b", "modified": "t2", "hash": "hb2"},
            {"refpath": "c", "modified": "t2", "hash": "hc2"},
        ]
        local = [{"refpath": "b", "modified": "t1", "hash": "hb1"}]
        result = cptool.build_final_files(server, {"b", "c"}, local)
        # 失败的 b 保留本地旧记录；失败的新文件 c 不写入
        self.assertEqual(result, [server[0], local[0]])
        self.assertEqual(cptool.build_final_files(server, set(), local), server)

    def test_parse_args_positional_and_option(self):
        args = cptool.parse_args(
            ["http://localhost:8234/files.manifest", "kim", "Kd2025", "--local-dir", "D:/bak"]
        )
        self.assertEqual(args.manifest_url, "http://localhost:8234/files.manifest")
        self.assertEqual(args.username, "kim")
        self.assertEqual(args.password, "Kd2025")
        self.assertEqual(args.local_dir, "D:/bak")


# ---------- file:// 协议 ----------


class FileProtocolTests(ResetDefaultsMixin, ToolRunMixin, unittest.TestCase):
    def setUp(self):
        self.reset_tool_defaults()
        self.tmp = tempfile.mkdtemp(prefix="cptool-test-")
        # 目录名带空格，顺带覆盖 file:// 地址的百分号编解码
        self.server_dir = os.path.join(self.tmp, "srv data")
        self.local_dir = os.path.join(self.tmp, "local")
        os.makedirs(self.server_dir)
        os.makedirs(self.local_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def refpath(rel):
        ref = rel.replace(os.sep, "/")
        return "/" + ref if "/" in ref else ref

    def write_server_file(self, rel, data, modified):
        full = os.path.join(self.server_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
        return {"refpath": self.refpath(rel), "modified": modified, "hash": md5_hex(data)}

    def write_manifest(self, entries):
        write_json(os.path.join(self.server_dir, "files.manifest"), {"path": self.server_dir, "files": entries})

    def manifest_url(self):
        path = os.path.join(self.server_dir, "files.manifest").replace(os.sep, "/")
        return "file:///" + quote(path, safe="/:")

    def local_manifest(self):
        return read_json(os.path.join(self.local_dir, "file.manifest"))

    def test_full_download_and_local_manifest(self):
        top = self.write_server_file("a.bak", b"hello-" * 4, "2026-05-15 15:00:00")
        nested = self.write_server_file(os.path.join("sub", "b.dat"), b"nested", "2026-05-15 14:00:00")
        self.write_manifest([top, nested])

        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 0, output)
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "a.bak")), b"hello-" * 4)
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "sub", "b.dat")), b"nested")

        manifest = self.local_manifest()
        self.assertEqual(manifest["path"], os.path.abspath(self.local_dir))
        self.assertEqual(len(manifest["files"]), 2)
        # 临时清单已被替换为正式清单
        self.assertFalse(os.path.exists(os.path.join(self.local_dir, "file.manifest.tmp")))
        # 无残留的文件临时文件
        self.assertFalse(os.path.exists(os.path.join(self.local_dir, "a.bak.tmp")))

    def test_second_run_downloads_nothing(self):
        entry = self.write_server_file("a.bak", b"v1", "2026-05-15 15:00:00")
        self.write_manifest([entry])
        self.run_tool(self.manifest_url())
        target = os.path.join(self.local_dir, "a.bak")
        before = os.stat(target).st_mtime_ns

        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 0, output)
        self.assertIn("0 个需要更新", output)
        self.assertEqual(os.stat(target).st_mtime_ns, before)

    def test_only_changed_file_redownloaded(self):
        a = self.write_server_file("a.bak", b"a-v1", "2026-05-15 15:00:00")
        b = self.write_server_file(os.path.join("sub", "b.dat"), b"b-v1", "2026-05-15 14:00:00")
        self.write_manifest([a, b])
        self.run_tool(self.manifest_url())
        a_local = os.path.join(self.local_dir, "a.bak")
        a_mtime = os.stat(a_local).st_mtime_ns

        # 服务器上仅 b.dat 有新版本（内容与修改时间都更新）
        b_new = self.write_server_file(os.path.join("sub", "b.dat"), b"b-v2", "2026-05-16 14:00:00")
        self.write_manifest([a, b_new])
        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 0, output)
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "sub", "b.dat")), b"b-v2")
        self.assertEqual(os.stat(a_local).st_mtime_ns, a_mtime, "无变化文件不应被重新下载")
        manifest = self.local_manifest()
        by_ref = {f["refpath"]: f for f in manifest["files"]}
        self.assertEqual(by_ref["/sub/b.dat"]["hash"], md5_hex(b"b-v2"))

    def test_empty_local_manifest_treats_all_as_new(self):
        entry = self.write_server_file("a.bak", b"data", "2026-05-15 15:00:00")
        self.write_manifest([entry])
        # 需求：本地清单文件为空时表示所有文档都是新版本
        with open(os.path.join(self.local_dir, "file.manifest"), "w", encoding="utf-8") as fh:
            fh.write("")

        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 0, output)
        self.assertTrue(os.path.isfile(os.path.join(self.local_dir, "a.bak")))
        self.assertIn("1 个需要更新", output)

    def test_hash_mismatch_keeps_old_record_and_prints_reason(self):
        a = self.write_server_file("a.bak", b"a-v1", "2026-05-15 15:00:00")
        self.write_manifest([a])
        self.run_tool(self.manifest_url())

        # 服务器发布新版本，但清单中的 hash 是错的
        bad = self.write_server_file("a.bak", b"a-v2", "2026-05-16 15:00:00")
        bad["hash"] = "0" * 32
        self.write_manifest([bad])
        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 1, output)
        self.assertIn("MD5 校验失败", output)
        # 重试了至少 3 次
        self.assertIn("重试 3/3", output)
        # 本地文件与本地清单记录都保持旧版本
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "a.bak")), b"a-v1")
        self.assertEqual(self.local_manifest()["files"], [a])
        # 校验失败后临时文件被清理
        self.assertFalse(os.path.exists(os.path.join(self.local_dir, "a.bak.tmp")))

    def test_failed_new_file_not_recorded_then_succeeds(self):
        a = self.write_server_file("a.bak", b"a", "2026-05-15 15:00:00")
        self.write_manifest([a])
        self.run_tool(self.manifest_url())

        bad_new = self.write_server_file("new.bin", b"new", "2026-05-16 15:00:00")
        bad_new["hash"] = "0" * 32
        self.write_manifest([a, bad_new])
        code, _ = self.run_tool(self.manifest_url())
        self.assertEqual(code, 1)
        # 失败的新文件不写入本地清单（下次运行会重新尝试）
        self.assertFalse(os.path.isfile(os.path.join(self.local_dir, "new.bin")))
        self.assertEqual({f["refpath"] for f in self.local_manifest()["files"]}, {"a.bak"})

        good_new = dict(bad_new, hash=md5_hex(b"new"))
        self.write_manifest([a, good_new])
        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 0, output)
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "new.bin")), b"new")

    def test_missing_local_file_is_redownloaded(self):
        entry = self.write_server_file("a.bak", b"data", "2026-05-15 15:00:00")
        self.write_manifest([entry])
        self.run_tool(self.manifest_url())
        os.remove(os.path.join(self.local_dir, "a.bak"))

        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 0, output)
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "a.bak")), b"data")

    def test_backslash_refpath_uses_local_separators(self):
        # 需求：清单 refpath 中的 "\" 拼路径时替换为 "/"；保存本地时用系统分隔符
        data = b"backslash content"
        source = os.path.join(self.server_dir, "dir", "x.bin")
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with open(source, "wb") as fh:
            fh.write(data)
        entries = [
            {"refpath": "dir\\x.bin", "modified": "2026-05-15 15:00:00", "hash": md5_hex(data)},
        ]
        self.write_manifest(entries)

        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 0, output)
        expected = os.path.join(self.local_dir, "dir", "x.bin")
        self.assertEqual(read_bytes(expected), data)

    def test_traversal_refpath_rejected(self):
        evil_content = b"escaped"
        with open(os.path.join(self.tmp, "escape.txt"), "wb") as fh:
            fh.write(evil_content)
        entries = [
            {"refpath": "../escape.txt", "modified": "2026-05-15 15:00:00", "hash": md5_hex(evil_content)},
        ]
        self.write_manifest(entries)

        code, output = self.run_tool(self.manifest_url())
        self.assertEqual(code, 1, output)
        self.assertIn("拒绝下载", output)
        # 本地目录内与本地清单都不包含该记录
        self.assertFalse(os.path.exists(os.path.join(self.local_dir, "escape.txt")))
        self.assertEqual(self.local_manifest()["files"], [])

    def test_manifest_not_found_returns_2(self):
        code, output = self.run_tool("file:///" + quote(
            os.path.join(self.server_dir, "no-such.manifest").replace(os.sep, "/"), safe="/:"
        ))
        self.assertEqual(code, 2)
        self.assertIn("清单文件不存在", output)

    def test_username_without_password_returns_2(self):
        entry = self.write_server_file("a.bak", b"data", "2026-05-15 15:00:00")
        self.write_manifest([entry])
        code, output = self.run_tool(self.manifest_url(), user=USER)
        self.assertEqual(code, 2)
        self.assertIn("成对提供", output)


# ---------- http(s) 集成（真实 file-distribution-server 服务端） ----------


@unittest.skipUnless(HAS_SERVER_APP, "需要同一工作区的 file-distribution-server 应用")
class HttpIntegrationTests(ResetDefaultsMixin, ToolRunMixin, unittest.TestCase):
    def setUp(self):
        self.reset_tool_defaults()
        if SERVER_APP_ROOT not in sys.path:
            sys.path.insert(0, SERVER_APP_ROOT)
        import main as filesvr

        self.filesvr = filesvr
        self.tmp = tempfile.mkdtemp(prefix="cptool-http-")
        self.data_dir = os.path.join(self.tmp, "data")
        self.local_dir = os.path.join(self.tmp, "local")
        os.makedirs(self.data_dir)
        os.makedirs(self.local_dir)

        self.contents = {
            "a.bak": b"top-level",
            os.path.join("sub", "b.dat"): b"nested data",
            "large.bin": b"0123456789" * 10,
            "测 试.txt": "中文 内容".encode("utf-8"),
        }
        for rel, data in self.contents.items():
            full = os.path.join(self.data_dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(data)
        self.server = filesvr.create_server(("127.0.0.1", 0), [self.data_dir], USER, PASSWORD)
        self.builder = self.server.mounts[0][2]
        self.builder.refresh()

        class LoggingHandler(filesvr.FileRequestHandler):
            seen_paths = []
            seen_ranges = []

            def do_GET(self):
                path = unquote(urlsplit(self.path).path)
                cls = type(self)
                cls.seen_paths.append(path)
                if self.headers.get("Range"):
                    cls.seen_ranges.append((path, self.headers["Range"]))
                super().do_GET()

        self.handler = LoggingHandler
        self.server.RequestHandlerClass = LoggingHandler
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_http(self, user=USER, password=PASSWORD, local_dir=None):
        url = f"http://127.0.0.1:{self.port}/files.manifest"
        return self.run_tool(url, user=user, password=password, local_dir=local_dir)

    def local_path(self, rel):
        return os.path.join(self.local_dir, *rel.split(os.sep))

    def test_http_download_with_basic_auth(self):
        code, output = self.run_http()
        self.assertEqual(code, 0, output)
        for rel, data in self.contents.items():
            self.assertEqual(read_bytes(self.local_path(rel)), data, rel)
        manifest = read_json(os.path.join(self.local_dir, "file.manifest"))
        self.assertEqual(manifest["path"], os.path.abspath(self.local_dir))
        server_manifest = read_json(os.path.join(self.data_dir, "files.manifest"))
        self.assertEqual(
            {f["refpath"] for f in manifest["files"]},
            {f["refpath"] for f in server_manifest["files"]},
        )

    def test_http_wrong_password_rejected(self):
        code, output = self.run_http(password="wrong")
        self.assertEqual(code, 2)
        self.assertIn("HTTP 401", output)
        # 清单获取失败不应触碰本地清单
        self.assertFalse(os.path.exists(os.path.join(self.local_dir, "file.manifest")))

    def test_http_no_credentials_rejected(self):
        code, _ = self.run_http(user=None, password=None)
        self.assertEqual(code, 2)

    def test_http_incremental_second_run(self):
        self.run_http()
        target = self.local_path("a.bak")
        before = os.stat(target).st_mtime_ns
        self.handler.seen_paths.clear()
        self.handler.seen_ranges.clear()
        code, output = self.run_http()
        self.assertEqual(code, 0, output)
        self.assertIn("0 个需要更新", output)
        self.assertEqual(os.stat(target).st_mtime_ns, before)
        # 第二次运行只请求了清单，没有再请求任何数据文件
        self.assertEqual(set(self.handler.seen_paths), {"/files.manifest"})

    def test_http_manifest_with_backslash_and_unicode(self):
        # 手工把清单 refpath 改成反斜杠形式，验证拼 URL 时的替换与百分号编码
        manifest = read_json(os.path.join(self.data_dir, "files.manifest"))
        for entry in manifest["files"]:
            if "/" in entry["refpath"]:
                entry["refpath"] = entry["refpath"].replace("/", "\\")
        write_json(os.path.join(self.data_dir, "files.manifest"), manifest)

        code, output = self.run_http()
        self.assertEqual(code, 0, output)
        for rel, data in self.contents.items():
            self.assertEqual(read_bytes(self.local_path(rel)), data, rel)

    def test_http_resume_from_partial_tmp(self):
        cptool.RESUME_THRESHOLD = 16  # 让 100 字节的文件也走续传分支
        content = self.contents["large.bin"]
        with open(self.local_path("large.bin") + ".tmp", "wb") as fh:
            fh.write(content[:40])

        code, output = self.run_http()
        self.assertEqual(code, 0, output)
        self.assertEqual(read_bytes(self.local_path("large.bin")), content)
        # 服务器确实收到了断点请求，且起点是本地临时文件长度
        self.assertIn(("/large.bin", "bytes=40-"), self.handler.seen_ranges)
        self.assertFalse(os.path.exists(self.local_path("large.bin") + ".tmp"))

    def test_http_complete_tmp_skips_download(self):
        cptool.RESUME_THRESHOLD = 16
        content = self.contents["large.bin"]
        # 临时文件已完整（上次下载完成后、校验前中断）：直接校验并重命名
        with open(self.local_path("large.bin") + ".tmp", "wb") as fh:
            fh.write(content)

        code, output = self.run_http()
        self.assertEqual(code, 0, output)
        self.assertEqual(read_bytes(self.local_path("large.bin")), content)
        self.assertNotIn("/large.bin", self.handler.seen_paths)

    def test_http_unreachable_manifest_returns_2(self):
        code, output = self.run_tool("http://127.0.0.1:1/files.manifest")
        self.assertEqual(code, 2)
        self.assertIn("获取或解析文件清单失败", output)


# ---------- http 可控故障服务器（断点续传 / 重试） ----------


class FlakyHandler(BaseHTTPRequestHandler):
    """测试用可控故障服务器：首次请求可配置为截断或 500，并支持 Range。"""

    protocol_version = "HTTP/1.1"
    file_bytes = b""
    manifest_body = b""
    mode = "drop"  # drop: 首次发 40 字节后断开; error: 首次返回 500
    count = 0
    seen_ranges = []

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(type(self).file_bytes)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        cls = type(self)
        path = unquote(urlsplit(self.path).path)
        if path == "/files.manifest":
            self.send_response(200)
            self.send_header("Content-Length", str(len(cls.manifest_body)))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(cls.manifest_body)
            return
        cls.count += 1
        first = cls.count == 1
        range_header = self.headers.get("Range")
        if range_header:
            cls.seen_ranges.append(range_header)
        if first and cls.mode == "error":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if first and cls.mode == "drop" and not range_header:
            # 发送 40 字节后强制断开，模拟网络不稳定
            self.send_response(200)
            self.send_header("Content-Length", str(len(cls.file_bytes)))
            self.end_headers()
            self.wfile.write(cls.file_bytes[:40])
            self.close_connection = True
            return
        start = 0
        if range_header:
            start = int(range_header[len("bytes="):].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(cls.file_bytes) - 1}/{len(cls.file_bytes)}")
        else:
            self.send_response(200)
        data = cls.file_bytes[start:]
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


class FlakyHttpTests(ResetDefaultsMixin, ToolRunMixin, unittest.TestCase):
    def setUp(self):
        self.reset_tool_defaults()
        self.tmp = tempfile.mkdtemp(prefix="cptool-flaky-")
        self.local_dir = os.path.join(self.tmp, "local")
        os.makedirs(self.local_dir)

        self.content = b"0123456789" * 10
        manifest = {
            "path": "/virtual/data",
            "files": [
                {"refpath": "large.bin", "modified": "2026-06-07 10:00:00", "hash": md5_hex(self.content)}
            ],
        }
        FlakyHandler.file_bytes = self.content
        FlakyHandler.manifest_body = json.dumps(manifest).encode("utf-8")
        FlakyHandler.count = 0
        FlakyHandler.seen_ranges = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_download_retries_and_resumes_after_connection_drop(self):
        cptool.RESUME_THRESHOLD = 16
        FlakyHandler.mode = "drop"

        code, output = self.run_tool(f"http://127.0.0.1:{self.port}/files.manifest")
        self.assertEqual(code, 0, output)
        self.assertIn("传输中断", output)
        self.assertIn("重试 1/3", output)
        # 第一次被截断，重试时从断点继续
        self.assertEqual(FlakyHandler.count, 2)
        self.assertIn("bytes=40-", FlakyHandler.seen_ranges)
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "large.bin")), self.content)
        manifest = read_json(os.path.join(self.local_dir, "file.manifest"))
        self.assertEqual(manifest["files"][0]["hash"], md5_hex(self.content))

    def test_download_retries_after_server_error(self):
        cptool.RESUME_THRESHOLD = 16
        FlakyHandler.mode = "error"

        code, output = self.run_tool(f"http://127.0.0.1:{self.port}/files.manifest")
        self.assertEqual(code, 0, output)
        self.assertIn("HTTP 500", output)
        self.assertEqual(FlakyHandler.count, 2)
        self.assertEqual(read_bytes(os.path.join(self.local_dir, "large.bin")), self.content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
