# AI + NAS 家庭智能系统部署教程

> arm64 Linux + CasaOS + Docker · 基于 `/home/pi/NAS-Demo` 项目

## 架构一览

```
OpenClaw (24190, AI核心) ──┐
  ├─ nas_files        文件操作（挂载 /nas_share）
  ├─ download_media   媒体下载 (28081)
  ├─ kb_search        知识库检索 (28084)
  ├─ immich           智能相册 (2283)
  └─ Jellyfin (8096)  家庭影院
voice_bridge (28082)  语音助手（宿主机 systemd）
voice_remote (28083)  网页对话助手
redirect (24192)      CasaOS 跳转层
```

## 1. 前置准备

```bash
sudo apt update && sudo apt install -y ffmpeg curl git
# docker 免密权限
sudo usermod -aG docker pi
echo 'pi ALL=(ALL) NOPASSWD: /usr/bin/docker' | sudo tee /etc/sudoers.d/pi-docker
sudo chmod 440 /etc/sudoers.d/pi-docker
# NAS 根目录（bind mount 源，属主必须正确）
sudo mkdir -p /home/pi/nas_share
sudo chown 1001:1001 /home/pi/nas_share
# 业务目录：知识库用（其余目录由脚本/容器自动创建）
mkdir -p /home/pi/nas_share/工作文档
sudo mkdir -p /DATA/AppData/openclaw
# 克隆项目
cd /home/pi && git clone <仓库地址> NAS-Demo && cd NAS-Demo && chmod +x oc.sh
```

## 2. 部署 AI 核心（OpenClaw）

```bash
./oc.sh deploy      # 自动拉 arm64 镜像、启动容器
./oc.sh status      # 容器 Up 即成功
```

首次访问 `https://<IP>:24190/#token=casaos`，容器内执行配置向导：

```bash
sudo docker exec -it openclaw node dist/index.js config
# 配置模型提供商 + 网关选 Local；配对：devices list / approve <id>
# 局域网 WebSocket（必做）：
sudo docker exec openclaw node dist/index.js config set gateway.controlUi.allowedOrigins '["*"]'
sudo docker restart openclaw
```

## 3. 部署 NAS 能力

按需逐项启用：

```bash
./oc.sh tools-nas-setup      # ① 文件操作 nas_files
./oc.sh tools-media-setup    # ② 媒体下载 download_media
./oc.sh tools-kb-setup       # ③ 知识库 kb_search
./oc.sh tools-immich-setup   # ④ 智能相册（Immich 商店装好，然后连共享网络）
./oc.sh jellyfin-deploy      # ⑤ 家庭影院
./oc.sh nas-files-deploy     # ⑥ NAS 文件浏览器 (28085)
./oc.sh voice-assistant-deploy  # ⑦ 网页对话助手 (28083)
```

**NAS 写权限（必须）**：容器内 uid=1000 无写权限，加 ACL：

```bash
sudo setfacl -R -m u:1000:rwX /home/pi/nas_share
sudo setfacl -R -d -m u:1000:rwX /home/pi/nas_share
```

**Jellyfin 必做**：首次向导建媒体库（电影 `/media/家庭影院/电影`），并关闭转码（arm64 CPU 弱）。

## 4. 部署语音助手（宿主机 systemd）

```bash
python3 -m pip install --user sherpa-onnx numpy
cd /home/pi/NAS-Demo/local_voice_chat
chmod +x install_voice_bridge_service.sh && ./install_voice_bridge_service.sh
```

前台调试：`python3 voice_bridge.py --wake-duration 5`（唤醒词：小远同学）
管理：`sudo systemctl restart voice-bridge` · 日志：`journalctl -u voice-bridge -f`

## 5. 验证

```bash
sudo docker ps | grep -E 'openclaw|media_downloader|knowledge_base|jellyfin|filebrowser|immich'
sudo docker exec openclaw node dist/index.js mcp list   # 应有 download_media/immich/kb_search/nas_files
curl -k -I https://127.0.0.1:24190/healthz
```

功能验收（对语音助手说）：分类照片 / 下载测试视频 / 播放测试视频 / 合同在哪 / 找有海的照片。

## 6. 关键坑速查

| 现象 | 原因 | 解法 |
|------|------|------|
| Exec format error | 用了 amd64 镜像 | `./oc.sh deploy` 换 arm64 镜像 |
| 容器访问 28081/28084 超时 | iptables INPUT 默认 DROP | 容器间用共享网络+容器名访问，勿走宿主端口 |
| 语音识别全单字 | systemd 缺 PulseAudio 环境 | 确保 service 里有 `LD_PRELOAD` + `PULSE_SERVER` |
| 照片分类卡死 | immich-machine-learning 退出 | `sudo docker start immich-machine-learning` |
| 改了代码不生效 | 只改了源没同步副本 | `./oc.sh tools-sync` |

> **维护约定**：源码唯一在 `NAS-Demo/`（进 git），`nas_share/tools/` 是运行副本勿手改；改完跑 `./oc.sh tools-sync`。
