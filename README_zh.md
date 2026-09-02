# OpenClaw 在 CasaOS 的 arm64 部署说明

你当前机器是 arm64（aarch64），原应用商店里的 `icewhaletech/openclaw:2026.5.7` 实际是 amd64，
会出现 `Exec format error` 并不断重启。

本目录提供了可在 arm64 上使用的 Compose：`openclaw/openclaw:latest`。

## 0. 终端快捷入口（最小改动）

新增了一个轻量脚本 [oc.sh](oc.sh)，只封装常用命令，不改现有部署逻辑。

```bash
chmod +x /home/pi/NAS-Demo/oc.sh
/home/pi/NAS-Demo/oc.sh deploy
/home/pi/NAS-Demo/oc.sh status
/home/pi/NAS-Demo/oc.sh logs
/home/pi/NAS-Demo/oc.sh health
/home/pi/NAS-Demo/oc.sh url
/home/pi/NAS-Demo/oc.sh model
/home/pi/NAS-Demo/oc.sh tools-nas-setup
/home/pi/NAS-Demo/oc.sh tools-nas-show
/home/pi/NAS-Demo/oc.sh tools-media-setup
/home/pi/NAS-Demo/oc.sh tools-media-show
/home/pi/NAS-Demo/oc.sh tools-kb-setup     # 配置知识库搜索（kb_search MCP）
/home/pi/NAS-Demo/oc.sh tools-kb-show       # 查看知识库搜索 MCP 配置
/home/pi/NAS-Demo/oc.sh tools-sync          # 统一同步源码→运行副本（改完代码后执行）
/home/pi/NAS-Demo/oc.sh pair-list
/home/pi/NAS-Demo/oc.sh pair-approve <request_id>
/home/pi/NAS-Demo/oc.sh jellyfin-deploy   # 部署 Jellyfin（家庭影院播放）
/home/pi/NAS-Demo/oc.sh jellyfin-show     # 查看 Jellyfin 容器状态
/home/pi/NAS-Demo/oc.sh nas-files-deploy   # 部署 NAS 文件浏览（只读 nas_share）
/home/pi/NAS-Demo/oc.sh nas-files-show     # 查看文件浏览容器状态
```

`pair-approve` 会自动去掉你从页面复制时常见的末尾中文句号 `。`。

`tools-nas-setup` 会注册一个 MCP 文件工具服务器（`nas_files`），
并把文件操作范围限制到容器内的 `/nas_share`（对应宿主机 `/home/pi/nas_share`）。

> **代码维护约定（唯一源码原则）**：
> 所有运行脚本的唯一源码在 `NAS-Demo/`（git 仓库），`/home/pi/nas_share/tools/`
> 只是容器可见的运行副本，**不进 git、不要手动改**。
> 修改代码后执行 `./oc.sh tools-sync` 统一同步（并自动清理遗留副本），
> 其中 `download_media_mcp.js` / `kb_mcp.js` 有变更时还会重启 openclaw 使 MCP 配置生效。

## 0.1 文件工作流自动化（最小改动）

执行以下命令即可启用文件管理工具并重启容器：

```bash
/home/pi/NAS-Demo/oc.sh deploy
/home/pi/NAS-Demo/oc.sh tools-nas-setup
```

启用后可在 OpenClaw 里用自然语言触发，例如：

```text
把周末派对照片移到家庭相册
```

说明：
- `move_file` 已按原名暴露
- `list_files` 对应 `list_directory`
- `create_folder` 对应 `create_directory`

### 0.1.0 本地快速通道注册表（classify / download / play）

`voice_bridge.py` 里新增了统一的本地快速通道注册表 `LOCAL_FAST_CHANNELS`，用于把高频、确定性动作先在本地执行，避免进入 agent 长链路。

当前登记顺序（也是优先级）：
- `classify`：照片分类归档（脚本执行）
- `download`：媒体下载（直连 `media_downloader`）
- `play`：Jellyfin 播放

处理流程：

```text
语音/文本 -> _run_local_fast_channels() -> 命中本地通道即返回
                                   \-> 未命中才进入 OpenClaw agent
```

扩展方式（最小改动）：
- 在 `LOCAL_FAST_CHANNELS` 里新增一项 `(name, handler)`
- `handler(args, user_text)` 返回 `None` 表示未命中；返回字符串表示已处理并直接回复

### 0.1.0 媒体下载工具（download_media）

目标：最小改动接入 yt-dlp 下载能力，默认落盘到 `/home/pi/nas_share/downloads`。

启用步骤：

```bash
cd /home/pi/NAS-Demo
./oc.sh deploy
./oc.sh tools-media-setup
./oc.sh tools-media-show
```

说明：`tools-media-setup` 会自动同步 MCP 脚本到 `/home/pi/nas_share/tools/`，并在需要时自动构建/启动 `media_downloader` 容器（即使系统缺少 docker compose 插件）。

能力说明：
- 工具名：`download_media`
- 下载根目录固定：`/home/pi/nas_share/downloads`
- 自然语言目标目录会映射到该根目录子目录（如“家庭影院文件夹” -> `家庭影院`）
- 默认完成通知文案：`下载已完成`（可在调用参数中关闭或自定义）

示例指令：

```text
下载《流浪地球3》预告片，放到家庭影院文件夹
```

#### 0.1.0.1 下载服务“连不上”的根因与修复（2026-08-18 实测）

现象：OpenClaw 回复“下载服务连不上，无法下载”，但 `curl http://localhost:28081/healthz` 正常。

根因：**MCP 脚本 `download_media_mcp.js` 与 openclaw 内置 MCP SDK 的 stdio 传输协议不匹配**。

- openclaw 容器内的 `@modelcontextprotocol/sdk` 版本为 **1.29.0**，
  其 stdio 传输格式是**换行分隔 JSON**（发送 `JSON.stringify(message) + '\n'`，读取按 `\n` 分割）。
