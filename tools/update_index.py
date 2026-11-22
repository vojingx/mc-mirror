#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 hello 镜像站的版本数据库（index/ 目录）。

数据源是 Mojang 官方的版本清单，我们只保存“元数据”（版本号、文件地址、校验值），
不保存游戏本体文件，所以仓库很小，也不会涉及游戏文件的再分发问题。

用法：
  python tools/update_index.py                     # 更新清单 + 镜像最新 3 个系列
  python tools/update_index.py --no-mirror         # 只更新清单，不下载版本 JSON
  python tools/update_index.py --mirror-series 26.1 --mirror-latest-series 0
  python tools/update_index.py --mirror-all-releases   # 镜像全部正式版（仓库会变大，慎用）
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OFFICIAL_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
USER_AGENT = "hello-mc-mirror/1.0 (GitHub Actions index builder)"
ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = ROOT / "index"
VERSIONS_DIR = INDEX_DIR / "versions"


# ---------------------------------------------------------------- 网络工具
def fetch(url: str, retries: int = 4, timeout: int = 30) -> bytes:
    """带重试的 GET，返回原始字节。"""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last_err = err
            if attempt < retries:
                time.sleep(attempt * 1.5)
    raise RuntimeError(f"下载失败：{url}（{last_err}）")


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url).decode("utf-8"))


# ---------------------------------------------------------------- 版本归类
def series_of(version_id: str) -> str:
    """把版本号归到“系列”：26.1.2 -> 26.1，1.21.4 -> 1.21，26.1-snapshot-3 -> 26.1。"""
    base = version_id.split("-")[0]
    parts = base.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else base


