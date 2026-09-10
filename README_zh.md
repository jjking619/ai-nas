# 智能NAS系统

系统以CasaOS服务、OpenClaw智能体为核心，通过语音或文本下达指令，系统自动规划并调用工具，完成文件管理、相册整理、影音下载、知识问答与图片处理，带来"一句话摘定"的智能交互体验。

## 目录

- [它能做什么](#它能做什么)
- [架构一览](#架构一览)
- [一键安装](#一键安装)
- [首次使用](#首次使用)
- [日常维护](#日常维护)
- [语音助手](#语音助手)
- [项目结构](#项目结构)
- [常见问题](#常见问题)

---

## 它能做什么

| 能力 | 说明 | 示例指令 |
|------|------|----------|
| 📁 文件管理 | 在 NAS 内移动/整理/搜索文件 | “把xxx照片移到家庭相册” |
| 🖼️ 照片分类 | 按内容自动分类归档 | “帮我把家庭相册下的照片分类” |
| 🎬 媒体下载 | 说一句话下载视频，自动进 Jellyfin 媒体库 | “下载测试视频，放到 Movies 文件夹” |
| 🔍 知识库问答 | 文档全文检索 + 照片语义搜索 | “住房合同在哪”  |
| 🎨 图片滤镜 | 一键套用复古 / 日系 / 胶片风格，输出到原目录的风格子目录，不覆盖原图，支持先预览 | “把旅行照片加复古滤镜” |
| 🗣️ 语音助手 | 系统核心入口：网页对话开箱即用，带麦克风可加唤醒词对话、离线 ASR/TTS | 使用小远同学唤醒对话 |

模型接入使用 **OpenAI 兼容接口**（DeepSeek、通义、OpenAI 等均可），只需一个 API Key。

---

## 架构一览

```
┌─ 交互入口 ─────────────────────────────────────────────┐
│  OpenClaw 控制台   https://<IP>:24190   （AI 核心）    │
│  Voice Assistant   http://<IP>:28083                   │
│  语音助手(宿主)    http://<IP>:28082  + 唤醒词         │
└──────────────────────┬─────────────────────────────────┘
                       │
   ┌───────────────────┼───────────────────────┐
   ▼                   ▼                       ▼
 nas_files         download_media          kb_search
(文件操作)         (yt-dlp 下载)         (文档+照片检索)
   │                   │                       │
   │             media_downloader         knowledge_base
   │               (:28081)                 (:28084)
   │                   │                       │
   ▼                   ▼                       ▼
${HOME}/nas_share  downloads/Movies,TV Shows  Immich (:2283)
(NAS 共享根目录)         │                   照片库+CLIP
                         ▼
                    Jellyfin (:8096)
                    家庭影院媒体库
```

| 服务 | 端口 | 作用 |
|------|------|------|
| openclaw | 24190 (HTTPS) | AI 核心（网关、MCP 工具、Control UI） |
| immich | 2283 | 智能相册（照片管理 + CLIP 语义搜索） |
| jellyfin | 8096 | 家庭影院（海报墙、断点续播） |
| media_downloader | 28081 | 媒体下载服务（yt-dlp） |
| knowledge_base | 28084 | 知识库检索（文档 FTS5 + 照片语义） |
| filebrowser | 28085 | NAS Files（免登录文件浏览） |
| voice_assistant | 28083 | Voice Assistant（CasaOS 磁贴，必选入口） |
| voice_bridge | 28082 | 语音桥（宿主机 systemd，唤醒词语音必需） |

所有用户数据在 `${HOME}/nas_share`；服务配置在 `/DATA/AppData/`。
安装脚本会按当前执行用户自动写入 `NAS_ROOT`、`APP_DIR`、`NAS_PUID`、`NAS_PGID`，无需手工填写用户名。

---

## 一键安装

### 执行

```bash
git clone https://github.com/jjking619/ai-nas.git NAS-Demo
cd ~/NAS-Demo
bash install.sh
```

推荐用交互式安装：

1. Base URL 直接回车（默认 `https://api.deepseek.com/v1`）
2. 粘贴 API Key（输入不回显）
3. Model ID 默认 `deepseek-chat`（可不填）

默认只需输入 API Key。

安装脚本会自动完成 CasaOS 应用入口注册（OpenClaw / Immich / Jellyfin），安装后可直接在 CasaOS 应用页点击使用。

然后脚本会自动完成：

1. 生成/补齐 `.env` 配置
2. 启动核心容器（openclaw / media_downloader / knowledge_base / Immich / Jellyfin），并部署 filebrowser、Voice Assistant（由 `oc.sh` 渲染各自的 compose 后启动）
3. 配置 OpenClaw 模型 Provider
4. 注册 MCP 工具：`nas_files`、`download_media`、`kb_search`（`immich` 需先配置 `IMMICH_API_KEY` 才会注册）
5. 安装语音桥（Voice Assistant 的后端，宿主机 systemd 服务，端口 28082）：自动检查依赖、预下载离线 ASR/TTS 模型并启动服务（可用 `--skip-voice` 跳过）
6. 输出各服务访问地址

> 直接编辑 `~/NAS-Demo/.env` 里的 `OPENCLAW_MODEL_BASE_URL` / `OPENCLAW_MODEL_API_KEY` / `OPENCLAW_MODEL_ID`，
> 然后执行 `./oc.sh model-apply`，约 10 秒即可生效（自动写入配置并重启 openclaw，不影响其它服务）。

常见提供商参数：

```text
DeepSeek:  base-url=https://api.deepseek.com/v1                          model-id=deepseek-chat
通义千问:  base-url=https://dashscope.aliyuncs.com/compatible-mode/v1    model-id=qwen-plus
OpenAI:    base-url=https://api.openai.com/v1                            model-id=gpt-4o-mini
```

说明：`install.sh` 可重复执行（幂等），会在保留现有服务的前提下更新模型配置。



常用参数：

```bash
bash install.sh --check        # 仅做环境自检，不改动系统
bash install.sh reset          # 重置 OpenClaw 配置为出厂模板并重启
bash install.sh --skip-voice   # 跳过自动安装语音桥（稍后可手动安装）
```

---

## 首次使用

### 1. 先打开 CasaOS 并配置应用

CasaOS 管理入口：

```bash
bash ./oc.sh casaos-url
```

先在 CasaOS 应用页确认/配置其它应用（如 Immich、Jellyfin、NAS Files、Voice Assistant）。

### 2. 打开 OpenClaw 控制台并配对

```bash
# 终端查看本机 IP 和完整带 token 的地址
bash ./oc.sh url
```

浏览器打开 `https://<IP>:24190/#token=<token>`，首次会提示自签证书风险 → 点“继续访问”。在页面中 **approve 设备配对**（若不方便，可终端执行  `./oc.sh pair-list` 后再 `./oc.sh pair-approve <request_id>`）。


### 3. 登录 Immich 相册

- 首次打开点击入门按页面创建管理员账号和密码（建议邮箱用 `admin@immich.app`）
- 若直接进入登录页，请使用你已创建的管理员账号密码
- 选择语言，后面使用默认配置继续下一步即可
- 首次进入建议建相册并上传照片，稍候 CLIP 后台任务完成即可语义搜索

**若想启用语义搜索**（可选，不影响照片分类），在 Immich 中创建 API Key：

1. 在 Immich：管理后台 → API Keys → 新建 API Key（赋予所有权限）
2. 把密钥写入 `~/NAS-Demo/.env` 的 `IMMICH_API_KEY` 字段
3. 注册 immich 工具：
   ```bash
   cd ~/NAS-Demo
   ./oc.sh tools-immich-setup
   ```

### 4. 首次配置 Jellyfin

- 地址：`http://<IP>:8096`（首次打开为设置向导，创建本地管理员即可，与 Immich 无关）
- 首次启动约需 10~20 秒初始化数据库，期间页面打不开属正常，稍等刷新
- 媒体目录挂载关系：宿主 `${HOME}/nas_share/downloads` → 容器 `/media`

> **媒体目录已自动建好**：`install.sh` 安装时已创建 `Movies` 与 `TV Shows` 文件夹（属主、权限一并设置），
> 正常情况下进入向导即可直接选择 `/media/Movies`、`/media/TV Shows`，无需返回终端。
> 若缺失（如跳过安装脚本单独部署），先执行 `./oc.sh tools-media-setup` 自动补建，再刷新向导即可。

首次向导步骤：

1. 选择语言，继续下一步
2. 创建管理员账号和密码
3. 添加媒体库：
   - 类型「电影」→ 路径填 `/media/Movies`
   - 类型「电视节目」→ 路径填 `/media/TV Shows`
   - 若选择器里只能看到 `/media`、点不开子目录，回到终端执行上面提到的 `./oc.sh tools-media-setup`，然后点击文件夹列表上方的「刷新」重新浏览即可选择
4. 元数据语言默认即可，完成向导

**向导完成后，创建一个 API 密钥**（语音说“播放 / 下载后自动播放”时，语音桥需调用 Jellyfin API 自动扫库、搜索与远程播放；不配置只影响自动播放，不影响下载与唤醒）：

1. 在 Jellyfin：管理后台 → 高级 → API 密钥 → 新增 API 密钥
2. 把生成的密钥写入 `~/NAS-Demo/.env` 文件中的 `JELLYFIN_API_KEY` 字段
3. 让配置生效：
   - 若语音桥**尚未安装**（还没做第 5 步）：到此即可，第 5 步安装语音桥时会自动读取，无需重启
   - 若语音桥**已在运行**：重启后生效
     ```bash
     sudo systemctl restart voice-bridge
     ./oc.sh jellyfin-key-check                    # 一键校验 .env 与运行态是否一致、接口是否 401
     ```

以后 media_downloader 下载的视频放入 `~/nas_share/downloads/Movies` 或 `~/nas_share/downloads/TV Shows`，Jellyfin 会自动扫描入库。

### 5. 启用语音入口（必做）

本系统主打「一句话搞定」，语音/网页对话是**默认交互入口**，首次使用必须启用。

> ⚠️ **Voice Assistant（28083）与语音桥（28082）是同一套服务**：28083 只是网页界面，对话实际转发给 28082 处理。
> 因此**必须先安装并运行语音桥**，否则 28083 看似能打开，但一发送消息就报错（502）。

#### a) 安装语音桥（含依赖）

> ✅ **本步已由 `install.sh` 自动完成**：安装时会检查依赖、预下载 ASR/TTS 模型、写入并启动 systemd 服务。
> 只有这几种情况才需要按下面手动执行：安装时跳过（非交互环境 / 无 sudo / 加了 `--skip-voice`）、代码更新后需重装、或服务异常。

语音桥依赖 `numpy` + `sherpa_onnx`（离线 ASR/TTS）；调用 OpenClaw 需要能免密使用 docker
（`docker ps` 能直接执行即可；脚本也支持 `sudo -n docker` 回退）：

```bash
# 1) 安装 Python 依赖（已装可跳过；也可用 run_local_voice_chat.sh 自动安装）
python3 -m pip install --user sherpa-onnx numpy
# Debian 12+ / Ubuntu 24+ 若提示 externally-managed-environment，追加 --break-system-packages

# 2) 仅当 `docker ps` 提示权限不足时才需要配置免密 docker
USER_NAME="$(id -un)"
echo "${USER_NAME} ALL=(ALL) NOPASSWD: /usr/bin/docker" | sudo tee "/etc/sudoers.d/${USER_NAME}-docker"
sudo chmod 440 "/etc/sudoers.d/${USER_NAME}-docker"

# 3) 安装为 systemd 服务并启动（首次会预下载 ASR/TTS 模型，视网络需数分钟；幂等，可重复执行）
cd ~/NAS-Demo/local_voice_chat
./install_voice_bridge_service.sh
```

#### b) 验证两个入口

```bash
systemctl is-active voice-bridge          # 输出 active 即正常
curl -sS http://127.0.0.1:28082/healthz   # 语音桥就绪
journalctl -u voice-bridge -f             # 实时日志（边说边看）
```

| 入口 | 地址/方式 | 说明 |
|------|-----------|------|
| 语音桥 | `http://<IP>:28082`（后端） | 对话处理核心，支持唤醒词「小远同学」（离线 ASR/TTS） |
| Voice Assistant | `http://<IP>:28083` | 浏览器文字/语音输入，转发给 28082，**依赖语音桥运行** |

> 若机器没有麦克风：仍需让语音桥进程保持运行（否则 28083 不可用），只是跳过唤醒环节，
> 直接用 Voice Assistant（28083）打字，即可使用文件管理/相册/下载等全部能力。

### 6. 试试说一句话

在对话助手应用中输入文本/语音进行对话（或说出唤醒词“小远同学”）：

```text
下载测试视频并播放
帮我把家庭相册的照片分类
把家庭相册下的照片复古滤镜
住房合同在哪
```
---

## 日常维护

所有维护命令统一走 [oc.sh](oc.sh)（由 [install.sh](install.sh) 一键安装时自动使用）：

```bash
./oc.sh status                # 查看 openclaw 容器状态
./oc.sh logs                  # 查看 openclaw 日志（默认最近 120 行）
./oc.sh health                # 网关健康检查
./oc.sh url                   # 打印控制台访问地址（含 token）
./oc.sh casaos-url            # 打印 CasaOS 首页地址
./oc.sh model                 # 查看当前模型配置
./oc.sh model-apply           # 改完 .env 的模型配置后立即生效
./oc.sh pair-list             # 待配对设备列表
./oc.sh pair-approve <id>     # 批准设备配对
```

按需管理单项能力：

```bash
./oc.sh tools-nas-setup       # 文件操作工具（nas_files）
./oc.sh tools-media-setup     # 媒体下载工具（download_media）
./oc.sh tools-kb-setup        # 知识库工具（kb_search，并自动同步内置测试合同到 文档/）
./oc.sh tools-immich-setup    # 智能相册 MCP（immich，配置后自动导入内置测试照片）
./oc.sh openclaw-app-deploy   # 补装 OpenClaw 网页入口（CasaOS 应用）
./oc.sh immich-apply          # 部署/修复 Immich（CasaOS 应用）
./oc.sh jellyfin-deploy       # 部署 Jellyfin 家庭影院
./oc.sh jellyfin-key-check    # 校验 Jellyfin API key 是否已生效（.env + 语音桥运行态 + 接口鉴权）
./oc.sh nas-files-deploy      # 部署 NAS Files
./oc.sh voice-assistant-deploy  # 部署 Voice Assistant
./oc.sh tools-sync            # 同步源码 → 容器运行副本
./oc.sh tools-photos-setup    # 同步样例照片 → 家庭相册/测试样例，并自动导入 Immich（幂等）
```

> `./oc.sh nas-files-deploy` 会先渲染 [filebrowser-compose.yml](filebrowser-compose.yml) 里的 `__NAS_ROOT__`、`__NAS_PUID__`、`__NAS_PGID__` 占位符；
> 不要直接执行 `docker compose -f filebrowser-compose.yml up -d`，否则会因占位符未替换导致启动失败。

> **唯一源码约定**：运行脚本的唯一源码在本仓库 `NAS-Demo/`（进 git）；
> `${HOME}/nas_share/tools/` 只是容器可见的运行副本，**不要手动改**。
> 改完源码后执行 `./oc.sh tools-sync` 同步并自动重启受影响服务。

---

## 语音助手

> **服务管理**：启用（必做）见[首次使用第 5 步](#5-启用语音入口必做)，此处为安装原理与日常管理补充。

**依赖**：`numpy` + `sherpa_onnx`（离线 ASR/TTS 模型，安装脚本自动预下载）+ 免密 docker 权限；
如需语音唤醒还需 USB/板载麦克风（唤醒词“小远同学”）。
> Voice Assistant（28083）转发到语音桥（28082），因此**即使没有麦克风也让语音桥运行**，否则网页对话不可用。

**重装 / 修复**（若服务未运行或代码有更新后需重装）：

```bash
# 缺依赖时先补装
python3 -m pip install --user sherpa-onnx numpy

cd ~/NAS-Demo/local_voice_chat
./install_voice_bridge_service.sh      # 重新安装为 systemd 服务并启动
```

**管理**：

```bash
sudo systemctl restart voice-bridge    # 重启
journalctl -u voice-bridge -f          # 实时日志
systemctl is-active voice-bridge       # 查看是否 active
```

前台调试：`python3 local_voice_chat/voice_bridge.py --wake-duration 5`

> 默认唤醒词：**小远同学**。语音与网页对话共用 28082/28083 通道。
> 麦克风后端、唤醒词等高级配置见 [README.md](README.md) 第 6 节「语音交互桥接」（含 6.7 进阶用法与常见问题）。

---

## 项目结构

```text
NAS-Demo/
├── install.sh                 # 一键安装/配置入口（唯一需要维护的脚本）
├── oc.sh                      # 维护命令封装（status/logs/tools-*/deploy...）
├── docker-compose.yml         # 核心服务全家桶编排
├── .env(.example)             # 配置（模型 API、网关 token）
├── openclaw.bootstrap.json    # OpenClaw 出厂配置模板（reset 时恢复）
├── knowledge_base/            # 知识库服务（API + MCP 桥）
├── media_downloader/          # 媒体下载服务
├── local_voice_chat/          # 语音助手（ASR/TTS/桥接 + MCP 脚本源码）
├── voice_remote/              # 网页对话助手
├── redirect/                  # CasaOS 图标跳转层
└── filebrowser-compose.yml    # NAS 文件浏览
    jellyfin-compose.yml       # Jellyfin
    immich-compose.yml         # Immich（CasaOS 商店版）
    voice-assistant-compose.yml# 网页对话助手
```

---

## 常见问题

| 现象 | 处理 |
|------|------|
| openclaw 状态显示 `unhealthy` | 镜像健康检查用 HTTP 探测 HTTPS 端口所致，网关实际正常（HTTPS 探活返回 200），无需处理 |
| 语音识别全单字/无反应 | systemd 缺 PulseAudio 环境。确认 `voice-bridge.service` 含 `LD_PRELOAD` + `PULSE_SERVER` 后 `sudo systemctl restart voice-bridge` |
| `bash ./oc.sh casaos-url` 打不开 | 先看命令是否提示“未检测到 CasaOS Web 服务（80/443）”。若有，执行 `curl -fsSL https://get.casaos.io \| sudo bash`；若 `curl` 超时/失败，先 `sudo apt install wget -y` 再重试安装 |
| CasaOS 页面里看不到 OpenClaw/Immich/Jellyfin，或点击应用打不开 | 运行 `./oc.sh openclaw-app-deploy`、`./oc.sh immich-apply`、`./oc.sh jellyfin-deploy` 重新注册应用入口；若提示端口占用，新脚本会自动迁移同名容器后重试 |
| 照片分类卡死 | `immich-machine-learning` 可能退出：`sudo docker start immich-machine-learning` |
| 改了 `.env` 的模型配置（Base URL/API Key/Model ID）不生效 | 执行 `./oc.sh model-apply`（读 .env 立即应用并重启，约 10 秒） |
| 改了代码不生效 | 只改了源码没同步副本：`./oc.sh tools-sync` |
| `docker compose -f filebrowser-compose.yml up -d` 启动 filebrowser 失败，报 `unable to find user __NAS_PUID__` | 该 compose 含 `__NAS_ROOT__` / `__NAS_PUID__` / `__NAS_PGID__` 占位符，必须走 `./oc.sh nas-files-deploy` 先渲染后再部署；若手动调试，请先把占位符替换成当前机器的实际路径与 uid/gid |
| Immich 首次打开显示管理员注册 | 正常，表示尚未初始化；按页面创建管理员账号和密码（建议邮箱 `admin@immich.app`） |
| Immich 照片搜不到 | 后台 CLIP 任务未完成：管理界面触发 Smart Search，或 `./oc.sh immich-sync-jobs` |
| Voice Assistant（28083）发消息报 502 / connection refused | 语音桥（28082）未运行。重跑 `bash install.sh` 即可自动安装，或手动执行 `cd ~/NAS-Demo/local_voice_chat && ./install_voice_bridge_service.sh` |
| 语音桥安装报“找不到装有 numpy/sherpa_onnx 的 python3” | 依赖未装：`python3 -m pip install --user sherpa-onnx numpy` 后重试（或先跑 `./run_local_voice_chat.sh` 自动安装） |
| Voice Assistant 播放被拦（Chrome/Edge 提示 Block Audio） | 浏览器自动播放策略所致，非服务故障。点击地址栏左侧锁/权限图标 → 选择 **Permissions** → 把 **Autoplay** 改成 **Audio and Video**即可放行 |