- 而 `download_media_mcp.js` 旧实现是 **Content-Length 帧协议**（LSP 风格，`\r\n\r\n` 分隔）。
- 结果：openclaw 发来的消息脚本永远解析不到，30 秒后报
  `[bundle-mcp] failed to start server "download_media" ... MCP server connection timed out after 30000ms`，
  agent 因此看不到 `download_media` 工具，只能退回 curl 直接下载（还能下载，但绕过了媒体目录映射）。

修复（已写入 `local_voice_chat/download_media_mcp.js`，需重新同步到 `/nas_share/tools/` 并重启 openclaw）：

1. `send()` 输出改为换行分隔 JSON（`JSON.stringify(msg) + '\n'`）。
2. stdin 解析**同时兼容**两种协议：优先按 `\n` 切行解析 JSON；若没有换行则回退到 Content-Length 帧（兼容旧客户端）。
3. 顺手修复同类目录映射 bug：`shortMap` 中 `电影/剧集/电视剧` 必须排在 `家庭影院` 前面，
   否则 `家庭影院/电影` 会被 `家庭影院` 分支先命中而被扁平化成 `家庭影院`（与 `api_server.py` 的 `_safe_subdir` 同款问题）。

同步与验证：

```bash
cp /home/pi/NAS-Demo/local_voice_chat/download_media_mcp.js /home/pi/nas_share/tools/download_media_mcp.js
sudo docker restart openclaw
# 验证 MCP 脚本能响应换行分隔 JSON：
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}\n' \
  | sudo docker exec -i openclaw sh -c 'timeout 10 node /nas_share/tools/download_media_mcp.js'
```

验证通过的结果：agent 用 `download_media` 工具下载测试视频，正确落盘到
`/home/pi/nas_share/downloads/家庭影院/电影/`，Jellyfin 电影库实时扫描可见。

### 0.1.1 NAS 挂载成功但语音无法移动文件的原因

`/home/pi/nas_share -> /nas_share` 只表示**路径映射成功**，不代表容器进程自动获得写权限。

当前环境中：
- 宿主机 `pi` 是 `uid=1001`
- OpenClaw 容器内进程是 `node (uid=1000)`
- `nas_share` 子目录权限多为 `775`、文件多为 `664`，属主/属组是 `1001:1001`

这会导致：容器能看到文件，但在子目录里 `touch/move` 可能被 `Permission denied` 拒绝。

推荐最小改动修复（已验证可用）：给 `uid=1000` 添加 ACL 写权限，并设置默认继承。

```bash
sudo setfacl -R -m u:1000:rwX /home/pi/nas_share
sudo setfacl -R -d -m u:1000:rwX /home/pi/nas_share
```

验证：

```bash
sudo -n docker exec openclaw sh -lc 'touch /nas_share/家庭相册/.perm_test && rm -f /nas_share/家庭相册/.perm_test'
```

如果你希望用 Compose 固化（可选），也可以给容器补充组：

```yaml
services:
  openclaw:
    group_add:
      - "1001"
```

然后重建容器。

### 0.1.2 Immich 智能相册集成（人脸/场景识别 + 自然语言搜索）

目标：通过语音（或文本）对 Immich 照片库做语义搜索，例如「找出所有有海的照片」。

已实现并验证（2026-08-11）：

1. OpenClaw 已注册 `immich` MCP 工具（`immich-mcp`），支持自然语言搜索、人脸/场景相关接口。
2. 修复了两个关键配置坑：
   - `immich-mcp` 要求环境变量 **`IMMICH_BASE_URL`**（格式 `http://<host>:<port>/api`），不是 `IMMICH_URL`。
   - OpenClaw 容器（bridge 网络）默认**无法访问** CasaOS 安装的 Immich（独立网络）。已通过把 openclaw 加入 Immich 网络解决，并用容器名 `http://immich-server:2283/api` 访问（已在 `docker-compose.yml` 固化）。
3. 验证命令（自然语言搜索）：

```bash
sudo -n docker exec openclaw node dist/index.js agent --session-key agent:main:immich-test --message "用 immich 工具搜索照片库，找出所有包含'海'的照片，返回数量" --json
```

4. 若需要重新配置 Immich MCP：

```bash
cd /home/pi/NAS-Demo
./oc.sh tools-immich-setup   # 注意：需要先 docker network connect big-bear-immich_big_bear_immich_network openclaw
```

注意：Immich 的智能搜索依赖 CLIP 向量索引（Smart Search）。未完成索引的照片（新导入、未后台任务处理）不会被语义搜索命中，需在 Immich 管理界面确认后台任务完成。

### 0.1.3 语音照片分类卡死（最小修复）

现象：语音下达“按内容分类照片”后长时间无回复，OpenClaw 日志可能出现：

```text
Exec failed: fetch http://172.18.x.x:3003/predict
```

原因：分类脚本依赖 `immich-machine-learning:3003/predict`，当 ML 容器退出或不可达时，agent 会等待很久，表现为“卡死”。

最小修复（已验证）：

```bash
sudo -n docker start immich-machine-learning
mkdir -p /home/pi/nas_share/tools
cp /home/pi/NAS-Demo/local_voice_chat/nas_classify.py /home/pi/nas_share/tools/nas_classify.py
sudo -n docker exec openclaw sh -lc 'getent hosts immich-machine-learning && curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 http://immich-machine-learning:3003'
```

> **维护约定：`nas_classify.py` 的唯一源码是 `local_voice_chat/nas_classify.py`**（仓库根目录的同名文件已废弃删除）。
> 修改只改这一份并提交 git；`voice_bridge.py` 启动时会自动同步到 `/nas_share/tools/nas_classify.py`，
> 若未重启服务可手动执行上面的 `cp` 命令同步。

补充：
- `local_voice_chat/nas_classify.py` 已加入 3 秒连通性检查，不可达时会快速报错并提示 `docker start immich-machine-learning`。
- `voice_bridge.py` 的分类命令已改为 `timeout 120 python3 /nas_share/tools/nas_classify.py ...`，并支持脚本内自动归档，避免长时间阻塞与多步误操作。

