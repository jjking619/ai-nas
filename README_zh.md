# 智能NAS系统

系统以CasaOS服务、OpenClaw智能体为核心，通过语音或文本下达指令，系统自动规划并调用工具，完成文件管理、相册整理、影音下载、知识问答与图片处理，带来"一句话摘定"的智能交互体验。

## 目录

- [它能做什么](#它能做什么)
- [架构一览](#架构一览)
- [一键安装](#一键安装)
- [首次使用](#首次使用)
- [日常维护](#日常维护)
- [语音助手（可选）](#语音助手可选)
- [项目结构](#项目结构)
- [常见问题](#常见问题)

---

## 它能做什么

| 能力 | 说明 | 示例指令 |
|------|------|----------|
| 📁 文件管理 | 在 NAS 内移动/整理/搜索文件 | “把xxx照片移到家庭相册” |
| 🖼️ 照片分类 | 按内容自动分类归档（可预览/执行） | “帮我把手机相册的照片分类” |
| 🎬 媒体下载 | 说一句话下载视频，自动进 Jellyfin 媒体库 | “下载测试视频，放到家庭影院文件夹” |
| 🔍 知识库问答 | 文档全文检索 + 照片语义搜索 | “住房合同在哪”  |
| 📺 家庭影院 | Jellyfin 海报墙、多端播放 | “播放xxx视频” |
| 🗣️ 语音助手（可选） | 唤醒词对话、本地离线 ASR/TTS | 对小远同学说“合同在哪” |

模型接入使用 **OpenAI 兼容接口**（DeepSeek、通义、OpenAI 等均可），只需一个 API Key。

---

## 架构一览

```
┌─ 交互入口 ─────────────────────────────────────────────┐
│  OpenClaw 控制台   https://<IP>:24190   （AI 核心）    │
│  网页对话助手      http://<IP>:28083                   │
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
${HOME}/nas_share  downloads/家庭影院     Immich (:2283)
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
| filebrowser | 28085 | NAS 文件浏览（免登录） |
| voice_assistant | 28083 | 网页对话助手（CasaOS 磁贴） |
| voice_bridge | 28082 | 语音桥（宿主机 systemd，可选） |

所有用户数据在 `${HOME}/nas_share`；服务配置在 `/DATA/AppData/`。
安装脚本会按当前执行用户自动写入 `NAS_ROOT`、`APP_DIR`、`NAS_PUID`、`NAS_PGID`，无需手工填写用户名。

---

## 一键安装

> 整个安装只需要你提供 **OpenClaw 的模型 API**（Base URL + API Key），其余全自动。

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

然后脚本会自动完成：

1. 生成/补齐 `.env` 配置
2. 启动容器全家桶（openclaw / media_downloader / knowledge_base / Immich / Jellyfin / filebrowser / 网页对话助手）
3. 配置 OpenClaw 模型 Provider
4. 注册 4 个 MCP 工具：`nas_files`、`download_media`、`kb_search`、`immich`
5. 输出各服务访问地址

非交互方式（远程/脚本）：

```bash
# 1) 前面没填 API，后面补配置
export OPENCLAW_API_KEY='sk-xxxx'
bash install.sh --api-key="$OPENCLAW_API_KEY"

# 2) 指定提供商/模型
bash install.sh \
  --model-base-url=<你的BaseURL> \
  --api-key="$OPENCLAW_API_KEY" \
  --model-id=<你的ModelID>
```

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
```

> 脚本幂等：重复执行安全，已存在的容器不会重复创建。
> 若没有 docker compose 插件，会自动降级用 `oc.sh` 逐个启动服务。

---

## 首次使用

### 1. 打开 AI 控制台并配对

```bash
# 终端查看本机 IP 和完整带 token 的地址
bash ./oc.sh url
```

浏览器打开 `https://<IP>:24190/#token=<token>`，首次会提示自签证书风险 → 点“继续访问”。在页面中 **approve 设备配对**（若不方便，可终端执行 `./oc.sh pair-approve <request_id>`）。

### 2. 登录 Immich 相册

- 地址：`http://<IP>:2283`
- 管理员：`admin@immich.app`
- 密码：见宿主机 `/DATA/AppData/immich-admin-password.txt`（仅 root 可读；用 `sudo cat` 查看）
- 首次进入建议建相册并上传照片，稍候 CLIP 后台任务完成即可语义搜索

### 3. 试试说一句话

在 OpenClaw 控制台 / 网页对话助手 / 语音助手里输入（或说出唤醒词“小远同学”）：

```text
把周末派对照片移到家庭相册
下载测试视频并播放
帮我把手机相册的照片分类
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
./oc.sh model                 # 查看当前模型配置
./oc.sh pair-list             # 待配对设备列表
./oc.sh pair-approve <id>     # 批准设备配对
```

按需管理单项能力：

```bash
./oc.sh tools-nas-setup       # 文件操作工具（nas_files）
./oc.sh tools-media-setup     # 媒体下载工具（download_media）
./oc.sh tools-kb-setup        # 知识库工具（kb_search）
./oc.sh tools-immich-setup    # 智能相册 MCP（immich）
./oc.sh jellyfin-deploy       # 部署 Jellyfin 家庭影院
./oc.sh nas-files-deploy      # 部署 NAS 文件浏览
./oc.sh voice-assistant-deploy  # 部署网页对话助手
./oc.sh tools-sync            # 同步源码 → 容器运行副本
```

> **唯一源码约定**：运行脚本的唯一源码在本仓库 `NAS-Demo/`（进 git）；
> `${HOME}/nas_share/tools/` 只是容器可见的运行副本，**不要手动改**。
> 改完源码后执行 `./oc.sh tools-sync` 同步并自动重启受影响服务。

---

## 语音助手（可选）

需要：USB/板载麦克风 + 本地语音 SDK（唤醒词“小远同学”，离线 ASR/TTS）。

```bash
cd ~/NAS-Demo/local_voice_chat
./install_voice_bridge_service.sh      # 安装为 systemd 服务并启动
```

管理：

```bash
sudo systemctl restart voice-bridge    # 重启
journalctl -u voice-bridge -f          # 实时日志
```

前台调试：`python3 local_voice_chat/voice_bridge.py --wake-duration 5`

> 默认唤醒词：**小远同学**。语音与网页对话共用 28082/28083 通道。
> 麦克风后端、唤醒词等高级配置见 [local_voice_chat/README.md](local_voice_chat/README.md)。

---

## 项目结构

```text
NAS-Demo/
├── install.sh                 # ★ 一键安装/配置入口（唯一需要维护的脚本）
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
| 容器启动报 `Exec format error` | 用了 amd64 镜像。`./oc.sh deploy` 已强制 `--platform linux/arm64`，重建即可 |
| 容器能 ping 通宿主但访问 28081/28084 超时 | 宿主机 iptables INPUT 默认 DROP。服务间应走共享网络 + 容器名（本仓库已如此配置），勿走宿主端口 |
| openclaw 状态显示 `unhealthy` | 镜像健康检查用 HTTP 探测 HTTPS 端口所致，网关实际正常（HTTPS 探活返回 200），无需处理 |
| 语音识别全单字/无反应 | systemd 缺 PulseAudio 环境。确认 `voice-bridge.service` 含 `LD_PRELOAD` + `PULSE_SERVER` 后 `sudo systemctl restart voice-bridge` |
| 照片分类卡死 | `immich-machine-learning` 可能退出：`sudo docker start immich-machine-learning` |
| 改了代码不生效 | 只改了源码没同步副本：`./oc.sh tools-sync` |
| 忘记 Immich 管理员密码 | `sudo cat /DATA/AppData/immich-admin-password.txt` |
| Immich 照片搜不到 | 后台 CLIP 任务未完成：管理界面触发 Smart Search，或 `./oc.sh immich-sync-jobs` |

