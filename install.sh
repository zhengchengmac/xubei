#!/bin/bash
# 一键安装（开源版）：建 Python 环境 → 生成定时器 LaunchAgent → 打包 App → 自检
# 只在你自己已登录各家客户端的 macOS 账号上跑。
set -euo pipefail
cd "$(dirname "$0")"
PROJ=$(pwd)

if [ ! -d "/Applications" ]; then echo "/Applications 不存在？"; exit 1; fi

echo "[1/4] Python 虚拟环境与依赖"
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

echo "[2/4] 定时器 LaunchAgent（每天 10:01 / 15:01 / 20:01 跑一轮）"
sed "s|__PROJECT__|$PROJ|g" com.eric.agent-points.plist > "$HOME/Library/LaunchAgents/com.eric.agent-points.plist"
launchctl bootout "gui/$(id -u)/com.eric.agent-points" 2>/dev/null || true
sleep 2
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.eric.agent-points.plist"

echo "[3/4] 编译打包并装到 /Applications/续杯.app（菜单栏常驻由它接管）"
./build.sh

echo "[4/4] 冒烟自检（全程只读）"
bash tools/冒烟检查.sh

echo
echo "装好了。DeepSeek 的余额要你自己建一个 API Key：echo 'sk-xxxx' > $PROJ/.deepseek-key && chmod 600 $PROJ/.deepseek-key"
echo "菜单栏平时不显示；双击 /Applications/续杯.app 可以让它出来一小时。"