### 0.1.4 识别后自动重命名并按规则归档（最小改动）

目标：把“识别内容 -> 重命名 -> 归档”收敛为一次脚本调用，减少 agent 多工具链路的漂移。

为什么这是当前最优策略（在现有架构下）：

1. 最小改动：不新增服务，不改 Compose，不改容器挂载；仅扩展现有 `nas_classify.py` 与 `voice_bridge.py` 提示词。
2. 风险最低：默认行为保持不变（只分类）；只有显式加 `--archive` 才执行移动。
3. 归档稳定：优先保留原一级相册根（`家庭相册/手机相册/旅行/备份`），在根下按类别落盘，避免跨库混放。
4. 命名可追溯：统一重命名为 `YYYYMMDD_HHMMSS_类别_原文件名.ext`；同名冲突自动加 `_2/_3...`。
5. 低置信兜底：分数低于阈值时归入 `待确认`，防止误分硬落盘。
6. 重复归档幂等：文件名若已带归档前缀（`YYYYMMDD_HHMMSS_类别_`）会被自动剥离后重拼，避免前缀无限叠加（如反复归档把 `test_1.jpg` 变成多层 `..._风景_..._风景_test_1.jpg`）。

脚本新增参数（向后兼容）：

- `--archive`：执行自动重命名+归档
- `--dry-run`：只打印计划，不执行移动
- `--min-score`：低置信阈值（默认 `0.20`）
- `--unknown-dir`：低置信目录名（默认 `待确认`）

推荐工作流（先预览再执行）：

```bash
# 1) 先看计划（不改文件）
timeout 120 python3 /nas_share/tools/nas_classify.py --dir /nas_share/家庭相册 --recursive --archive --dry-run

# 2) 确认后执行
timeout 120 python3 /nas_share/tools/nas_classify.py --dir /nas_share/家庭相册 --recursive --archive
```

输出示例：

```text
/nas_share/家庭相册/IMG_1001.jpg  建议: 风景 (score=0.284)  风景=0.284 植物=0.201 建筑=0.173  MOVED: /nas_share/家庭相册/风景/20260814_101530_风景_IMG_1001.jpg
```

语音自动化行为（已接入）：

- 当你说“按内容归档/整理照片”时，`voice_bridge.py` 会优先引导 OpenClaw 调用脚本的 `--archive` 模式。
- 未明确“立即执行”时，优先 `--dry-run` 预览；失败时才回退到 `nas_files` 逐个移动。

> **时间戳语义（2026-08-20 起）**：归档文件名前缀由“文件 mtime”改为“**当前处理时间**”。
> 因此每次执行分类都会刷新为本次处理时间戳，文件名每次都会变化，能直观看出“这次执行了分类操作”；
> 原有“已归档前缀自动剥离”仍生效，重复归档不会叠加前缀。
> 命名示例：`20260820_153322_风景_test_2.jpg`（前缀=本次处理时间）。

### 0.1.5 语音照片分类超时（OpenClaw agent timeout after 180s）根因与修复

现象：语音说“帮我手机相册下的照片分类”后约 3 分钟报 `OpenClaw agent timeout after 180s`。

根因（2026-08-14 实测日志）：**不是分类脚本慢，而是 agent 多轮 LLM 推理 + 远端模型 API 延迟叠加超时**。

- 分类脚本本身仅需约 2 秒（5 张图 1.96s）。
- OpenClaw agent 为“规划→执行脚本→汇报结果”跑了约 6 轮模型调用，其中单次模型请求最长 112 秒（走 `qlitellm.phicotek.com` 转发，`elapsedMs=112581`）。
- `voice_bridge.py` 的 `--openclaw-timeout` 原默认 180 秒，agent 实际耗时约 215 秒，被硬超时掐断。

最小修复（已验证）：
1. `voice_bridge.py` 新增“快速照片分类捷径”`try_classify_locally()`：当指令明确含“分类/归档/整理 + 照片/图片/相册 + 指定相册目录（手机相册/家庭相册/旅行/备份）”时，直接在容器内跑 `nas_classify.py` 并本地汇总结果，**完全不经 OpenClaw agent**。
2. 主循环改为“先试本地捷径，未命中才调 agent”，避免无关指令被误拦截。
3. `--openclaw-timeout` 默认值从 180 提到 300 秒，作为 agent 兜底。

验证：
- 原超时指令“帮我手机相册下的照片分类”现在秒级返回：`手机相册分类完成：风景3张，美食1张，植物1张。`
- “预览”类指令走 `--dry-run`，不实际移动文件。
- “今天天气怎么样”“把照片移到家庭相册”等不命中关键词，仍交给 agent，不受影响。

### 0.1.6 知识库搜索（kb_search MCP）

目标：通过语音/文本对 NAS 上的文档、照片做检索（文档走 SQLite FTS5，照片走 Immich CLIP）。

架构（最小改动）：
- 后端：`knowledge_base` 容器（[knowledge_base/api_server.py](knowledge_base/api_server.py)），容器内监听 `8084`，宿主机映射 `28084`。
- 前端：`kb_mcp.js`（同步到 `/home/pi/nas_share/tools/kb_mcp.js`）作为 OpenClaw 的 stdio MCP 桥，暴露 `kb_search` 工具。
- 数据：DB 文件 `/home/pi/nas_share/knowledge_base_data/kb.db`（容器挂载 `/data`）。

启用：

```bash
cd /home/pi/NAS-Demo
./oc.sh tools-kb-setup
```

#### 0.1.6.1 知识库“连不上”的根因与修复（2026-08-25 实测）

现象：语音问“合同在哪”等知识库问题，agent 报搜索失败或超时。

根因：宿主机 iptables 防火墙（filter 表）**INPUT 链默认策略为 DROP**，只放行端口白名单
（tcp 22 / 3389 / 5900 / 18789 / 80 / 24192 / 24190）。容器访问宿主进程端口时目标为宿主自身 IP，
数据包进入 INPUT 链被静默丢弃；`host.docker.internal:28084`（即 `172.17.0.1:28084`）不在白名单，故超时。
openclaw 的 `kb_search` MCP 原本用该地址连 KB，自然失败。

