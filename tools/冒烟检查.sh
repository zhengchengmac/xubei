#!/bin/bash
# 只读冒烟基线：给下一个 AI 改完代码后自证"没改坏生产签到"用。
# 全程只读——绝不执行 领积分.py、不 kickstart/bootout/bootstrap、不 build、不领取、不发通知。
# 判据逐字照抄 docs/工作原理.md 第二、三、七节。退出码非 0 = 有东西坏了。
set -u

cd "$(dirname "$0")/.." || { echo "冒烟失败：进不去项目根目录"; exit 1; }

fail() { echo "冒烟失败：$1"; exit 1; }

DOMAIN="gui/$(id -u)"
NOTE=""

# ── ① 定时器与常驻任务是否加载、下一次什么时候跑 ──────────────────
timer_out=$(launchctl print "$DOMAIN/com.eric.agent-points" 2>&1) \
  || fail "定时器 com.eric.agent-points 未加载（launchctl print 失败）：$timer_out"
echo "$timer_out" | grep -q "state" || fail "定时器已 print 但输出里没有 state 行，不符合手册判据"

# 手册判据 grep -E "state|Hour|Minute"：Hour/Minute 配对出计划时间
pairs=$(echo "$timer_out" | awk '
  /"Minute"/ { min=$NF; if (hour != "") { print hour ":" min; hour=""; min="" } ; next }
  /"Hour"/  { hour=$NF; if (min  != "") { print hour ":" min; hour=""; min="" } }')
[ -n "$pairs" ] || fail "定时器输出里没有 Hour/Minute 计划时间，定时可能没排上"

now_min=$(( 10#$(date +%H) * 60 + 10#$(date +%M) ))
next_line=""
next_tomorrow=0
while IFS= read -r p; do
  hh=${p%%:*}; mm=${p##*:}
  t=$(( 10#$hh * 60 + 10#$mm ))
  if [ $t -ge $now_min ]; then
    next_line="今天 $(printf '%02d:%02d' "$hh" "$mm")"
    break
  fi
done <<EOF
$(echo "$pairs" | sort -t: -k1,1n)
EOF
if [ -z "$next_line" ]; then
  p=$(echo "$pairs" | sort -t: -k1,1n | head -1)
  next_line="明天 ${p}"
  next_tomorrow=1
fi
timer_state=$(echo "$timer_out" | grep -m1 "^	state = " | sed 's/.*state = //')
[ -n "$timer_state" ] || timer_state=$(echo "$timer_out" | grep -m1 "state = " | sed 's/.*state = //')
echo "[1/6] 定时器已加载（state=$timer_state，闲置时 not running 属正常），下一次跑：$next_line"

mb_out=$(launchctl print "$DOMAIN/com.eric.agent-points.menubar" 2>&1) \
  || fail "菜单栏常驻 com.eric.agent-points.menubar 未加载（launchctl print 失败）：$mb_out"
mb_state=$(echo "$mb_out" | grep -m1 "state = " | sed 's/.*state = //')
if [ "$mb_state" = "running" ]; then
  echo "[1/6] 菜单栏常驻已加载且在跑（state=running）"
else
  NOTE="$NOTE 菜单栏常驻 state=$mb_state（未跑；若使用者从菜单点过「退出」则属预期）"
  echo "[1/6] 菜单栏常驻已加载，但 state=$mb_state，当前没在跑"
fi

# ── ② 今天的结论（只读 今日状态.json）──────────────────────────────
json_out=$(.venv/bin/python -c '
import json, datetime, sys
try:
    d = json.load(open("今日状态.json"))
except Exception as e:
    print("读今日状态.json失败：%s" % e); sys.exit(1)
today = datetime.date.today().isoformat()
if d.get("date") != today:
    print("今日状态.json 的日期是 %s，不是今天 %s" % (d.get("date"), today)); sys.exit(2)
items = d.get("items") or []
if not items:
    print("今日状态.json 的 items 是空的"); sys.exit(1)
bad = [i for i in items if i.get("state") not in ("claimed", "already")]
if bad:
    print("有异常状态：" + "、".join("%s=%s" % (i.get("name"), i.get("state")) for i in bad)); sys.exit(1)
print("今天 %s 的结论：%s" % (d.get("ran_at", "?"),
      "、".join("%s=%s" % (i.get("name"), i.get("state")) for i in items)))
' 2>&1)
rc=$?
if [ $rc -eq 2 ]; then
  # 手册第二节 shouldShow 规则：凌晨到早上 10 点之间没有今天的结论不算异常
  if [ $now_min -lt 600 ]; then
    NOTE="$NOTE 今日结论还没产出（凌晨窗口属正常）"
    echo "[2/6] $json_out，但现在还没到早上 10 点，按手册规则不算异常"
  else
    fail "$json_out，且已过 10 点（定时器该跑的 10:01 轮没出结论）"
  fi
elif [ $rc -ne 0 ]; then
  fail "$json_out"
else
  echo "[2/6] $json_out"
fi

# ── ③ 菜单栏 App 能不能正常出文字（手册第三节验证过的只打印口子）──
dump_out=$("/Applications/续杯.app/Contents/MacOS/AgentPoints" --dump 2>&1) \
  || fail "AgentPoints --dump 运行失败（退出码非 0）：$dump_out"
echo "$dump_out" | grep -q "标题" || fail "AgentPoints --dump 输出里没有「标题」行，菜单栏出字有问题：$dump_out"
echo "[3/6] 菜单栏 --dump 正常出字：$(echo "$dump_out" | grep -m1 "标题")"

# ── ④ 有没有残留的 QoderWake 守护进程（手册第七节：应为空）─────────
# 你正用着 Qoder/Trae 等家时，菜单栏会实时刷余额并短暂拉起 daemon，那是正常状态；
# 所以先等它自己退，退不掉且前台不是受支持的 App 才算故障。
resid=$(pgrep -f "qoderwake-cn __daemon" 2>/dev/null); rc=$?
if [ $rc -gt 1 ]; then fail "pgrep 执行异常（退出码 $rc）"; fi
if [ $rc -eq 0 ]; then
  for _ in 1 2 3; do
    sleep 5
    resid=$(pgrep -f "qoderwake-cn __daemon" 2>/dev/null) || break
  done
fi
if [ -n "$resid" ]; then
  front=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>/dev/null || echo "")
  case "$front" in
    Qoder*|Trae*|MiniMax*|Kimi*|ZCode*|DeepSeek*)
      echo "[4/6] QoderWake 在跑（pid: $(echo $resid | tr '\n' ' ')）——你正在用 $front，实时刷余额会短暂拉起它，不算故障" ;;
    *)
      fail "有残留的 qoderwake-cn __daemon 进程（pid: $(echo $resid | tr '\n' ' ')），脚本用完该自己退掉" ;;
  esac
else
  echo "[4/6] 无残留 QoderWake 守护进程"
fi

# ── ⑤ 语法编译检查（只 compile，绝不执行）──────────────────────────
.venv/bin/python -m py_compile 领积分.py || fail "领积分.py 语法编译不通过"
echo "[5/6] 领积分.py 语法编译通过（未执行）"

# ── ⑥ 日志尾行有没有 error ──────────────────────────────────────────
for f in 领积分.log 定时.log; do
  errs=$(tail -5 "$f" 2>/dev/null | grep -inE "error|traceback|失败|异常") && {
    fail "$f 尾行有报错：$errs"
  } || {
    [ -f "$f" ] || fail "$f 不存在"
  }
done
echo "[6/6] 领积分.log / 定时.log 尾行无报错"

echo "冒烟通过：定时器今天/明日下一轮 $next_line，今日结论各家均正常，菜单栏出字正常，无守护进程残留，语法通过，日志干净。$NOTE"
