# -*- coding: utf-8 -*-
"""一次性补丁：修正缓存来源判断与数据库时间戳，运行后随工作流一起删除。"""
import pathlib

DL = pathlib.Path("cli/download.py")
UI = pathlib.Path("tools/update_index.py")


def sub(path, old, new):
    text = path.read_text(encoding="utf-8")
    hits = text.count(old)
    if hits != 1:
        raise SystemExit(f"{path}: 目标片段命中 {hits} 次，放弃打补丁")
    path.write_text(text.replace(old, new), encoding="utf-8")


sub(DL, '''            if time.time() - cached.get("_fetched", 0) < 6 * 3600:
                return cached["data"], cached.get("_source", "本地缓存")''',
    '''            # 换了镜像站仓库就重新读，避免用到旧来源的缓存
            if (cached.get("_repo", "") == cfg.get("repo", "")
                    and time.time() - cached.get("_fetched", 0) < 6 * 3600):
                return cached["data"], cached.get("_source", "本地缓存")''')

sub(DL, '''                    json.dumps({"_fetched": time.time(), "_source": label, "data": data}),''',
    '''                    json.dumps({"_fetched": time.time(), "_source": label,
                                "_repo": cfg.get("repo", ""), "data": data}),''')

sub(DL, '''    save_config(cfg)
    print(f"  ✅ 镜像站仓库已设置为 {repo}")''',
    '''    save_config(cfg)
    (CACHE_DIR / "manifest.json").unlink(missing_ok=True)  # 清掉旧缓存，下次立刻用新仓库
    print(f"  ✅ 镜像站仓库已设置为 {repo}")''')

sub(UI, '''    dump(INDEX_DIR / "manifest.json", manifest)''',
    '''    # 版本没变化就沿用原来的生成时间，避免定时任务每天产生“空提交”
    manifest_path = INDEX_DIR / "manifest.json"
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (old.get("versions") == manifest["versions"]
                    and old.get("latest") == manifest["latest"]):
                manifest["generated"] = old.get("generated", manifest["generated"])
        except (OSError, ValueError):
            pass

    dump(manifest_path, manifest)''')

print("补丁已应用")