排查证据（iptables 实测，非“路由损坏”）：
- 容器 → 宿主 `172.17.0.1:22` / `:24190` **通**（在白名单）；→ `:28082 / :8096 / :28081` 全部超时（不在白名单）。
- 局域网访问映射端口正常：走 `PREROUTING DNAT → FORWARD`（Docker ACCEPT），**不经 INPUT**；
  只有“容器 → 宿主进程端口”进 INPUT，被默认 DROP 拦下。
- nat 表 MASQUERADE/DNAT 完整正常，FORWARD 默认 DROP 是 Docker 标准行为，均非故障。

修复（与 `download_media` / `immich` 同套路：共享网络 + 容器名）：
1. `knowledge_base` 以容器方式运行，并接入 `big-bear-immich` 共享网络（`oc.sh tools-kb-setup` 已固化该步骤）。
2. [kb_mcp.js](knowledge_base/kb_mcp.js) 与 MCP 环境的 `KB_API_URL` 改为 `http://knowledge_base:8084`（默认值与注释同步更新）。
3. 停用并禁用旧的宿主机 `knowledge-base.service`（原先独占 28084；数据目录不变，无感切换）。

验证：

```bash
sudo docker exec openclaw node -e "fetch('http://knowledge_base:8084/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:'测试'})}).then(r=>r.json()).then(d=>console.log('results:',d.results.length)).catch(e=>console.error(e))"
```

> **通用教训（2026-08-25，iptables 实测修正）**：本机宿主机 **iptables INPUT 链默认 DROP + 端口白名单**，
> 任何容器访问宿主进程端口（28080 系列、8096 等未放行端口）都会被静默丢弃，表现与“网络不通”完全一样。
> 排查时务必先做端口矩阵（对比 22/24190 是否通）并查看 iptables，避免误判为路由损坏。
> 容器需要访问宿主或其他容器服务时，优先用「共享网络 + 容器名」或 `network_mode: host`；
> 确需容器直连宿主进程端口时，需在 INPUT 链放行对应端口/网段。

### 0.1.7 图片风格滤镜幂等化（2026-08-25）

目标：同一风格重复转换时跳过已生成的产物，避免对同一批照片重复处理、重复生成。

实现文件：[local_voice_chat/image_batch.py](local_voice_chat/image_batch.py)（唯一维护源；
`voice_bridge.py` 启动时会自动同步到 `/home/pi/nas_share/tools/image_batch.py`）。

改动要点：
- 输出仍为「原目录/`复古风格` 或 `日系风格` 或 `胶片风格`/」子目录，不覆盖原图。
- 处理前先判断目标风格文件是否已存在且不早于原图（按 `mtime` 比较）：
  - 已存在且未过期 → 跳过（计数 `skipped`，打印 `[SKIP]`）；
  - 原图新增或更新 → 才重新生成（打印 `[OK]`）。
  这样重复执行是幂等的，只有真正变化的原图才会被重新处理。
- `voice_bridge.py` 的播报同步支持「跳过 N 张」：重跑时不再返回误导性的「成功 0 张」，
  而是「已是最新，跳过 N 张，无需重复处理」或「成功 X 张，跳过 N 张」。

使用：

```bash
python3 local_voice_chat/image_batch.py --dir /home/pi/nas_share/家庭相册 --style vintage --recursive
python3 local_voice_chat/image_batch.py --dir /home/pi/nas_share/家庭相册 --style 复古 --recursive --dry-run
```

## 0.2 CasaOS 跳转层（点击图标直接打开 OpenClaw）

OpenClaw 网关只支持 HTTPS，且禁止被 iframe 内嵌（`X-Frame-Options: DENY`），
所以 CasaOS 内嵌 WebUI 无法直接显示 OpenClaw 页面。

解决方案：用一个轻量跳转页作为中间层。CasaOS 加载跳转页后，JS 会把整个浏览器 tab
导航到 OpenClaw 的 HTTPS 地址，绕过 iframe 限制。

### 跳转页文件

跳转页位于 [redirect/index.html](redirect/index.html)，访问时自动执行：

```javascript
window.top.location.href = 'https://<host>:24190/#token=casaos';
```

> ⚠️ 若跳转页服务返回 **404（File not found）**，通常是 systemd 服务指向的目录
> 不存在。本仓库实际路径是 `/home/pi/NAS-Demo/redirect`，而服务文件指向的是
> 旧路径 `/home/pi/openclaw-casaos/redirect`。最小修复（无需改 systemd）：
>
> ```bash
> mkdir -p /home/pi/openclaw-casaos
> ln -sfn /home/pi/NAS-Demo/redirect /home/pi/openclaw-casaos/redirect
> curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:24192/   # 应为 200
> ```
>
> 另外，OpenClaw 镜像自带健康检查用 `http://127.0.0.1:18789/healthz` 探测，
> 但网关是 HTTPS-only，会导致容器状态显示 `unhealthy`（不影响功能，网关
> HTTPS 探活正常返回 200）。如需修复健康检查，需在创建容器时覆盖 healthcheck
> 为 HTTPS 探测（`docker update` 不支持改 healthcheck，重建容器会中断服务并
> 需要重新连接 immich 网络，风险较高，一般无需处理）。

### 跳转服务（systemd）

使用系统自带 Python 3 起服务，**不新增 Docker 容器**（避免在 CasaOS 出现重复 app 图标）。

服务文件 `/etc/systemd/system/openclaw-redirect.service`：

```ini
[Unit]
Description=OpenClaw Redirect Page
After=network.target

[Service]
Type=simple
User=pi
ExecStart=/usr/bin/python3 -m http.server 24192 --directory /home/pi/openclaw-casaos/redirect
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

若需重建服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-redirect
# 验证
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:24192/
```

### 配置 CasaOS WebUI

