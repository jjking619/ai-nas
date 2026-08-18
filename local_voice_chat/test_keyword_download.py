#!/usr/bin/env python3
"""测试关键词下载功能"""
import json
import subprocess
import time

TEST_CASES = [
    ("下载海洋到视频", "keyword: 海洋, folder: 视频"),
    ("下载预告片到电影", "keyword: 预告片, folder: 电影"),
    ("下载兔子", "keyword: 兔子, default folder"),
    ("下载样本到视频", "keyword: 样本, folder: 视频"),
]

def test_openclaw_command(user_input):
    """调用 OpenClaw 测试语音命令"""
    cmd = [
        "docker", "exec", "openclaw", "node", "dist/index.js", "agent",
        "--session-key", "agent:main:voice-bridge",
        "--message", user_input,
        "--json"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.stdout + result.stderr
    except Exception as e:
        return f"Error: {e}"

print("=" * 60)
print("🧪 关键词下载功能测试")
print("=" * 60)

for test_input, description in TEST_CASES:
    print(f"\n📝 测试：{test_input}")
    print(f"   预期：{description}")
    print(f"   运行中...")
    
    output = test_openclaw_command(test_input)
    
    # 检查关键输出
    if "下载已完成" in output or "ok" in output.lower():
        print(f"   ✅ 成功")
    else:
        print(f"   ⚠️  返回：{output[:200]}")
    
    time.sleep(1)

print("\n" + "=" * 60)
print("检查下载目录...")
import subprocess as sp
result = sp.run(
    ["find", "/home/pi/nas_share/downloads", "-name", "*.mp4", "-newermt", "10 minutes ago"],
    capture_output=True, text=True
)
if result.stdout:
    print("✅ 新文件：")
    for line in result.stdout.strip().split('\n'):
        print(f"  - {line}")
else:
    print("❌ 未发现新下载的文件")
