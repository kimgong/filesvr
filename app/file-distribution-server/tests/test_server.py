"""文件分发服务器自动化测试（标准库 unittest，无第三方依赖）。

运行方式::

    python -m unittest discover -s tests -v
    python tests/test_server.py
"""

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from urllib.parse import quote

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import main as filesvr  # noqa: E402

USER = "admin"
PASSWORD = "secret"

TOP_FILE_CONTENT = b"hello-world-" * 10
NESTED_FILE_CONTENT = b"nested file content"
DEEP_FILE_CONTENT = b"0123456789" * 100
CHINESE_FILE_CONTENT = "中文内容 with spaces".encode("utf-8")


def http_request(port, path, credentials=(USER, PASSWORD), method="GET", headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        all_headers = dict(headers or {})
        if credentials is not None:
            token = base64.b64encode(
                f"{credentials[0]}:{credentials[1]}".encode("utf-8")
            ).decode("ascii")
            all_headers["Authorization"] = "Basic " + token
        conn.request(method, path, headers=all_headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


class FileServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="filesvr-test-")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.data_dir)
        self.write_file("a.bak", TOP_FILE_CONTENT)
        self.write_file(os.path.join("sub", "a.bak"), NESTED_FILE_CONTENT)
        self.write_file(os.path.join("sub", "deep", "nested.dat"), DEEP_FILE_CONTENT)
        self.write_file("测 试.txt", CHINESE_FILE_CONTENT)
        self.write_file("原始文件.txt", "raw utf8 path".encode("utf-8"))

        self.server = filesvr.create_server(("127.0.0.1", 0), [self.data_dir], USER, PASSWORD)
        self.builder = self.server.mounts[0][2]
        self.builder.refresh()
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_file(self, rel, data):
        full = os.path.join(self.data_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
        return full

    def request(self, path, credentials=(USER, PASSWORD), method="GET", headers=None):
        return http_request(self.port, path, credentials, method, headers)

    # ---------- 需求 1.1 Basic Authentication ----------

    def test_requires_authentication(self):
        status, _, _ = self.request("/files.manifest", credentials=None)
        self.assertEqual(status, 401)

    def test_missing_auth_gets_challenge(self):
        status, headers, _ = self.request("/", credentials=None)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("www-authenticate", ""))

    def test_wrong_credentials_rejected(self):
        status, _, _ = self.request("/", credentials=("admin", "wrong"))
        self.assertEqual(status, 401)

    # ---------- 需求 1.2/1.3/1.4/1.5 清单 ----------

    def test_manifest_structure(self):
        status, headers, body = self.request("/files.manifest")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        manifest = json.loads(body.decode("utf-8"))
        self.assertEqual(manifest["path"], os.path.abspath(self.data_dir))
        refpaths = {f["refpath"] for f in manifest["files"]}
        self.assertIn("a.bak", refpaths)
        self.assertIn("/sub/a.bak", refpaths)
        self.assertIn("/sub/deep/nested.dat", refpaths)
        self.assertIn("测 试.txt", refpaths)
        self.assertIn("原始文件.txt", refpaths)
        self.assertNotIn("files.manifest", refpaths)

    def test_manifest_hash_and_modified_format(self):
        _, _, body = self.request("/files.manifest")
        by_ref = {f["refpath"]: f for f in json.loads(body.decode("utf-8"))["files"]}
        self.assertEqual(by_ref["a.bak"]["hash"], hashlib.md5(TOP_FILE_CONTENT).hexdigest())
        self.assertEqual(by_ref["/sub/a.bak"]["hash"], hashlib.md5(NESTED_FILE_CONTENT).hexdigest())
        for entry in by_ref.values():
            self.assertRegex(entry["modified"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_manifest_refpath_separator(self):
        _, _, body = self.request("/files.manifest")
        for entry in json.loads(body.decode("utf-8"))["files"]:
            self.assertNotIn("\\", entry["refpath"])

    def test_manifest_download_matches_disk_file(self):
        _, _, body = self.request("/files.manifest")
        with open(os.path.join(self.data_dir, "files.manifest"), "rb") as fh:
            self.assertEqual(body, fh.read())

    def test_refresh_without_change_returns_false(self):
        self.assertFalse(self.builder.refresh())

    # ---------- 文件下载 ----------

    def test_download_top_level_file(self):
        status, _, body = self.request("/a.bak")
        self.assertEqual(status, 200)
        self.assertEqual(body, TOP_FILE_CONTENT)

    def test_download_nested_file(self):
        status, _, body = self.request("/sub/a.bak")
        self.assertEqual(status, 200)
        self.assertEqual(body, NESTED_FILE_CONTENT)

    def test_download_with_double_slash(self):
        # 客户端用 "base + '/' + refpath" 拼接时可能出现双斜杠
        status, _, body = self.request("//sub/a.bak")
        self.assertEqual(status, 200)
        self.assertEqual(body, NESTED_FILE_CONTENT)

    def test_head_request(self):
        status, headers, body = self.request("/a.bak", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("content-length"), str(len(TOP_FILE_CONTENT)))

    def test_not_found(self):
        status, _, _ = self.request("/no/such/file.txt")
        self.assertEqual(status, 404)

    def test_post_not_supported(self):
        status, _, _ = self.request("/a.bak", method="POST")
        self.assertEqual(status, 501)

    # ---------- 需求 1.8 URL 编码 ----------

    def test_url_encoded_path(self):
        status, _, body = self.request("/" + quote("测 试.txt"))
        self.assertEqual(status, 200)
        self.assertEqual(body, CHINESE_FILE_CONTENT)

    def test_raw_utf8_path(self):
        # 部分客户端直接发送未经百分号编码的 UTF-8 字节路径
        import socket

        token = base64.b64encode(f"{USER}:{PASSWORD}".encode("utf-8")).decode("ascii")
        request = (
            "GET /原始文件.txt HTTP/1.1\r\n".encode("utf-8")
            + f"Host: 127.0.0.1:{self.port}\r\nAuthorization: Basic {token}\r\n"
            "Connection: close\r\n\r\n".encode("ascii")
        )
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(request)
            data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n")[0].decode("ascii")
        self.assertIn("200", status_line)
        self.assertEqual(body, "raw utf8 path".encode("utf-8"))

    # ---------- 需求 1.6 目录浏览 ----------

    def test_root_listing_includes_manifest(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        text = body.decode("utf-8")
        self.assertIn("files.manifest", text)
        self.assertIn("sub/", text)
        self.assertIn("a.bak", text)

    def test_listing_links_are_url_encoded(self):
        _, _, body = self.request("/")
        text = body.decode("utf-8")
        self.assertIn('href="/%E6%B5%8B%20%E8%AF%95.txt"', text)

    def test_sub_directory_listing(self):
        status, _, body = self.request("/sub/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("a.bak", text)
        self.assertIn("deep/", text)
        self.assertIn('href="/sub/deep/"', text)

    def test_directory_listing_without_trailing_slash(self):
        status, _, body = self.request("/sub")
        self.assertEqual(status, 200)
        self.assertIn("deep/", body.decode("utf-8"))

    # ---------- 安全校验 ----------

    def test_path_traversal_blocked(self):
        status, _, _ = self.request("/../outside.txt")
        self.assertEqual(status, 403)
        status, _, _ = self.request("/sub/../../outside.txt")
        self.assertEqual(status, 403)
        status, _, _ = self.request("/sub/%2e%2e/outside.txt")
        self.assertEqual(status, 403)

    # ---------- Range 请求（供下载工具断点续传） ----------

    def test_range_request(self):
        status, headers, body = self.request(
            "/sub/deep/nested.dat", headers={"Range": "bytes=10-19"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers.get("content-range"), "bytes 10-19/1000")
        self.assertEqual(body, b"0123456789")

    def test_range_open_ended(self):
        status, _, body = self.request("/sub/deep/nested.dat", headers={"Range": "bytes=990-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, DEEP_FILE_CONTENT[990:])

    def test_range_suffix(self):
        status, _, body = self.request("/sub/deep/nested.dat", headers={"Range": "bytes=-10"})
        self.assertEqual(status, 206)
        self.assertEqual(body, DEEP_FILE_CONTENT[-10:])

    def test_range_unsatisfiable(self):
        status, headers, _ = self.request("/sub/deep/nested.dat", headers={"Range": "bytes=2000-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers.get("content-range"), "bytes */1000")

    def test_range_multi_part_ignored(self):
        status, _, body = self.request(
            "/sub/deep/nested.dat", headers={"Range": "bytes=0-1,3-4"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, DEEP_FILE_CONTENT)

    def test_parse_range_unit(self):
        parse = filesvr.FileRequestHandler._parse_range
        self.assertEqual(parse("bytes=0-4", 10), (0, 4))
        self.assertEqual(parse("bytes=5-", 10), (5, 9))
        self.assertEqual(parse("bytes=-3", 10), (7, 9))
        self.assertEqual(parse("bytes=-99", 10), (0, 9))
        self.assertIsNone(parse("bytes=0-1,3-4", 10))
        self.assertIsNone(parse("bytes=abc", 10))
        self.assertIsNone(parse("bytes=9-3", 10))
        self.assertFalse(parse("bytes=20-", 10))
        self.assertFalse(parse("bytes=0-", 0))
        self.assertFalse(parse("bytes=-0", 10))

    # ---------- 监听目录变化 ----------

    def test_manifest_updated_after_new_file(self):
        self.write_file("brandnew.bin", b"new content")
        self.assertTrue(self.builder.refresh())
        _, _, body = self.request("/files.manifest")
        manifest = json.loads(body.decode("utf-8"))
        refpaths = {f["refpath"] for f in manifest["files"]}
        self.assertIn("brandnew.bin", refpaths)
        status, _, body = self.request("/brandnew.bin")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"new content")

    def test_hash_changes_after_file_modified(self):
        _, _, body = self.request("/files.manifest")
        before = {f["refpath"]: f for f in json.loads(body.decode("utf-8"))["files"]}["a.bak"]["hash"]
        time.sleep(0.02)
        self.write_file("a.bak", b"changed content")
        self.assertTrue(self.builder.refresh())
        _, _, body = self.request("/files.manifest")
        after = {f["refpath"]: f for f in json.loads(body.decode("utf-8"))["files"]}["a.bak"]["hash"]
        self.assertNotEqual(before, after)
        self.assertEqual(after, hashlib.md5(b"changed content").hexdigest())

    def test_monitor_thread_detects_changes(self):
        stop = threading.Event()
        worker = threading.Thread(
            target=filesvr.monitor_loop, args=([self.builder], 0.2, stop), daemon=True
        )
        worker.start()
        try:
            self.write_file("late-file.txt", b"late")
            deadline = time.time() + 5
            seen = False
            while time.time() < deadline:
                _, _, body = self.request("/files.manifest")
                manifest = json.loads(body.decode("utf-8"))
                if any(f["refpath"] == "late-file.txt" for f in manifest["files"]):
                    seen = True
                    break
                time.sleep(0.2)
            self.assertTrue(seen, "监控线程未在期限内把新文件写入清单")
        finally:
            stop.set()
            worker.join(timeout=5)


class MultiDirServerTestCase(unittest.TestCase):
    """需求 1.9：多个数据目录（--data-dir 以英文逗号分隔）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="filesvr-multi-")
        self.dir_a = os.path.join(self.tmp, "alpha")
        self.dir_b = os.path.join(self.tmp, "beta")
        os.makedirs(os.path.join(self.dir_a, "inner"))
        os.makedirs(self.dir_b)
        self.put(os.path.join(self.dir_a, "a.bak"), b"alpha top file")
        self.put(os.path.join(self.dir_a, "inner", "x.dat"), b"alpha nested")
        self.put(os.path.join(self.dir_b, "b.bin"), b"beta file")
        self.put(os.path.join(self.dir_b, "共享 文档.txt"), "beta 中文".encode("utf-8"))

        self.server = filesvr.create_server(
            ("127.0.0.1", 0), [self.dir_a, self.dir_b], USER, PASSWORD
        )
        for _, _, builder in self.server.mounts:
            builder.refresh()
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, full, data):
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)

    def request(self, path, credentials=(USER, PASSWORD), method="GET", headers=None):
        return http_request(self.port, path, credentials, method, headers)

    # ---------- 各目录独立清单 ----------

    def test_each_dir_has_own_manifest(self):
        for mount, data_dir in (("alpha", self.dir_a), ("beta", self.dir_b)):
            status, headers, body = self.request(f"/{mount}/files.manifest")
            self.assertEqual(status, 200)
            self.assertIn("application/json", headers.get("content-type", ""))
            manifest = json.loads(body.decode("utf-8"))
            self.assertEqual(manifest["path"], os.path.abspath(data_dir))
            self.assertNotIn("files.manifest", {f["refpath"] for f in manifest["files"]})

    def test_manifest_contents_per_dir(self):
        _, _, body = self.request("/alpha/files.manifest")
        refpaths = {f["refpath"] for f in json.loads(body.decode("utf-8"))["files"]}
        self.assertEqual(refpaths, {"a.bak", "/inner/x.dat"})
        _, _, body = self.request("/beta/files.manifest")
        refpaths = {f["refpath"] for f in json.loads(body.decode("utf-8"))["files"]}
        self.assertEqual(refpaths, {"b.bin", "共享 文档.txt"})

    # ---------- 多目录下的文件下载 ----------

    def test_download_files_from_each_mount(self):
        status, _, body = self.request("/alpha/a.bak")
        self.assertEqual((status, body), (200, b"alpha top file"))
        status, _, body = self.request("/alpha/inner/x.dat")
        self.assertEqual((status, body), (200, b"alpha nested"))
        status, _, body = self.request("/beta/b.bin")
        self.assertEqual((status, body), (200, b"beta file"))

    def test_url_encoded_file_in_mount(self):
        status, _, body = self.request("/beta/" + quote("共享 文档.txt"))
        self.assertEqual(status, 200)
        self.assertEqual(body, "beta 中文".encode("utf-8"))

    def test_unknown_mount_404(self):
        status, _, _ = self.request("/gamma/a.bak")
        self.assertEqual(status, 404)

    # ---------- 根路径为挂载索引页 ----------

    def test_root_is_mount_index(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        text = body.decode("utf-8")
        self.assertIn('href="/alpha/"', text)
        self.assertIn('href="/beta/"', text)

    def test_root_manifest_not_available_in_multi_dir_mode(self):
        status, _, _ = self.request("/files.manifest")
        self.assertEqual(status, 404)

    # ---------- 多目录下的目录浏览 ----------

    def test_directory_listing_under_mount(self):
        status, _, body = self.request("/alpha/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("a.bak", text)
        self.assertIn('href="/alpha/inner/"', text)
        self.assertIn('href="/"', text)  # 上级链接回到挂载索引页
        status, _, body = self.request("/alpha/inner")
        self.assertIn("x.dat", body.decode("utf-8"))

    def test_mount_listing_includes_own_manifest(self):
        status, _, body = self.request("/beta/")
        self.assertEqual(status, 200)
        self.assertIn("files.manifest", body.decode("utf-8"))

    # ---------- 安全校验 ----------

    def test_traversal_blocked_per_mount(self):
        status, _, _ = self.request("/alpha/../../outside.txt")
        self.assertEqual(status, 403)
        status, _, _ = self.request("/alpha/%2e%2e/outside.txt")
        self.assertEqual(status, 403)

    # ---------- 挂载名分配 ----------

    def test_mount_name_conflict_gets_suffix(self):
        dir1 = os.path.join(self.tmp, "pair-one", "data")
        dir2 = os.path.join(self.tmp, "pair-two", "data")
        os.makedirs(dir1)
        os.makedirs(dir2)
        self.put(os.path.join(dir1, "one.txt"), b"one")
        self.put(os.path.join(dir2, "two.txt"), b"two")
        server = filesvr.create_server(("127.0.0.1", 0), [dir1, dir2], USER, PASSWORD)
        names = [name for name, _, _ in server.mounts]
        self.assertEqual(names, ["data", "data-2"])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            status, _, body = http_request(port, "/data/one.txt")
            self.assertEqual((status, body), (200, b"one"))
            status, _, body = http_request(port, "/data-2/two.txt")
            self.assertEqual((status, body), (200, b"two"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_chinese_mount_name(self):
        dir_c = os.path.join(self.tmp, "数据目录")
        os.makedirs(dir_c)
        self.put(os.path.join(dir_c, "c.txt"), b"c-content")
        server = filesvr.create_server(("127.0.0.1", 0), [self.dir_a, dir_c], USER, PASSWORD)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            status, _, body = http_request(port, "/" + quote("数据目录") + "/c.txt")
            self.assertEqual((status, body), (200, b"c-content"))
            status, _, body = http_request(port, "/")
            self.assertIn('href="/%E6%95%B0%E6%8D%AE%E7%9B%AE%E5%BD%95/"', body.decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    # ---------- --data-dir 参数解析与多目录监听 ----------

    def test_split_data_dirs(self):
        split = filesvr.split_data_dirs
        self.assertEqual(split("D:\\a,D:\\b"), ["D:\\a", "D:\\b"])
        self.assertEqual(split(" D:\\a , ,D:\\b "), ["D:\\a", "D:\\b"])
        self.assertEqual(split("D:\\a"), ["D:\\a"])
        self.assertEqual(split(""), [])
        # 同一目录（仅大小写不同）只保留一次
        same = os.path.join(self.tmp, "alpha")
        self.assertEqual(split(f"{same},{same.upper()}"), [same])

    def test_monitor_refreshes_all_dirs(self):
        stop = threading.Event()
        builders = [builder for _, _, builder in self.server.mounts]
        worker = threading.Thread(
            target=filesvr.monitor_loop, args=(builders, 0.2, stop), daemon=True
        )
        worker.start()
        try:
            self.put(os.path.join(self.dir_a, "new-a.txt"), b"a")
            self.put(os.path.join(self.dir_b, "new-b.txt"), b"b")
            deadline = time.time() + 5
            ok_a = ok_b = False
            while time.time() < deadline and not (ok_a and ok_b):
                _, _, body = self.request("/alpha/files.manifest")
                ok_a = any(
                    f["refpath"] == "new-a.txt" for f in json.loads(body.decode("utf-8"))["files"]
                )
                _, _, body = self.request("/beta/files.manifest")
                ok_b = any(
                    f["refpath"] == "new-b.txt" for f in json.loads(body.decode("utf-8"))["files"]
                )
                time.sleep(0.2)
            self.assertTrue(ok_a and ok_b, "监控线程未在期限内同时更新两个目录的清单")
        finally:
            stop.set()
            worker.join(timeout=5)


class DirAliasTestCase(unittest.TestCase):
    """--dir-alias 指定各目录的挂载名（按位置与 --data-dir 一一对应）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="filesvr-alias-")
        self.dir_a = os.path.join(self.tmp, "dir-one")
        self.dir_b = os.path.join(self.tmp, "dir-two")
        for data_dir in (self.dir_a, self.dir_b):
            os.makedirs(data_dir)
        self.put(os.path.join(self.dir_a, "a.txt"), b"a-content")
        self.put(os.path.join(self.dir_b, "b.txt"), b"b-content")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, full, data):
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)

    def start_server(self, data_dirs, aliases=None):
        server = filesvr.create_server(("127.0.0.1", 0), data_dirs, USER, PASSWORD, aliases)
        for _, _, builder in server.mounts:
            builder.refresh()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    # ---------- 别名生效 ----------

    def test_aliases_override_mount_names(self):
        server = self.start_server([self.dir_a, self.dir_b], ["db", "备份库"])
        port = server.server_address[1]
        self.assertEqual([name for name, _, _ in server.mounts], ["db", "备份库"])
        status, _, body = http_request(port, "/db/a.txt")
        self.assertEqual((status, body), (200, b"a-content"))
        status, _, body = http_request(port, "/" + quote("备份库") + "/b.txt")
        self.assertEqual((status, body), (200, b"b-content"))
        status, _, body = http_request(port, "/db/files.manifest")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["path"], os.path.abspath(self.dir_a))
        status, _, body = http_request(port, "/")
        text = body.decode("utf-8")
        self.assertIn('href="/db/"', text)
        self.assertIn('href="/%E5%A4%87%E4%BB%BD%E5%BA%93/"', text)

    def test_single_dir_with_alias_mounts_under_alias(self):
        server = self.start_server([self.dir_a], ["db"])
        port = server.server_address[1]
        status, _, body = http_request(port, "/db/a.txt")
        self.assertEqual((status, body), (200, b"a-content"))
        status, _, body = http_request(port, "/db/files.manifest")
        self.assertEqual(status, 200)
        # 指定别名后根路径不再直接提供清单, 而是挂载索引页
        status, _, _ = http_request(port, "/files.manifest")
        self.assertEqual(status, 404)
        status, _, body = http_request(port, "/")
        self.assertIn('href="/db/"', body.decode("utf-8"))

    def test_aliases_do_not_auto_suffix_on_case_difference(self):
        # 别名大小写保留原样, 精确匹配
        mounts = filesvr.build_mounts([self.dir_a, self.dir_b], ["Data", "data"])
        self.assertEqual([name for name, _, _ in mounts], ["Data", "data"])

    # ---------- 别名校验（配置错误直接报错） ----------

    def test_alias_count_mismatch_raises(self):
        with self.assertRaises(ValueError):
            filesvr.build_mounts([self.dir_a, self.dir_b], ["only-one"])
        with self.assertRaises(ValueError):
            filesvr.build_mounts([self.dir_a], ["a", "b"])

    def test_duplicate_aliases_raise(self):
        with self.assertRaises(ValueError):
            filesvr.build_mounts([self.dir_a, self.dir_b], ["same", "same"])

    def test_invalid_alias_raises(self):
        for bad in ("", "  ", ".", "..", "a/b", "a\\b"):
            with self.assertRaises(ValueError):
                filesvr.build_mounts([self.dir_a, self.dir_b], [bad, "ok"])

    def test_alias_whitespace_trimmed(self):
        mounts = filesvr.build_mounts([self.dir_a, self.dir_b], [" db ", " backup "])
        self.assertEqual([name for name, _, _ in mounts], ["db", "backup"])

    # ---------- 参数解析与默认行为回归 ----------

    def test_parse_args_dir_alias_optional(self):
        args = filesvr.parse_args(["--user", "u", "--pass", "p", "--data-dir", "D:\\x"])
        self.assertIsNone(args.dir_alias)
        args = filesvr.parse_args(
            ["--user", "u", "--pass", "p", "--data-dir", "D:\\x", "--dir-alias", "db,backup"]
        )
        self.assertEqual(args.dir_alias, "db,backup")

    def test_default_mount_names_without_alias(self):
        mounts = filesvr.build_mounts([self.dir_a, self.dir_b])
        self.assertEqual([name for name, _, _ in mounts], ["dir-one", "dir-two"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