在 CasaOS 中打开 openclaw 应用设置，将 Web UI 字段改为：

```
http://  10.55.84.133  :24192  （路径留空）
```

保存后，点击 openclaw 图标即可直接跳转到 OpenClaw 界面。

### 必须设置 allowedOrigins（v2026.2.26+ 要求）

容器绑定到局域网地址后，网关会校验 Control UI WebSocket 连接的来源（Origin）。
若不配置，重启后 UI 会加载但 WebSocket 无法连接，页面无响应。

家庭局域网部署推荐直接使用通配符，换任何设备、任何 IP 都无需再改配置：

```bash
sudo docker exec openclaw node dist/index.js config set \
  gateway.controlUi.allowedOrigins '["*"]'
sudo docker restart openclaw
```

> 说明：`["*"]` 表示允许任意浏览器来源，官方 schema 注明适合受控的本地/局域网测试环境。
> 若部署到公网，请改为显式来源列表（例如 `["https://你的域名:24190"]`）。

验证：重启后日志中不再出现 `seeded gateway.controlUi.allowedOrigins` 警告。

跳转页本身通过 `location.hostname` 动态获取当前设备 IP，因此换设备时
CasaOS WebUI 无需修改，跳转与 WebSocket 均自动适配新地址。

### 访问流程

```
CasaOS 图标点击
  → 加载 http://10.55.84.133:24192/（跳转页，无 iframe 限制）
  → JS 导航整个 tab 到 https://10.55.84.133:24190/#token=casaos
  → OpenClaw 界面
```

### 0.2.1 隐藏指定 Legacy 卡片不生效（简要排障）

现象：刷新 CasaOS 后，`media_downloader`、`immich-server`、`immich-machine-learning` 仍显示在 Legacy 区。

根因（关键点）：
- 首页数据来自 `/v2/app_management/web/appgrid`。
- Legacy 条目的 `name` 是容器哈希，不是容器名；容器名在 `title.en_us`（或 `title.en_US`）。
- 因此按 `item.name` 过滤会失效，需按 `title` 过滤。

最小改动修复：
- 在 Home 分包 `AppSection.getList()` 的 `oldAppList` 赋值处改为按 `title.en_us || title.en_US` 做黑名单过滤。
- 重启网关：`sudo systemctl restart casaos-gateway`
- 浏览器强刷：`Ctrl+F5`

说明：该方案只隐藏卡片显示，不会停止或删除容器。

## 0.3 Jellyfin 家庭影院（播放下载的视频）

目标：用 Jellyfin 播放 `media_downloader` 下载到 `/home/pi/nas_share/downloads` 的视频，
提供海报墙、多端播放、断点续播，并集成进 CasaOS。

实现文件（最小改动）：

- [jellyfin-compose.yml](jellyfin-compose.yml) — 官方 `jellyfin/jellyfin:latest`（arm64 可用）
- `oc.sh` 新增 `jellyfin-deploy` / `jellyfin-show` 两条命令

部署方式（本机**没有** `docker compose` 插件，走 CasaOS CLI，与 Immich 相同套路）：

```bash
cd /home/pi/NAS-Demo
./oc.sh jellyfin-deploy
# 输出 "compose app is being installed asynchronously" 后容器异步创建
./oc.sh jellyfin-show
```

路径映射：

| 宿主机 | 容器内 | 说明 |
|---|---|---|
| `/DATA/AppData/jellyfin/config` | `/config` | Jellyfin 配置 |
| `/DATA/AppData/jellyfin/cache` | `/cache` | 缓存 |
| `/home/pi/nas_share/downloads` | `/media` | 只读挂载，媒体库根目录 |

首次访问：

- 地址：`http://<你的CasaOS主机IP>:8096`
- 首次启动有约 10~20 秒数据库初始化，期间网页打不开属正常，稍等刷新即可
- 若宿主机有防火墙，需放行端口：`sudo iptables -A INPUT -p tcp --dport 8096 -j ACCEPT`

首次向导完成后必做：

1. 建媒体库：类型选「电影」→ 路径 `/media/家庭影院/电影`；再建类型「电视节目」→ `/media/家庭影院/剧集`
2. 关闭转码：管理后台 → 控制台 → 播放 → 转码 → 取消勾选「允许转码」（树莓派 CPU 弱，必须关）

常见坑（已踩过并修复）：

1. `./oc.sh: line 187: docker-compose: command not found`
   → 本机没有旧版 `docker-compose`，也不要尝试 `docker compose`（没有该插件）。
   部署统一走 `casaos-cli app-management install -f`。
2. `Error: 404 Not Found - compose app 'jellyfin' not found`
   → 首次部署要用 `install` 而不是 `apply`（`apply` 只对已安装的 app 生效）。

## 0.4 NAS 文件浏览（FileBrowser）

目标：在 CasaOS 加一个「NAS 文件」磁贴，点击直接进入 `/home/pi/nas_share` 的文件浏览器，
方便查看、上传、重命名和删除（照片分类、媒体下载等）。

实现文件（最小改动）：

- [filebrowser-compose.yml](filebrowser-compose.yml) — 官方 `filebrowser/filebrowser:latest`（arm64 可用）
- `oc.sh` 新增 `nas-files-deploy` / `nas-files-show` 两条命令

部署：

```bash
cd /home/pi/NAS-Demo
./oc.sh nas-files-deploy
```

路径映射：

| 宿主机 | 容器内 | 说明 |
|---|---|---|
| `/home/pi/nas_share` | `/srv` | 可读写挂载，文件浏览器根目录 |
| `/DATA/AppData/filebrowser/config` | `/config` | FileBrowser 配置文件（settings.json） |
| `/DATA/AppData/filebrowser/database` | `/database` | FileBrowser 数据库（filebrowser.db） |

说明：

- 容器以 `uid:gid=1001:1001`（宿主机 `pi`）运行，与 `nas_share` 文件属主一致，保证可读写，新建文件仍归 `pi` 所有。
- 部署脚本会在数据库初始化后：停止容器 → `config set --auth.method=noauth` 关闭登录（免密）→ 再启动；
  免登录后按 `admin` 身份自动登录，具备增删改查权限。
