#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""download —— 我的世界版本一键下载器（mc-mirror 镜像站客户端）

用法：
    download Minecraft 26.1              # 下载 26.1 系列最新正式版
    download Minecraft 26.1 26.1.2       # 下载指定版本
    download list 26.1                   # 看看 26.1 系列有哪些版本

下载前会弹出文件夹选择框（默认「下载」文件夹），确认后自动下载到
「<你选的文件夹>\\Minecraft 26.1」，并生成「启动游戏.bat」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

APP_NAME = "download"
APP_VERSION = "1.0.0"
OFFICIAL_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RAW_BASE = "https://raw.githubusercontent.com/{repo}/main/index"
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (Minecraft version downloader)"

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "download-mc"
CONFIG_PATH = APP_DIR / "config.json"
CACHE_DIR = APP_DIR / "cache"

OS_NAME = {"Windows": "windows", "Darwin": "osx", "Linux": "linux"}.get(
    platform.system(), "linux"
)
MACHINE = platform.machine().lower()
IS_ARM64 = MACHINE in ("arm64", "aarch64")
IS_32BIT = platform.architecture()[0] == "32bit"

DEFAULT_CONFIG = {
    "repo": "",            # GitHub 仓库，如 owner/mc-mirror
    "username": "Player",  # 游戏内离线用户名
    "memory": "2G",        # 最大内存
    "threads": 8,          # 普通文件并发数
    "assets": True,        # 是否下载资源文件
}