def dump(path: Path, obj) -> bool:
    """写 JSON（缩进 2、UTF-8、LF），内容没变就不写，返回是否发生变化。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


# ---------------------------------------------------------------- 主流程
def build_manifest(official: dict) -> dict:
    """把官方清单瘦身成镜像站清单（保留客户端需要的字段）。"""
    versions = []
    for v in official["versions"]:
        versions.append(
            {
                "id": v["id"],
                "type": v["type"],
                "releaseTime": v["releaseTime"],
                "url": v["url"],
                "sha1": v.get("sha1", ""),
                "size": v.get("size", 0),
                "complianceLevel": v.get("complianceLevel", 0),
            }
        )
    return {
        "schema": 1,
        "name": "hello Minecraft 版本数据库",
        "source": OFFICIAL_MANIFEST,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest": official.get("latest", {}),
        "count": len(versions),
        "versions": versions,
    }


def build_summary(manifest: dict, mirrored: dict[str, str]) -> dict:
    """给网页用的分组摘要：每个系列有哪些版本、最新正式版是哪个。"""
    groups: dict[str, dict] = {}
    for v in manifest["versions"]:
        s = series_of(v["id"])
        g = groups.setdefault(
            s,
            {"series": s, "releases": [], "snapshots": [], "latest_release": None,
             "latest_snapshot": None, "latest_time": v["releaseTime"], "mirrored": []},
        )
        if v["type"] == "release":
            g["releases"].append(v["id"])
        else:
            g["snapshots"].append(v["id"])
        if v["releaseTime"] > g["latest_time"]:
            g["latest_time"] = v["releaseTime"]
        if v["id"] in mirrored:
            g["mirrored"].append(v["id"])

    # 最新正式版要按时间排序后再取，上面的循环顺序不可靠，这里重算一次
    for s, g in groups.items():
        rel = [
            v for v in manifest["versions"]
            if v["type"] == "release" and series_of(v["id"]) == s
        ]
        rel.sort(key=lambda x: x["releaseTime"], reverse=True)
        snaps = [
            v for v in manifest["versions"]
            if v["type"] == "snapshot" and series_of(v["id"]) == s
        ]
        snaps.sort(key=lambda x: x["releaseTime"], reverse=True)
        g["latest_release"] = rel[0]["id"] if rel else None
        g["latest_snapshot"] = snaps[0]["id"] if snaps else None
        g["releases"] = [v["id"] for v in rel]
        g["snapshots"] = [v["id"] for v in snaps]
        g["release_count"] = len(rel)
        g["snapshot_count"] = len(snaps)

    ordered = sorted(groups.values(), key=lambda g: g["latest_time"], reverse=True)
    return {
        "schema": 1,
        "generated": manifest["generated"],
        "latest": manifest.get("latest", {}),
        "group_count": len(ordered),
        "groups": ordered,
    }


def mirror_versions(manifest: dict, series_list: list[str], max_per_series: int,
                    mirrored: dict[str, str]) -> int:
    """下载指定系列的正式版版本 JSON，存到 index/versions/<id>.json。"""
    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    index = {v["id"]: v for v in manifest["versions"]}
    changed = 0

    for series in series_list:
        targets = [
            v for v in manifest["versions"]
            if v["type"] == "release" and series_of(v["id"]) == series
        ]
        targets.sort(key=lambda x: x["releaseTime"], reverse=True)
        if max_per_series > 0:
            targets = targets[:max_per_series]
        for v in reversed(targets):  # 旧版本先下，日志看起来更顺
            vid = v["id"]
            path = VERSIONS_DIR / f"{vid}.json"
            mirrored[vid] = f"versions/{vid}.json"
            # 已有且 sha1 一致就跳过，省流量
            if path.exists() and v.get("sha1"):
                if hashlib.sha1(path.read_bytes()).hexdigest() == v["sha1"]:
                    continue
            raw = fetch(v["url"])
            obj = json.loads(raw.decode("utf-8"))
            if dump(path, obj):
                changed += 1
                print(f"  镜像 {vid:<24} {len(raw)//1024:>4} KB")
            time.sleep(0.4)  # 对官方服务器友好一点
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 hello 镜像站的版本数据库")
    ap.add_argument("--mirror-latest-series", type=int, default=3,
                    help="镜像最近 N 个系列的全部正式版（默认 3，0 表示不镜像）")
    ap.add_argument("--mirror-series", action="append", default=[],
                    help="额外指定要镜像的系列，可重复，如 --mirror-series 26.1")
    ap.add_argument("--mirror-max-per-series", type=int, default=30,
                    help="每个系列最多镜像几个正式版（默认 30）")
    ap.add_argument("--mirror-all-releases", action="store_true",
                    help="镜像全部正式版（会明显增大仓库体积）")
    ap.add_argument("--no-mirror", action="store_true", help="只更新清单，不下载版本 JSON")
    args = ap.parse_args()

    print(f"读取官方清单 {OFFICIAL_MANIFEST}")
    official = fetch_json(OFFICIAL_MANIFEST)
    manifest = build_manifest(official)
    print(f"  共 {manifest['count']} 个版本，最新正式版 {manifest['latest'].get('release')}")

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    # 版本没变化就沿用原来的生成时间，避免定时任务每天产生“空提交”
    manifest_path = INDEX_DIR / "manifest.json"
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (old.get("versions") == manifest["versions"]
                    and old.get("latest") == manifest["latest"]):
                manifest["generated"] = old.get("generated", manifest["generated"])
        except (OSError, ValueError):
            pass

    dump(manifest_path, manifest)

    mirrored: dict[str, str] = {}
    if not args.no_mirror:
        series_list = list(args.mirror_series)
        if args.mirror_all_releases:
            series_list = sorted({series_of(v["id"]) for v in manifest["versions"]
                                  if v["type"] == "release"})
        elif args.mirror_latest_series > 0:
            groups = build_summary(manifest, {})["groups"]
            recent = [g["series"] for g in groups if g["releases"]]
            series_list += recent[: args.mirror_latest_series]
        series_list = list(dict.fromkeys(series_list))  # 去重保持顺序
        print(f"镜像系列：{', '.join(series_list) or '（无）'}")
        mirror_versions(manifest, series_list, args.mirror_max_per_series, mirrored)

    summary = build_summary(manifest, mirrored)
    dump(INDEX_DIR / "summary.json", summary)

    meta = {
        "schema": 1,
        "generated": manifest["generated"],
        "source": OFFICIAL_MANIFEST,
        "version_count": manifest["count"],
        "latest": manifest["latest"],
        "mirrored": mirrored,
    }
    dump(INDEX_DIR / "meta.json", meta)

    print(f"完成：{len(mirrored)} 个版本 JSON 已镜像，清单写入 index/manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