- 点击磁贴直接打开 `http://<CasaOS主机IP>:28085`；图标为内置 SVG data URI，无需外链图片。

## 1. 在 CasaOS 中导入 Compose

1. 打开 CasaOS。
2. 进入应用页面，选择“自定义安装”或“导入 Compose”。
3. 把 [docker-compose.yml](docker-compose.yml) 的内容粘贴进去。
4. 确认端口映射：
   - 主机 `24190` -> 容器 `18789`
   - 主机 `18790` -> 容器 `18790`
5. 确认数据卷：
   - `/DATA/AppData/openclaw` -> `/home/node/.openclaw`
  - `/home/pi/nas_share` -> `/nas_share`
6. 点击安装并等待容器状态变为 Running。

## 2. 首次访问

- 访问地址：
  - `https://你的CasaOS主机IP:24190/`
- 如果提示认证，使用 token：`casaos`
  - 推荐 URL 形式：`https://你的CasaOS主机IP:24190/#token=casaos`
  - 首次访问若提示证书风险，这是自动生成的自签名证书，手动放行一次即可

## 3. 必做初始化（容器里执行）

容器启动后，进入终端（CasaOS 容器终端或 SSH）执行：

```bash
sudo docker exec -it openclaw bash
node /app/dist/index.js config
```

重要：如果第一条命令失败，不要继续执行第二条。否则第二条会在宿主机执行，
就会出现 `Cannot find module '/app/dist/index.js'`。

更安全的写法（推荐一次性复制）：

```bash
sudo docker exec -it openclaw node dist/index.js config
```

在向导里至少配置：
- 模型提供商（Base URL / API Key / Model ID）
- 网关运行位置选择 Local

## 4. 若页面提示 pairing required

在容器中执行：

```bash
node /app/dist/index.js devices list
node /app/dist/index.js devices approve <request_id>
```

然后刷新网页。

## 5. 快速排错

1. 看容器状态：

```bash
sudo docker ps -a | grep -i openclaw
```

2. 看日志：

```bash
sudo docker logs --tail 200 openclaw
```

## 6. 语音交互桥接（KWS -> ASR -> OpenClaw -> TTS）

目标：对着麦克风说「小远同学」唤醒，随后直接口述指令；指令会发给 OpenClaw 执行，结果再由 TTS 播报。

实现文件（最小改动，均在现有语音目录下）：

- `/home/pi/NAS-Demo/local_voice_chat/voice_bridge.py`
- `/home/pi/NAS-Demo/local_voice_chat/voice-bridge.service`
- `/home/pi/NAS-Demo/local_voice_chat/install_voice_bridge_service.sh`

说明：OpenClaw gateway 在容器内是 `18789`，宿主映射到 `24190`。语音桥接不直接调用 HTTP `127.0.0.1:18789`，而是调用官方 CLI：

```bash
docker exec openclaw node dist/index.js agent --session-key agent:main:voice-bridge --message "..." --json
```

这种方式最稳定，且可直接复用 OpenClaw 已配置的模型、工具和会话能力。

### 6.1 前台验证

```bash
cd /home/pi/NAS-Demo/local_voice_chat
python3 -m pip install --user sherpa-onnx numpy
python3 voice_bridge.py
```

默认唤醒词模型是 `keyword_xiaoyuantongxue.bin`（小远同学）。

常用参数：

```bash
# 首次建议：增大唤醒窗口
python3 voice_bridge.py --wake-duration 5

# 若麦克风后端有问题，切 ALSA
python3 voice_bridge.py --record-backend alsa

# 识别到静音才收尾（避免半句话被截断）
python3 voice_bridge.py --speech-min-duration 3 --speech-duration 18 --speech-silence-threshold-dbfs -45

# 调试 OpenClaw 调用（不真实请求）
python3 voice_bridge.py --openclaw-dry-run
```

### 6.2 开机自启（systemd）

```bash
cd /home/pi/NAS-Demo/local_voice_chat
chmod +x install_voice_bridge_service.sh
./install_voice_bridge_service.sh
```

若 `voice_bridge.py` 日志中出现 Docker 权限错误（如 `sudo: a password is required`），请先执行：

```bash
sudo usermod -aG docker pi
# 注：当前会话内 newgrp docker 可能报 setgid failed，无需理会；推荐下面免密 sudoers 方案，立即生效、无需重登
echo 'pi ALL=(ALL) NOPASSWD: /usr/bin/docker' | sudo tee /etc/sudoers.d/pi-docker
sudo chmod 440 /etc/sudoers.d/pi-docker
sudo -n docker ps
```

若使用 systemd 服务，还需重启服务：

```bash
sudo systemctl restart voice-bridge
sudo systemctl status --no-pager voice-bridge
```

脚本内部先尝试 `docker exec ...`，失败后自动回退 `sudo -n docker exec ...`；
配置好上面的免密规则后，回退路径即可直接工作，无需重新登录。

查看日志：

```bash
journalctl -u voice-bridge -f
```

### 6.3 运行行为

- 唤醒一次后进入连续对话模式，不需要每句都重复唤醒词。
- 连续多轮空识别后自动休眠，再次需要唤醒词。
- 语音控制词：
  - 说「休眠 / 停止监听 / 待机」：进入待机，重新等待唤醒词
  - 说「退出程序 / 关闭对话助手」：退出桥接进程（兼容「关闭语音助手」）

### 6.4 排障记录：systemd 下语音识别失效/唤醒词失灵（2026-08-20）

现象：网页点击语音或说唤醒词都没反应，识别结果多为单字（“啊/好/不知”）。