# --------------------------------------------------------------------------- 基础工具
def log(msg: str = "") -> None:
    print(msg, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    if os.environ.get("MC_MIRROR_REPO"):
        cfg["repo"] = os.environ["MC_MIRROR_REPO"]
    return cfg


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def downloads_dir() -> Path:
    """用户的「下载」文件夹。"""
    try:
        import ctypes
        from ctypes import wintypes

        # FOLDERID_Downloads
        guid = ctypes.c_wchar_p("{374DE290-123F-4565-9164-39C4925E467B}")
        path_ptr = ctypes.c_wchar_p()
        ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(path_ptr))
        result = Path(path_ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        if result.exists():
            return result
    except Exception:
        pass
    for candidate in (Path.home() / "Downloads", Path.home() / "下载"):
        if candidate.exists():
            return candidate
    return Path.home()


def series_of(version_id: str) -> str:
    base = version_id.split("-")[0]
    parts = base.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else base


def offline_uuid(username: str) -> str:
    digest = hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest()
    b = bytearray(digest)
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    h = bytes(b).hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# --------------------------------------------------------------------------- 网络
def http_open(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_bytes(url: str, retries: int = 3, timeout: int = 30) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with http_open(url, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last = err
            if attempt < retries:
                time.sleep(attempt * 1.2)
    raise RuntimeError(f"下载失败：{url}（{last}）")


def fetch_json(url: str) -> dict:
    return json.loads(fetch_bytes(url).decode("utf-8"))


# --------------------------------------------------------------------------- 数据库
def mirror_available(cfg: dict) -> bool:
    return bool(cfg.get("repo"))


def load_manifest(cfg: dict, refresh: bool = False) -> tuple[dict, str]:
    """优先用镜像站数据库，失败再退回官方清单。返回 (清单, 来源说明)。"""
    cache_file = CACHE_DIR / "manifest.json"
    if not refresh and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # 换了镜像站仓库就重新读，避免用到旧来源的缓存
            if (cached.get("_repo", "") == cfg.get("repo", "")
                    and time.time() - cached.get("_fetched", 0) < 6 * 3600):
                return cached["data"], cached.get("_source", "本地缓存")
        except (OSError, ValueError):
            pass

    sources: list[tuple[str, str]] = []
    if mirror_available(cfg):
        sources.append((f"{RAW_BASE.format(repo=cfg['repo'])}/manifest.json", "镜像站数据库"))
    sources.append((OFFICIAL_MANIFEST, "Mojang 官方"))

    for url, label in sources:
        try:
            data = fetch_json(url)
            if isinstance(data, dict) and data.get("versions"):
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps({"_fetched": time.time(), "_source": label,
                                "_repo": cfg.get("repo", ""), "data": data}),
                    encoding="utf-8",
                )
                return data, label
        except Exception:
            continue
    raise RuntimeError("拿不到版本清单：镜像站和官方都连不上，请检查网络。")


def resolve_version(manifest: dict, name: str) -> dict | None:
    """把 26.1 / 26.1.2 这样的输入解析成清单里的一条版本记录。"""
    versions = manifest.get("versions", [])
    lowered = name.lower()

    for v in versions:  # 完全匹配
        if v["id"].lower() == lowered:
            return v

    # 系列匹配：优先正式版，再按发布时间取最新
    candidates = [v for v in versions if v["id"].lower().startswith(lowered)]
    if not candidates:
        return None
    releases = [v for v in candidates if v.get("type") == "release"]
    pool = releases or candidates
    return max(pool, key=lambda v: v.get("releaseTime", ""))


def fetch_version_json(cfg: dict, entry: dict) -> dict:
    vid = entry["id"]
    if mirror_available(cfg):
        url = f"{RAW_BASE.format(repo=cfg['repo'])}/versions/{urllib.parse.quote(vid)}.json"
        try:
            return fetch_json(url)
        except Exception:
            pass  # 镜像里没这个版本就回官方
    return fetch_json(entry["url"])


# --------------------------------------------------------------------------- 规则判断
def _rule_matches(rule: dict, features: dict | None = None) -> bool:
    os_rule = rule.get("os") or {}
    if os_rule:
        if "name" in os_rule and os_rule["name"] != OS_NAME:
            return False
        arch = os_rule.get("arch")
        if arch:
            if arch == "x86" and not IS_32BIT:
                return False
            if arch == "arm64" and not IS_ARM64:
                return False
            if arch in ("x86_64", "amd64") and IS_32BIT:
                return False
        if "version" in os_rule:
            try:
                if not re.match(os_rule["version"], platform.version()):
                    return False
            except re.error:
                pass
    feats = rule.get("features") or {}
    for key, want in feats.items():
        if bool((features or {}).get(key)) != bool(want):
            return False
    return True


def rule_allows(rules: list | None, features: dict | None = None) -> bool:
    """Mojang 规则：逐条匹配，取最后一条命中规则的动作；都不命中则不允许。"""
    if not rules:
        return True
    allowed = False
    for rule in rules:
        if _rule_matches(rule, features):
            allowed = rule.get("action") == "allow"
    return allowed


def natives_arch_ok(lib_name: str) -> bool:
    if lib_name.endswith(":natives-windows-arm64"):
        return IS_ARM64
    if lib_name.endswith(":natives-windows-x86"):
        return IS_32BIT
    if lib_name.endswith(":natives-windows"):
        return not IS_ARM64 and not IS_32BIT
    return True


def maven_url(name: str) -> str | None:
    """没有 downloads 字段的老版本依赖，手动拼 maven 地址。"""
    parts = name.split(":")
    if len(parts) < 3:
        return None
    group, artifact, version = parts[0], parts[1], parts[2]
    path = f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.jar"
    return f"https://libraries.minecraft.net/{path}"


# --------------------------------------------------------------------------- 下载计划
def build_plan(vjson: dict, folder: Path, want_assets: bool) -> dict:
    """算出要下载哪些文件，不实际下载。"""
    vid = vjson["id"]
    tasks: list[dict] = []
    natives: list[str] = []

    client = vjson["downloads"]["client"]
    tasks.append({
        "url": client["url"],
        "rel": f"versions/{vid}/{vid}.jar",
        "sha1": client["sha1"],
        "size": client.get("size", 0),
        "kind": "客户端主程序",
    })

    for lib in vjson.get("libraries", []):
        if not rule_allows(lib.get("rules")):
            continue
        name = lib.get("name", "")
        downloads = lib.get("downloads") or {}
        if ":natives-" in name and not natives_arch_ok(name):
            continue

        artifact = downloads.get("artifact")
        if artifact and artifact.get("url"):
            rel = f"libraries/{artifact['path']}"
            tasks.append({
                "url": artifact["url"], "rel": rel,
                "sha1": artifact.get("sha1", ""),
                "size": artifact.get("size", 0), "kind": "依赖库",
            })
            if ":natives-" in name:
                natives.append(rel)

        # 老版本用 natives 字段 + classifiers 指定本地库
        native_key = (lib.get("natives") or {}).get("windows")
        if native_key:
            key = native_key.replace("${arch}", "32" if IS_32BIT else "64")
            classifier = (downloads.get("classifiers") or {}).get(key)
            if classifier and classifier.get("url"):
                rel = f"libraries/{classifier['path']}"
                tasks.append({
                    "url": classifier["url"], "rel": rel,
                    "sha1": classifier.get("sha1", ""),
                    "size": classifier.get("size", 0), "kind": "本地库",
                })
                natives.append(rel)

        if not artifact and not downloads:
            url = maven_url(name)
            if url:
                rel = "libraries/" + url.split("libraries.minecraft.net/", 1)[-1]
                tasks.append({"url": url, "rel": rel, "sha1": "", "size": 0, "kind": "依赖库"})

    # 日志配置
    logging_conf = (vjson.get("logging") or {}).get("client")
    if logging_conf and logging_conf.get("file"):
        f = logging_conf["file"]
        tasks.append({
            "url": f["url"],
            "rel": f"assets/log_configs/{f['id']}",
            "sha1": f.get("sha1", ""), "size": f.get("size", 0), "kind": "日志配置",
        })

    # 资源索引（必下），资源对象按开关
    asset_index = vjson.get("assetIndex")
    asset_objects: list[dict] = []
    if asset_index:
        tasks.append({
            "url": asset_index["url"],
            "rel": f"assets/indexes/{asset_index['id']}.json",
            "sha1": asset_index.get("sha1", ""),
            "size": asset_index.get("size", 0),
            "kind": "资源索引",
        })
        if want_assets:
            index_data = fetch_json(asset_index["url"])
            for obj in index_data.get("objects", {}).values():
                h = obj["hash"]
                asset_objects.append({
                    "url": f"https://resources.download.minecraft.net/{h[:2]}/{h}",
                    "rel": f"assets/objects/{h[:2]}/{h}",
                    "sha1": h, "size": obj.get("size", 0), "kind": "资源文件",
                })

    return {
        "version_id": vid,
        "tasks": tasks,
        "assets": asset_objects,
        "natives": natives,
        "asset_index_id": asset_index["id"] if asset_index else "",
        "asset_size": asset_index.get("totalSize", 0) if asset_index else 0,
    }


# --------------------------------------------------------------------------- 下载执行
class Progress:
    def __init__(self, total_files: int, total_bytes: int, quiet: bool = False):
        self.total_files = total_files
        self.total_bytes = max(total_bytes, 1)
        self.done_files = 0
        self.done_bytes = 0
        self.quiet = quiet or not sys.stdout.isatty()
        self.lock = threading.Lock()
        self.start = time.time()
        self.last = 0.0

    def add(self, size: int) -> None:
        with self.lock:
            self.done_files += 1
            self.done_bytes += size
            now = time.time()
            if not self.quiet and now - self.last > 0.2:
                self.last = now
                self.render()
        if self.quiet and self.done_files % 200 == 0:
            log(f"  已下载 {self.done_files}/{self.total_files} 个文件，"
                f"{human(self.done_bytes)} / {human(self.total_bytes)}")

    def render(self) -> None:
        pct = min(self.done_bytes / self.total_bytes * 100, 100)
        elapsed = max(time.time() - self.start, 0.1)
        speed = self.done_bytes / elapsed
        bar_len = 24
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        sys.stdout.write(
            f"\r  [{bar}] {pct:5.1f}%  {human(self.done_bytes)}/{human(self.total_bytes)}"
            f"  {human(speed)}/s  "
        )
        sys.stdout.flush()

    def finish(self) -> None:
        if not self.quiet:
            self.render()
            sys.stdout.write("\n")
            sys.stdout.flush()
        elapsed = time.time() - self.start
        log(f"  下载完成：{self.total_files} 个文件，{human(self.done_bytes)}，用时 {elapsed:.0f} 秒")


def download_one(task: dict, root: Path, progress: Progress,
                 verify: bool = False) -> bool:
    dest = root / task["rel"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    expect_size = task.get("size") or 0

    if dest.exists():
        ok = True
        if verify and task.get("sha1"):
            ok = sha1_of(dest) == task["sha1"]
        elif expect_size:
            ok = dest.stat().st_size == expect_size
        if ok:
            progress.add(expect_size or dest.stat().st_size)
            return True

    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(1, 4):
        try:
            with http_open(task["url"], timeout=45) as resp, tmp.open("wb") as fh:
                h = hashlib.sha1()
                got = 0
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
            if task.get("sha1") and h.hexdigest() != task["sha1"]:
                raise ValueError("sha1 校验失败")
            tmp.replace(dest)
            progress.add(got)
            return True
        except Exception as err:
            tmp.unlink(missing_ok=True)
            if attempt >= 3:
                log(f"  ✗ {task['rel']}：{err}")
                return False
            time.sleep(attempt)
    return False


def run_download(plan: dict, root: Path, threads: int, verify: bool = False) -> bool:
    tasks = plan["tasks"] + plan["assets"]
    total_bytes = sum(t.get("size") or 0 for t in tasks)
    progress = Progress(len(tasks), total_bytes)
    log(f"  共 {len(tasks)} 个文件，约 {human(total_bytes)}")

    ok_count = 0
    lock = threading.Lock()

    def worker(task: dict) -> None:
        nonlocal ok_count
        success = download_one(task, root, progress, verify)
        with lock:
            if success:
                ok_count += 1

    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        list(pool.map(worker, tasks))

    progress.finish()
    if ok_count != len(tasks):
        log(f"  ⚠ 有 {len(tasks) - ok_count} 个文件没下成功，重新运行一次通常会补上。")
        return False
    return True


def extract_natives(natives: list[str], root: Path) -> int:
    if not natives:
        return 0
    out_dir = root / "natives"
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for rel in natives:
        jar = root / rel
        if not jar.exists():
            continue
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if name.lower().endswith((".dll", ".so", ".dylib")):
                        data = zf.read(name)
                        (out_dir / Path(name).name).write_bytes(data)
                        count += 1
        except zipfile.BadZipFile:
            continue
    return count


# --------------------------------------------------------------------------- 生成启动器
def build_launch_arguments(vjson: dict, plan: dict, ctx: dict) -> list[str]:
    def expand(entries: list, features: dict | None = None) -> list[str]:
        out: list[str] = []
        for item in entries:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                if not rule_allows(item.get("rules"), features):
                    continue
                value = item.get("value", [])
                out.extend([value] if isinstance(value, str) else value)
        return out

    arguments = vjson.get("arguments") or {}
    jvm = expand(arguments.get("jvm") or [])
    game = expand(arguments.get("game") or [], features={})

    if not game and vjson.get("minecraftArguments"):
        game = vjson["minecraftArguments"].split()
    if not jvm:
        jvm = ["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"]

    placeholders = {
        "auth_player_name": ctx["username"],
        "version_name": vjson["id"],
        "game_directory": ".",
        "assets_root": "assets",
        "assets_index_name": plan["asset_index_id"] or vjson.get("assets", "legacy"),
        "auth_uuid": ctx["uuid"],
        "auth_access_token": "0",
        "auth_session": "0",
        "clientid": "0",
        "auth_xuid": "0",
        "user_type": "legacy",
        "version_type": vjson.get("type", "release"),
        "natives_directory": "natives",
        "launcher_name": APP_NAME,
        "launcher_version": APP_VERSION,
        "classpath": ctx["classpath"],
        "classpath_separator": ";",
        "library_directory": "libraries",
        "resolution_width": "854",
        "resolution_height": "480",
    }

    def sub(token: str) -> str:
        for key, value in placeholders.items():
            token = token.replace("${" + key + "}", value)
        return token

    log_conf = (vjson.get("logging") or {}).get("client")
    if log_conf and log_conf.get("file"):
        jvm.append(f"-Dlog4j.configurationFile=assets/log_configs/{log_conf['file']['id']}")

    args = [f"-Xmx{ctx['memory']}", f"-Xms512M"] + [sub(t) for t in jvm]
    args += [vjson.get("mainClass", "net.minecraft.client.main.Main")]
    args += [sub(t) for t in game]
    return args


def write_launcher(root: Path, vjson: dict, plan: dict, args: list[str]) -> None:
    # Java 的 @argfile：一行一个参数，路径统一用正斜杠，避免反斜杠被当成转义符
    arg_lines = []
    for a in args:
        a = a.replace("\\", "/")
        if not a:
            a = "0"
        arg_lines.append(f'"{a}"' if " " in a else a)
    (root / "launch.args").write_text("\n".join(arg_lines) + "\n", encoding="utf-8", newline="\n")

    bat = f"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Minecraft {vjson['id']}
where java >nul 2>nul
if errorlevel 1 (
    echo.
    echo  [错误] 没有找到 Java，请先安装 Java 21 或更高版本。
    echo  下载地址：https://www.oracle.com/java/technologies/downloads/
    echo.
    pause
    exit /b 1
)
echo 正在启动 Minecraft {vjson['id']} ...
java @"launch.args"
echo.
echo 游戏已退出。
pause
"""
    (root / "启动游戏.bat").write_text(bat, encoding="utf-8", newline="\r\n")


def check_java(required: int) -> None:
    java = shutil.which("java")
    if not java:
        log("  ⚠ 没有找到 Java，下载完后需要先装 Java 才能启动游戏。")
        return
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True,
                             timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stderr or out.stdout
        m = re.search(r'version "(\d+)', text)
        if m:
            major = int(m.group(1))
            if major < required:
                log(f"  ⚠ 你的 Java 是 {major}，这个版本需要 Java {required}+，建议升级。")
            else:
                log(f"  Java {major} 可用。")
    except Exception:
        pass


# --------------------------------------------------------------------------- 界面交互
def pick_folder(initial: Path) -> str | None:
    """弹出文件夹选择框，返回路径；没有图形界面时返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(
            title="选择我的世界下载位置", initialdir=str(initial), mustexist=False
        )
        root.destroy()
        return path or ""
    except Exception:
        return None


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return "C"


def choose_location(default: Path, folder_name: str, auto_yes: bool) -> Path | None:
    """选文件夹 → 确认 → 返回最终下载根目录（None 表示取消）。"""
    root = default
    while True:
        target = root / folder_name
        log("")
        log(f"  下载位置：{target}")
        log(f"  （默认是「下载」文件夹，可以按 N 换一个地方）")
        answer = "Y" if auto_yes else ask("  是否下载到该文件夹？[Y] 继续  [N] 修改  [C] 取消 : ")
        answer = (answer or "Y").upper()[:1]
        if answer == "Y":
            return root
        if answer == "N":
            picked = pick_folder(root)
            if picked is None:
                typed = ask("  请输入下载文件夹路径（直接回车取消）: ").strip('" ')
                if not typed:
                    return None
                root = Path(typed)
            elif picked:
                root = Path(picked)
        else:
            return None


# --------------------------------------------------------------------------- 命令实现
def cmd_download(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="download Minecraft", add_help=True)
    ap.add_argument("target", help="系列或版本号，如 26.1 或 26.1.2")
    ap.add_argument("version", nargs="?", help="具体版本号，如 26.1.2")
    ap.add_argument("--dir", help="直接指定下载根目录（不弹选择框）")
    ap.add_argument("--no-assets", action="store_true", help="跳过资源文件（约 400+ MB）")
    ap.add_argument("--yes", action="store_true", help="自动确认，不询问")
    ap.add_argument("--dry-run", action="store_true", help="只显示要下载什么，不实际下载")
    ap.add_argument("--verify", action="store_true", help="对已存在文件做 sha1 校验")
    ap.add_argument("--username", help="游戏内用户名，默认 Player")
    ap.add_argument("--memory", help="最大内存，如 2G / 4G")
    ap.add_argument("--threads", type=int, help="下载并发数")
    ap.add_argument("--no-mirror", action="store_true", help="不查询镜像站，直接用官方清单")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.no_mirror:
        cfg["repo"] = ""
    username = args.username or cfg.get("username") or "Player"
    memory = args.memory or cfg.get("memory") or "2G"
    threads = args.threads or int(cfg.get("threads") or 8)
    want_assets = not args.no_assets and bool(cfg.get("assets", True))

    log("=" * 60)
    log(f"  {APP_NAME} {APP_VERSION}   Minecraft 版本下载器")
    log("=" * 60)

    log("· 读取版本数据库 ...")
    try:
        manifest, source = load_manifest(cfg)
    except RuntimeError as err:
        log(f"  ✗ {err}")
        return 1
    log(f"  来源：{source}（共 {len(manifest.get('versions', []))} 个版本）")
    if not mirror_available(cfg):
        log("  提示：还没配置镜像站仓库，先用官方清单顶着。")
        log("        部署好 GitHub 仓库后运行：download setup 你的用户名/mc-mirror")

    wanted = args.version or args.target
    entry = resolve_version(manifest, wanted)
    if not entry:
        log(f"  ✗ 找不到版本「{wanted}」，可以用 download list {series_of(wanted)} 查一下。")
        return 1
    vid = entry["id"]
    log(f"· 目标版本：{vid}（{entry.get('type')}，{entry.get('releaseTime', '')[:10]}）")

    log("· 读取版本详情 ...")
    try:
        vjson = fetch_version_json(cfg, entry)
    except Exception as err:
        log(f"  ✗ 版本详情读取失败：{err}")
        return 1

    folder_name = f"Minecraft {series_of(vid)}"
    plan = build_plan(vjson, Path("."), want_assets)

    log("· 计算下载内容 ...")
    base_size = sum(t.get("size") or 0 for t in plan["tasks"])
    asset_size = sum(t.get("size") or 0 for t in plan["assets"])
    log(f"  游戏文件 {len(plan['tasks'])} 个，约 {human(base_size)}")
    if want_assets:
        log(f"  资源文件 {len(plan['assets'])} 个，约 {human(asset_size)}")
    else:
        log("  资源文件：已跳过（游戏没有音效，但能正常玩）")
    log(f"  合计约 {human(base_size + asset_size)}")

    if args.dry_run:
        log("")
        log("  --dry-run 模式，不下载。前 10 个文件：")
        for t in plan["tasks"][:10]:
            log(f"    {t['rel']}  ({human(t['size'])})")
        return 0

    # 选择下载位置
    if args.dir:
        root = Path(args.dir).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        log(f"\n  下载到：{root / folder_name}")
    else:
        root = choose_location(downloads_dir(), folder_name,
                               args.yes or not sys.stdin.isatty())
        if root is None:
            log("  已取消。")
            return 0

    target = root / folder_name
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        log(f"  ✗ 无法创建文件夹 {target}：{err}")
        return 1

    log("")
    log(f"· 开始下载到 {target}")
    check_java(int((vjson.get("javaVersion") or {}).get("majorVersion", 21)))

    ok = run_download(plan, target, threads, args.verify)

    log("· 解压本地库 ...")
    n = extract_natives(plan["natives"], target)
    log(f"  解压 {n} 个本地库文件到 natives/")

    log("· 生成启动文件 ...")
    (target / "versions" / vid).mkdir(parents=True, exist_ok=True)
    (target / "versions" / vid / f"{vid}.json").write_text(
        json.dumps(vjson, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    classpath_entries: list[str] = [f"versions/{vid}/{vid}.jar"]
    classpath_entries += [t["rel"] for t in plan["tasks"]
                          if t["rel"].startswith("libraries/")]
    ctx = {
        "username": username,
        "uuid": offline_uuid(username),
        "memory": memory,
        "classpath": ";".join(classpath_entries),
    }
    launch_args = build_launch_arguments(vjson, plan, ctx)
    write_launcher(target, vjson, plan, launch_args)

    log("")
    if ok:
        log("=" * 60)
        log(f"  ✅ 下载完成：{target}")
        log(f"  双击「启动游戏.bat」就能玩（用户名：{username}）")
        log("=" * 60)
        return 0
    log("  ⚠ 下载不完整，但启动文件已经生成。再运行一次同样的命令可以补齐。")
    return 2


def cmd_list(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="download list")
    ap.add_argument("series", nargs="?", help="系列号，如 26.1；不填就列最新的几个系列")
    args = ap.parse_args(argv)
    cfg = load_config()
    manifest, source = load_manifest(cfg)
    versions = manifest.get("versions", [])

    if not args.series:
        groups: dict[str, list] = {}
        for v in versions:
            groups.setdefault(series_of(v["id"]), []).append(v)
        for s in list(groups)[:12]:
            rel = [v for v in groups[s] if v["type"] == "release"]
            mark = " ✓" if rel else ""
            print(f"  {s:<12} 正式版 {len(rel):>2} 个{mark}")
        print("\n  用法：download list 26.1")
        return 0

    hit = [v for v in versions if v["id"].lower().startswith(args.series.lower())]
    if not hit:
        print(f"  没有找到 «{args.series}» 相关的版本")
        return 1
    hit.sort(key=lambda v: v.get("releaseTime", ""), reverse=True)
    print(f"  {args.series} 相关版本（{source}）：")
    for v in hit:
        tag = "正式版" if v["type"] == "release" else "快照"
        print(f"    {v['id']:<26} {tag}  {v.get('releaseTime', '')[:10]}")
    return 0


def cmd_setup(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="download setup")
    ap.add_argument("repo", help="GitHub 仓库，如 owner/mc-mirror，也可以直接贴仓库网址")
    args = ap.parse_args(argv)
    repo = args.repo.strip().rstrip("/")
    if repo.startswith("http"):
        parts = repo.split("github.com/", 1)[-1].split("/")
        if len(parts) >= 2:
            repo = f"{parts[0]}/{parts[1]}"
    cfg = load_config()
    cfg["repo"] = repo
    save_config(cfg)
    (CACHE_DIR / "manifest.json").unlink(missing_ok=True)  # 清掉旧缓存，下次立刻用新仓库
    print(f"  ✅ 镜像站仓库已设置为 {repo}")
    try:
        fetch_json(f"{RAW_BASE.format(repo=repo)}/manifest.json")
        print("  ✅ 镜像站数据库连接正常")
    except Exception:
        print("  ⚠ 暂时连不上这个仓库的数据库，检查仓库名、分支是否为 main、是否已推送 index/ 目录")
    return 0


def cmd_info(argv: list[str]) -> int:
    cfg = load_config()
    print(f"  版本：{APP_NAME} {APP_VERSION}")
    print(f"  配置文件：{CONFIG_PATH}")
    print(f"  镜像站仓库：{cfg.get('repo') or '（未配置）'}")
    print(f"  默认用户名：{cfg.get('username')}   内存：{cfg.get('memory')}   并发：{cfg.get('threads')}")
    print(f"  下载默认位置：{downloads_dir()}")
    if CACHE_DIR.exists():
        size = sum(f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file())
        print(f"  缓存：{human(size)}")
    return 0


def print_help() -> None:
    log(f"""
  {APP_NAME} {APP_VERSION} —— 我的世界版本一键下载器

  常用命令：
    download Minecraft 26.1              下载 26.1 系列最新正式版
    download Minecraft 26.1 26.1.2       下载 26.1.2
    download Minecraft 26.1 26.1.2 --dir D:\\Games
    download list 26.1                   查看 26.1 系列有哪些版本
    download setup 用户名/mc-mirror      连接你的 GitHub 镜像站
    download info                        查看配置

  小技巧：
    --no-assets   不下载资源文件（省 400MB，但没有音效）
    --yes         不询问，直接下到「下载」文件夹
    --dry-run     只看要下载什么，不真下
""")


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print_help()
        return 0
    cmd = argv[0].lower()
    if cmd in ("minecraft", "mc", "下载"):
        return cmd_download(argv[1:])
    if cmd == "list":
        return cmd_list(argv[1:])
    if cmd == "setup":
        return cmd_setup(argv[1:])
    if cmd == "info":
        return cmd_info(argv[1:])
    print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
