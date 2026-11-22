# download · Minecraft 版本镜像站

一个 GitHub 仓库 + 一个命令行小工具。在命令行里敲：

```
download Minecraft 26.1 26.1.2
```

它就会弹出文件夹选择框（默认停在「下载」文件夹），确认后把 26.1.2 下载到
`<你选的文件夹>\Minecraft 26.1`，并自动生成 **启动游戏.bat**，双击就能玩。

---

## 1. 它是怎么跑起来的

```
download Minecraft 26.1 26.1.2
        │
        ├─ ① 读版本数据库（优先镜像站，连不上就用 Mojang 官方清单）
        ├─ ② 弹出文件夹选择框，默认「下载」文件夹
        ├─ ③ 问你：是否下载到该文件夹？ [Y] 继续  [N] 修改  [C] 取消
        ├─ ④ 下载客户端 + 依赖库 + 资源文件（全部做 sha1 校验）
        └─ ⑤ 生成 Minecraft 26.1\启动游戏.bat
```

下载完的文件夹结构，和官方启动器的 `.minecraft` 是同一套：

```
Minecraft 26.1\
├─ 启动游戏.bat          ← 双击这个
├─ launch.args           ← 启动参数（bat 会读它）
├─ versions\26.1.2\      ← 客户端 jar + 版本 json
├─ libraries\            ← 依赖库
├─ assets\               ← 音效、语言、图标等资源
└─ natives\              ← 本地库（.dll）
```

---

## 2. 先在自己电脑上试（3 步）

1. 装 Python 3.9 或更高版本（安装时记得勾 **Add python.exe to PATH**）。
2. 双击 `cli\install.bat`，它会把 `download` 命令装进 PATH。
3. **重新打开一个命令行窗口**，输入：

```
download Minecraft 26.1
```

> 已经是 Windows 11 的话，也可以直接在文件夹里 `Shift + 右键 → 在此处打开终端`。

---

## 3. 部署成你自己的镜像站（GitHub）

### 3.1 建仓库并推代码

在 GitHub 新建一个 Public 仓库，名字建议 `mc-mirror`（**不要**勾选自动生成 README）。
然后在本文件夹里执行（把 `你的用户名` 换成自己的）：

```bash
git init
git add .
git commit -m "init: Minecraft 版本镜像站"
git branch -M main
git remote add origin https://github.com/你的用户名/mc-mirror.git
git push -u origin main
```

### 3.2 自动更新（已内置）

`.github/workflows/update-index.yml` 会在每天北京时间 10:17 自动去 Mojang 拉最新版本清单，
有新版本就自动提交。什么都不用配，推上去就生效。

也可以在 Actions 页面点 **Run workflow** 手动跑一次。

### 3.3 可选：开一个网页版浏览页

仓库里已经带了 `site/index.html`（搜索版本、一键复制下载命令的页面）。

Settings → Pages → Source 选 **Deploy from a branch** → 分支 `main`、目录 `/ (root)` → 保存。
一分钟后访问：

```
https://你的用户名.github.io/mc-mirror/site/
```

### 3.4 让工具连上你的仓库

```
download setup 你的用户名/mc-mirror
```

看到「镜像站数据库连接正常」就成功了。之后所有下载都会优先读你自己的数据库，
自己的仓库挂了或者没这个版本，会自动退回 Mojang 官方，不会卡住。

### 3.5 分享给朋友

把仓库地址发给他们，让他们各跑一次：

```
download setup 你的用户名/mc-mirror
download Minecraft 26.1
```

---

## 4. 命令手册

| 命令 | 作用 |
| --- | --- |
| `download Minecraft 26.1` | 下载 26.1 系列最新正式版 |
| `download Minecraft 26.1 26.1.2` | 下载指定版本 |
| `download list 26.1` | 列出 26.1 系列所有版本（含快照） |
| `download list` | 看最近十几个系列 |
| `download setup 用户名/仓库` | 连接你的镜像站仓库 |
| `download info` | 查看配置、缓存大小、默认下载位置 |
| `download --help` | 帮助 |

常用选项：

| 选项 | 说明 |
| --- | --- |
| `--dir D:\Games` | 跳过选择框，直接下到指定目录 |
| `--yes` | 不询问，直接下到「下载」文件夹 |
| `--no-assets` | 跳过 400+ MB 资源文件（能玩，但没音效） |
| `--dry-run` | 只显示要下多少、下什么，不真下 |
| `--memory 4G` | 给游戏分配 4G 内存 |
| `--username 小明` | 游戏里的离线用户名 |
| `--threads 16` | 提高并发（默认 8） |
| `--verify` | 对已下载文件重新做 sha1 校验 |

---

## 5. 数据库里有什么

```
index/
├─ manifest.json       全部 925 个版本的元数据（id、类型、发布时间、官方地址、sha1）
├─ versions/*.json     常用系列的完整版本详情（依赖库列表、资源索引等）
├─ summary.json        网页用的分组摘要
└─ meta.json           生成时间、镜像了哪些版本
```

`tools/update_index.py` 负责生成它们：

```bash
python tools/update_index.py                        # 默认：更新清单 + 镜像最近 3 个系列
python tools/update_index.py --mirror-series 26.1    # 额外镜像指定系列
python tools/update_index.py --no-mirror             # 只更新清单，不下载版本详情
```

---

## 6. 为什么仓库里不放游戏文件

两个实在的原因：

1. **放不下**：单个版本的客户端 38 MB 还能勉强塞进 GitHub，但资源文件 440 MB、
   每个版本一套，几百个版本就是几个 TB，GitHub 单文件限制 100 MB，根本装不下。
2. **不该放**：游戏本体是 Mojang 的版权文件，公开再分发不合适。

所以这里镜像的是**版本索引**（版本号、文件地址、校验值），真正的游戏文件由客户端
从 Mojang 官方 CDN 按 sha1 校验后下载。好处是仓库只有 1 MB 出头、克隆飞快，
而且以后新增版本时数据库会自动更新，不用重新传文件。

如果你确实想做“文件级镜像”（比如给校园网做加速），可以在 `tools/update_index.py` 里
给版本 JSON 的下载地址加一个你自己的对象存储前缀，客户端不用改。

---

## 7. 常见问题

**下载到一半失败了？**
重新运行同一条命令，已经下好的文件会被跳过，只补缺的。

**提示 Java 版本不够？**
26.x 系列需要 Java 25 及以上。`启动游戏.bat` 启动前会检查，版本太低会提示下载地址。
Java 21 可以跑 1.20～1.21 系列。

**提示内存不足 / 卡顿？**
`download Minecraft 26.1 26.1.2 --memory 4G`，或在游戏里把渲染距离调小。

**能不能连服务器？**
离线模式进不了正版服务器。单人游戏、局域网联机没问题；上正版服请用正版账号登录的启动器。

**镜像站连不上？**
工具会自动退回 Mojang 官方清单，不影响下载。检查一下：仓库是不是 Public、
分支是不是 `main`、`index/manifest.json` 有没有推上去。

---

## 8. 许可

本仓库的代码（`cli/`、`tools/`、`site/`）使用 MIT 许可。
Minecraft 是 Mojang Studios 的商标与作品，本项目与 Mojang 无关联。