根因（已实验实锤）：
- 本机 `/etc/environment` 配置了 `LD_PRELOAD`（预加载 pulse 库）与 `PULSE_SERVER`，终端录音正常。
- **systemd 服务默认不读取 `/etc/environment`**，服务的 ffmpeg 子进程缺少 `LD_PRELOAD` 的 pulse 库，
  录音只录到 0.01~0.03s 碎片（`pa_stream_get_latency() failed` / `Generic error in an external library`），
  ASR 只能识别出单字。

修复（已在 `voice-bridge.service` 固化，仓库文件与 `/etc/systemd/system/` 均一致）：
```ini
Environment=LD_PRELOAD=/usr/lib/libpulse.so:/usr/lib/libpulse-mainloop-glib.so.0:/usr/lib/libpulse-mainloop-glib.so:/usr/lib/libpulse-simple.so:/usr/lib/pulseaudio/libpulsedsp.so:/usr/lib/pulseaudio/libpulsecommon-15.0.so:/usr/lib/pulseaudio/libpulsecore-15.0.so
Environment=PULSE_SERVER=unix:/run/pulse/native
```
改后需 `sudo systemctl daemon-reload && sudo systemctl restart voice-bridge`。

排查要点（后续遇到同类问题先看这三点）：
1. 看 `[HTTP][AUDIO]` 日志里的 `dur=` 是否只有 0.01~0.03s（正常应数秒）。
2. 对比服务进程环境：`tr '\0' '\n' < /proc/<PID>/environ | grep -i pulse`，确认 `LD_PRELOAD`/`PULSE_SERVER` 是否存在。
3. `voice_bridge.py` 的 `_ensure_audio_runtime_env()` 会补 `PULSE_SERVER`/`XDG_RUNTIME_DIR`，但**不补 `LD_PRELOAD`**，依赖 systemd unit 里的 `Environment=`。

其他已解决项：
- HTTP 模式（28082，systemd 非交互启动默认进入）与本地终端唤醒词模式并存：HTTP 模式下已内置后台唤醒线程（`--http-wakeword`，默认开启）。
- HTTP 唤醒线程与 `/trigger` 语音触发共享 `audio_lock` 串行化录音，避免并发抢麦克风导致 ffmpeg 卡死。

3. 看端口：

```bash
sudo ss -lntp | grep -E "24190|18790|18789"
```

4. 本机探活：

```bash
curl -k -I --max-time 5 https://127.0.0.1:24190/healthz
```

### 6.5 网页端对话助手（CasaOS App，端口 28083）

目标：在 CasaOS 加一个「对话助手」磁贴，网页端通过按钮/文本触发语音桥（`voice_bridge`），并实时显示唤醒对话状态。

实现文件（最小改动）：
- [voice-assistant-compose.yml](voice-assistant-compose.yml) — CasaOS 应用定义
- `voice_remote/` — 网页前端（`static/app.js`）+ 后端代理（`app.py`）

部署：

```bash
cd /home/pi/NAS-Demo
./oc.sh voice-assistant-deploy
# 打开 http://<CasaOS主机IP>:28083
```

#### 6.5.1 唤醒对话时网页端不显示实时状态（2026-08-25 实测）

现象：按钮对话正常，但语音唤醒对话时网页端「实时状态」不更新、按钮状态不同步。

根因：唤醒对话由 `voice_bridge` 后台直接处理（不走 `voice_remote` 任务队列），网页只能靠轮询 `/api/status` 同步（`app.js` 已实现 800ms 轮询）。但 `voice_assistant` 容器（bridge 网络）访问 `host.docker.internal:28082` 超时——根因是宿主机 iptables INPUT 链默认 DROP，`28082` 不在端口白名单（见 0.1.6.1 排查证据），[app.py](voice_remote/app.py) 代理失败后兜底返回 `state: idle`，网页永远收不到真实状态（按钮触发同样受影响）。

修复（[voice-assistant-compose.yml](voice-assistant-compose.yml)）：
- `network_mode: host`：容器直连宿主机网络，`UPSTREAM_BASE_URL` 改 `http://127.0.0.1:28082`。
- 容器监听端口改为 `28083`（对外访问不变），移除失效的 `ports` / `extra_hosts` 映射。

> 注：经 iptables 实测确认根因是防火墙白名单而非路由（见 0.1.6.1）。`network_mode: host` 方案
> 已能完整规避 INPUT 白名单限制，**继续沿用，不改动宿主机防火墙**。

重建（CasaOS 异步）并验证：

```bash
casaos-cli app-management apply voice-assistant -f /home/pi/NAS-Demo/voice-assistant-compose.yml
curl -s http://127.0.0.1:28083/api/healthz   # 返回 proxy_ok: true 即桥接正常
```

### 6.6 日志压缩（--verbose 开关，2026-08-25）

现象：`logs/voice_bridge.log` 体积高速增长，绝大部分是唤醒循环**每一轮约 4 秒打印一次**的音量读数
（`[HTTP][WAKE] clip level: -xx dBFS`），属于正常运行不需要的心跳日志。

修复：`voice_bridge.py` 新增 `--verbose` 开关（默认关闭），唤醒循环里的高频音量日志
（`clip level` / `low-level clip retry` / `boosted wake clip level`）仅在启用时打印；
唤醒命中、turn 结果、错误、状态变化等事件日志不受影响。

说明：
- 日志文件：`/home/pi/NAS-Demo/logs/voice_bridge.log`（由 `--log-file` 指定），自动轮转
  （单文件超 5MB 转 `.log.1`~`.log.3`，最多 4 份，约 20MB 上限）。
- 默认关闭后日志只在有事件时增长；需排查录音/音量问题时给服务加 `--verbose` 即可恢复全量日志。
- 服务端启用方式：在 `voice-bridge.service` 的 `ExecStart` 末尾追加 `--verbose`，再执行：

```bash
sudo systemctl daemon-reload && sudo systemctl restart voice-bridge
```

## 6. 清理旧的错误容器（如果之前装过商店版）

```bash
sudo docker rm -f openclaw
sudo docker rmi icewhaletech/openclaw:2026.5.7
```

再按第 1 步重新导入安装。

## 7. 常见报错对照

1. 报错：`No such container: openclaw`

