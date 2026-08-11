# OpenClaw 在 CasaOS 的 arm64 部署说明

你当前机器是 arm64（aarch64），原应用商店里的 `icewhaletech/openclaw:2026.5.7` 实际是 amd64，
会出现 `Exec format error` 并不断重启。

本目录提供了可在 arm64 上使用的 Compose：`openclaw/openclaw:latest`。

## 0. 终端快捷入口（最小改动）

新增了一个轻量脚本 [oc.sh](oc.sh)，只封装常用命令，不改现有部署逻辑。

```bash
chmod +x /home/pi/openclaw-casaos/oc.sh
/home/pi/openclaw-casaos/oc.sh deploy
/home/pi/openclaw-casaos/oc.sh status
/home/pi/openclaw-casaos/oc.sh logs
/home/pi/openclaw-casaos/oc.sh health
/home/pi/openclaw-casaos/oc.sh url
/home/pi/openclaw-casaos/oc.sh model
/home/pi/openclaw-casaos/oc.sh tools-nas-setup
/home/pi/openclaw-casaos/oc.sh tools-nas-show
/home/pi/openclaw-casaos/oc.sh pair-list
/home/pi/openclaw-casaos/oc.sh pair-approve <request_id>
```

`pair-approve` 会自动去掉你从页面复制时常见的末尾中文句号 `。`。

`tools-nas-setup` 会注册一个 MCP 文件工具服务器（`nas_files`），
并把文件操作范围限制到容器内的 `/nas_share`（对应宿主机 `/home/pi/nas_share`）。

## 0.1 文件工作流自动化（最小改动）

执行以下命令即可启用文件管理工具并重启容器：

```bash
/home/pi/openclaw-casaos/oc.sh deploy
/home/pi/openclaw-casaos/oc.sh tools-nas-setup
```

启用后可在 OpenClaw 里用自然语言触发，例如：

```text
把周末派对照片移到家庭相册
```

说明：
- `move_file` 已按原名暴露
- `list_files` 对应 `list_directory`
- `create_folder` 对应 `create_directory`

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
  - 说「退出程序 / 关闭语音助手」：退出桥接进程

3. 看端口：

```bash
sudo ss -lntp | grep -E "24190|18790|18789"
```

4. 本机探活：

```bash
curl -k -I --max-time 5 https://127.0.0.1:24190/healthz
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

