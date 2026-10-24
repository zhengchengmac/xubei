#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

APP="build/续杯.app"
BIN="Contents/MacOS/AgentPoints"
INSTALLED="/Applications/续杯.app"
AGENT="com.eric.agent-points.menubar"
# 版本号只认 git 标签，不手改这里
VERSION=$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null | tr -d 'v')
BUILD=$(git rev-list --count HEAD 2>/dev/null || echo 1)
VERSION=${VERSION:-0.1}

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

swiftc -O Sources/main.swift -o "$APP/$BIN" -framework Cocoa

# 图标由母版产出：母版内容哈希变了才重切（按时间戳跳过会静默沿用旧图）
ICON_SRC="图标资源/icon-1024.png"
ICON_STAMP="Resources/AppIcon.母版哈希"
if [ ! -f Resources/AppIcon.icns ] || [ ! -f "$ICON_STAMP" ] || \
   [ "$(shasum -a 256 "$ICON_SRC" | cut -d' ' -f1)" != "$(cat "$ICON_STAMP")" ]; then
  mkdir -p Resources
  rm -rf /tmp/AppIcon.iconset && mkdir -p /tmp/AppIcon.iconset
  for s in 16 32 128 256 512; do
    sips -z $s $s "$ICON_SRC" --out /tmp/AppIcon.iconset/icon_${s}x${s}.png >/dev/null
    d=$((s * 2))
    sips -z $d $d "$ICON_SRC" --out /tmp/AppIcon.iconset/icon_${s}x${s}@2x.png >/dev/null
  done
  iconutil -c icns /tmp/AppIcon.iconset -o Resources/AppIcon.icns
  rm -rf /tmp/AppIcon.iconset
  shasum -a 256 "$ICON_SRC" | cut -d' ' -f1 > "$ICON_STAMP"
  echo "已从 $ICON_SRC 重新生成 Resources/AppIcon.icns"
fi
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>AgentPoints</string>
  <key>CFBundleIdentifier</key><string>com.eric.agent-points</string>
  <key>CFBundleName</key><string>AgentPoints</string>
  <key>CFBundleDisplayName</key><string>续杯</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleVersion</key><string>${BUILD}</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# 数据目录指针：App 靠它找到 领积分.py 写的状态文件，不再把绝对路径写死在二进制里
pwd > "$APP/Contents/Resources/project.path"

# 用固定的签名身份，授权才不会每次编译都掉；没有这张证书时退回临时签名
IDENTITY="Apple Development: zhengcheng_mac@icloud.com (229R44BZP8)"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "zhengcheng_mac@icloud.com"; then
  codesign --force --deep --sign "$IDENTITY" "$APP" >/dev/null 2>&1 && echo "已用固定证书签名：$IDENTITY"
else
  codesign --force --sign - "$APP" >/dev/null 2>&1 && echo "（没找到固定证书，先用临时签名）"
fi

if [ -w /Applications ]; then
  ditto "$APP" "$INSTALLED" && echo "已同步到 $INSTALLED"
else
  echo "（/Applications 不可写，只有项目里这一份）"
fi

# 菜单栏常驻：登录就起来；崩了自动再起；使用者手动点「退出」就不再回来
cat > ~/Library/LaunchAgents/$AGENT.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$AGENT</string>
  <key>ProgramArguments</key>
  <array>
    <string>$INSTALLED/Contents/MacOS/AgentPoints</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
</dict>
</plist>
PLIST
# bootout 是异步的，紧接着 bootstrap 偶尔会撞上 "Bootstrap failed: 5: Input/output error"，所以退一次不够就等一下再来
launchctl bootout "gui/$(id -u)/$AGENT" 2>/dev/null || true
for delay in 0 2 4; do
  sleep "$delay"
  if launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$AGENT.plist 2>/dev/null; then
    echo "已装菜单栏常驻 $AGENT"
    break
  fi
done

# 新实例可能撞上还没退干净的旧实例：单实例守卫会直接 exit 0，KeepAlive 就不再拉
sleep 2
if ! pgrep -f "$INSTALLED/Contents/MacOS/AgentPoints" >/dev/null; then
  echo "（新实例撞上旧实例退出了，补拉一次）"
  launchctl kickstart "gui/$(id -u)/$AGENT" 2>/dev/null || true
fi

echo "已生成 $APP（版本 $VERSION，build $BUILD）"