含义：容器还没创建成功，或者名字不是 openclaw。

处理：先执行部署，再查容器。

```bash
sudo /home/pi/openclaw-casaos/deploy.sh
sudo docker ps -a | grep -i openclaw
```

2. 报错：`Cannot find module '/app/dist/index.js'`

含义：你在宿主机执行了容器内命令。

处理：改为直接对容器执行：

```bash
sudo docker exec -it openclaw node dist/index.js config
```

3. 报错：`unknown shorthand flag: 'f' in -f`

含义：当前系统没有 `docker compose` 子命令。

处理：新版 `deploy.sh` 已自动兼容。

```bash
sudo /home/pi/openclaw-casaos/deploy.sh
```

脚本会按顺序尝试：
- `docker compose`
- `docker-compose`
- 都没有时自动回退到 `docker run`

4. 报错：`curl: (56) Recv failure: Connection reset by peer`

含义：容器刚启动时网关还在初始化，或健康检查尚未完成。

处理：先看健康状态，再访问。

```bash
sudo docker inspect openclaw --format 'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
sudo docker logs --tail 120 openclaw
```

当 health 变成 `healthy` 后，再访问：

```bash
curl -k -I --max-time 5 https://127.0.0.1:24190/healthz
```

6. 日志报错：`Missing config. Run openclaw setup or set gateway.mode=local`

含义：容器启动命令没有带 `--allow-unconfigured`，导致首次配置前网关直接退出。

处理：使用已更新的部署文件重建容器。

```bash
sudo docker rm -f openclaw
sudo /home/pi/openclaw-casaos/deploy.sh
```

新版启动命令会先执行 `/app/start.sh`，再用 `--allow-unconfigured` 启动网关，和官方 CasaOS 模板保持一致。

补充：上面这条仅适用于 `icewhaletech/openclaw`。如果你使用的是上游镜像 `openclaw/openclaw`，不要调用 `/app/start.sh`，否则会报：

```bash
/bin/bash: line 1: /app/start.sh: No such file or directory
```

当前这份部署文件已经按 `openclaw/openclaw` 修正为：

```bash
node dist/index.js gateway --bind lan --allow-unconfigured --port 18789
```

7. 日志报错：`control ui requires device identity (use HTTPS or localhost secure context)`

含义：你正在通过局域网 `http://` 访问 Control UI。OpenClaw 默认要求浏览器处于安全上下文，
远程浏览器必须使用 `https://` 或者在网关主机本机通过 `localhost` 打开。

处理：

1. 使用这份仓库里的引导配置重置网关配置：

```bash
sudo /home/pi/openclaw-casaos/reset-config.sh
sudo docker restart openclaw
```

2. 用 HTTPS 访问：

```text
https://你的CasaOS主机IP:24190/#token=casaos
```

注意：
- 容器内部网关端口必须是 `18789`
- 主机映射端口才是 `24190`
- 在配置向导里不要把 `gateway.port` 填成 `24190`

5. 执行 `config` 时只看到 `│`

含义：交互式向导已启动（TUI 界面），不是报错。

处理：直接在该界面操作。
- 方向键选择
- 空格勾选
- 回车确认

如果终端显示异常，用下面命令重开向导：

```bash
sudo docker exec -it -e TERM=xterm-256color openclaw node dist/index.js config --section model --section gateway
```

8. 现象：仓库文件被异常清成 0 字节（含 `voice_remote/static/app.js` 等源码、`.git/objects` 里的 git 对象），
   表现：网页报「页面脚本异常：脚本未完成加载」，`git` 命令报 `bad object HEAD` / `对象文件 ... 为空`。

含义：机器经历了一次**异常掉电**（不是正常 `reboot` 关机流程）。根分区是 btrfs，掉电瞬间文件
inode 元数据已落盘、但 data extent 还没 flush，重启后表现为「文件还在、大小/时间正常、内容全是 0」。

排查：journald 的启动记录（`journalctl --list-boots`）在故障点出现日志断层，之后大量时间戳跳回
`1970-01-01`（RTC 时钟丢失），是最直接的掉电证据。

处理：
1. 数据文件用 git 恢复（`git checkout HEAD -- <文件>`）；git 对象则从干净 clone 复制 pack 后删除损坏的 0 字节松散对象。
2. 确认供电稳定（插座/线缆/电压），避免再次异常掉电。
3. 可选，做一次 btrfs 只读一致性自检：

```bash
sudo btrfs device stats /          # 看 write_io_errs / corruption_errs 是否非零
sudo btrfs scrub start /           # 启动一次数据一致性扫描
sudo btrfs scrub status /
```

## 8. 麦克风配置（默认外接 USB 麦克风）

语音桥默认已改为使用 USB 麦克风：`voice_bridge.py` 中 `--record-backend` 默认 `alsa`、`--mic-input` 默认 `plughw:0,0`，手动运行与 systemd 服务均默认生效。

查看设备：

```bash
cat /proc/asound/cards      # card 0 = USB 麦克风，card 1 = 板载 qcm6490
```

测试 USB 录音：

```bash
ffmpeg -y -f alsa -i plughw:0,0 -ac 1 -ar 16000 -t 3 /tmp/mic.wav
ffmpeg -i /tmp/mic.wav -af volumedetect -f null /dev/null 2>&1 | grep mean_volume
```

### 切回板载麦克风（无 USB 时）

把 `--mic-input` 改为板载设备（`plughw:1,1` 主麦克风 / `plughw:1,2` VA 通道）：

```bash
./run_local_voice_chat.sh --record-backend alsa --mic-input plughw:1,1
# 或改 /etc/systemd/system/voice-bridge.service 的 ExecStart 后：
sudo systemctl daemon-reload && sudo systemctl restart voice-bridge
```

> 注意：本机板载 codec 麦克风通路默认未使能，直接录制会报
> `ALSA read error: Invalid argument`，需先 `amixer -c 1 scontrols` 查看并在 mixer/UCM 中使能 DMIC/TX 通路。

