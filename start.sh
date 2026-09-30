#!/usr/bin/env bash
#
# at-ai-xhs 一键启动 / 停止 / 重启
#
#   ./start.sh            杀掉已占用端口，启动前后端（默认）
#   ./start.sh restart    同上
#   ./start.sh stop       停止前后端
#   ./start.sh status     查看运行状态
#   ./start.sh backend    只启后端（会先杀掉后端端口）
#   ./start.sh frontend   只启前端（会先杀掉前端端口）
#
# 日志：logs/backend.log、logs/frontend.log
# PID ：.run/backend.pid、.run/frontend.pid
#
# ⚠️ 改这个脚本时，变量引用**一律写成 ${var}**，不要写 $var。
#    实测（bash 3.2，macOS 自带）：
#      LC_CTYPE=C           $label）  → 正常
#      LC_CTYPE=zh_CN.UTF-8 $label）  → label）: unbound variable
#      LC_CTYPE=zh_CN.UTF-8 ${label}）→ 正常
#    中文终端下 LC_CTYPE 是 UTF-8，bash 3.2 会把全角括号的字节**并进变量名**，
#    再撞上下面那句 `set -u` 就直接中断。`sh start.sh` 和 `./start.sh` 都可能中招，
#    而且只在"端口恰好被占用"这类分支上触发，很容易以为脚本没坏。
#
set -uo pipefail

# ------------------------------- 配置 -------------------------------------- #
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PORT="${BACKEND_PORT:-5409}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
LOG_DIR="$ROOT/logs"
RUN_DIR="$ROOT/.run"
VENV_PY="$ROOT/.venv/bin/python"

# 有些机器上 node 装在 /usr/local/bin 但没进非交互 shell 的 PATH，
# 这里显式补上，避免 "npm: command not found"。
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"

# ------------------------------- 输出 -------------------------------------- #
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_DIM=''; C_OFF=''
fi
ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
info() { printf '%s·%s %s\n' "$C_BLUE" "$C_OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
err()  { printf '%s✗%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
dim()  { printf '%s  %s%s\n' "$C_DIM" "$*" "$C_OFF"; }
title(){ printf '\n%s== %s ==%s\n' "$C_BLUE" "$*" "$C_OFF"; }

# ---------------------------- 端口 / 进程 ---------------------------------- #
# 列出监听某端口的 PID（可能多个）
port_pids() {
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

port_busy() {
  [ -n "$(port_pids "$1")" ]
}

# 杀掉占用端口的进程（连同子进程），优雅退出失败则 kill -9
kill_port() {
  local port="$1" label="$2" pids
  pids="$(port_pids "$port")"

  if [ -z "$pids" ]; then
    dim "端口 $port 空闲"
    return 0
  fi

  warn "端口 ${port} 已被占用（${label}），准备强制结束"
  local comma_pids
  comma_pids="$(echo "$pids" | tr '\n' ',' | sed 's/,$//')"
  # 打印是什么进程占着，避免误杀时一头雾水
  ps -o pid=,comm= -p "$comma_pids" 2>/dev/null | while read -r pid comm; do
    dim "PID $pid  $comm"
  done

  # 先杀子进程再杀父进程，防止 npm 被杀了 vite 还活着
  local pid child
  for pid in $pids; do
    pkill -TERM -P "$pid" 2>/dev/null || true
  done
  kill -TERM $pids 2>/dev/null || true

  local i
  for i in $(seq 1 24); do
    sleep 0.25
    port_busy "$port" || { ok "端口 $port 已释放"; return 0; }
  done

  warn "优雅退出超时，改用 kill -9"
  for pid in $pids; do
    pkill -KILL -P "$pid" 2>/dev/null || true
  done
  kill -KILL $pids 2>/dev/null || true
  sleep 1

  if port_busy "$port"; then
    err "端口 $port 仍然被占用，请手动处理：lsof -iTCP:$port -sTCP:LISTEN"
    return 1
  fi
  ok "端口 $port 已强制释放"
}

# ---------------------------- 启动前检查 ----------------------------------- #
check_backend_env() {
  [ -x "$VENV_PY" ] || {
    err "找不到虚拟环境：$VENV_PY"
    dim "先执行："
    dim "  python3 -m venv .venv"
    dim "  .venv/bin/pip install -r requirements.txt"
    return 1
  }
  [ -f "$ROOT/sau_backend.py" ] || { err "找不到 sau_backend.py"; return 1; }
  return 0
}

check_frontend_env() {
  command -v npm >/dev/null 2>&1 || {
    err "找不到 npm，请先安装 Node.js"
    return 1
  }
  [ -d "$ROOT/sau_frontend/node_modules" ] || {
    err "前端依赖未安装：sau_frontend/node_modules 不存在"
    dim "先执行： cd sau_frontend && npm install"
    return 1
  }
  return 0
}

# 等待 HTTP 就绪
wait_http() {
  local url="$1" label="$2" tries="${3:-40}" i
  for i in $(seq 1 "$tries"); do
    if curl -sf -o /dev/null --max-time 2 "$url" 2>/dev/null; then
      ok "$label 已就绪"
      return 0
    fi
    sleep 0.5
  done
  err "$label 启动超时（${tries} 次探测）"
  return 1
}

# 首次启动会随机生成后台登录密码（myUtils/web_auth.py），但它只打进日志里，
# 而 start.sh 是用 nohup 起的、终端上看不到——不主动捞一次的话，第一次用的人
# 会卡在登录页上不知道密码是什么。
#
# 只提示**本次启动新打印的**那条：日志是追加的，不加偏移量的话每次重启都会把
# 历史里那条旧密码再翻出来提示一遍（那就成了"密码到底是哪个"的新困惑）。
show_login_hint() {
  local mark="${1:-0}" fresh
  [ -f "$LOG_DIR/backend.log" ] || return 0
  fresh="$(tail -c "+$((mark + 1))" "$LOG_DIR/backend.log" 2>/dev/null || true)"
  case "${fresh}" in
    *已生成后台登录密码*) ;;
    *) return 0 ;;
  esac

  # 注意 `^ *密码：` 这个锚点：日志里还有一行「改密码：编辑 settings.json…」，
  # 它不是密码本身，锚住行首才不会把它当成密码抓出来。
  local user pass
  user="$(printf '%s\n' "${fresh}" | grep -m1 '^ *账号：' | sed 's/^ *账号：//' || true)"
  pass="$(printf '%s\n' "${fresh}" | grep -m1 '^ *密码：' | sed 's/^ *密码：//' || true)"

  printf '\n'
  ok "已启用后台登录"
  dim "账号 ${user:-admin}"
  dim "密码 ${pass:-（见 settings.json 的 auth.password）}"
  dim "改密码：编辑 settings.json 的 auth.password，或删掉该字段后重启"
}

# ------------------------------ 后端 --------------------------------------- #
start_backend() {
  title "后端"
  kill_port "$BACKEND_PORT" "后端" || return 1
  check_backend_env || return 1

  mkdir -p "$LOG_DIR" "$RUN_DIR"
  # 超过 5MB 就切一份旧的，避免日志无限膨胀
  if [ -f "$LOG_DIR/backend.log" ] && [ "$(wc -c < "$LOG_DIR/backend.log")" -gt 5242880 ]; then
    mv "$LOG_DIR/backend.log" "$LOG_DIR/backend.log.1"
    info "日志已轮转：logs/backend.log.1"
  fi
  # 记下追加前的日志长度，供 show_login_hint 只捞"本次启动新打印的"那几行
  local log_mark=0
  [ -f "$LOG_DIR/backend.log" ] && log_mark="$(wc -c < "$LOG_DIR/backend.log")"
  info "启动 Flask（端口 ${BACKEND_PORT}）…"
  cd "$ROOT" || return 1
  # 追加而不是覆盖：排查登录/发布问题时，重启一次就把现场清掉太伤了
  printf '\n===== 后端启动 %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_DIR/backend.log"
  # -u 关闭 Python 输出缓冲：否则 print() 的诊断日志会卡在缓冲区里，
  # 排查问题时看不到任何实时输出（这个坑刚踩过）
  nohup "$VENV_PY" -u sau_backend.py >> "$LOG_DIR/backend.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$RUN_DIR/backend.pid"

  # ⚠️ 探测地址必须是**公开接口**。整个后台现在都要登录（myUtils/web_auth.py），
  #    业务接口（原来的 /xhs/accounts）未登录会回 401，而 `curl -sf` 把 4xx 当失败，
  #    拿它探测会把"后端其实已经起来了"误报成"启动超时/启动失败"。
  #    /auth/status 是刻意留的公开探活接口，200 且不泄漏任何信息。
  if wait_http "http://127.0.0.1:$BACKEND_PORT/auth/status" "后端" 40; then
    dim "PID $pid   日志 logs/backend.log"
    show_login_hint "$log_mark"
    return 0
  fi
  err "启动失败，最后 15 行日志："
  tail -15 "$LOG_DIR/backend.log" 2>/dev/null | sed 's/^/    /'
  return 1
}

# ------------------------------ 前端 --------------------------------------- #
start_frontend() {
  title "前端"
  kill_port "$FRONTEND_PORT" "前端" || return 1
  check_frontend_env || return 1

  mkdir -p "$LOG_DIR" "$RUN_DIR"
  info "启动 Vite（端口 ${FRONTEND_PORT}）…"
  cd "$ROOT/sau_frontend" || return 1
  printf '\n===== 前端启动 %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_DIR/frontend.log"
  nohup npm run dev >> "$LOG_DIR/frontend.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$RUN_DIR/frontend.pid"

  if wait_http "http://127.0.0.1:$FRONTEND_PORT/" "前端" 60; then
    dim "PID $pid   日志 logs/frontend.log"
    return 0
  fi
  err "启动失败，最后 15 行日志："
  tail -15 "$LOG_DIR/frontend.log" 2>/dev/null | sed 's/^/    /'
  return 1
}

# ------------------------------ 停止 --------------------------------------- #
stop_all() {
  title "停止服务"
  kill_port "$FRONTEND_PORT" "前端"
  kill_port "$BACKEND_PORT" "后端"
  rm -f "$RUN_DIR"/*.pid 2>/dev/null || true
  ok "已全部停止"
}

# ------------------------------ 状态 --------------------------------------- #
show_status() {
  title "运行状态"
  local port label
  for pair in "$BACKEND_PORT:后端(Flask)" "$FRONTEND_PORT:前端(Vite)"; do
    port="${pair%%:*}"; label="${pair##*:}"
    if port_busy "$port"; then
      local pids
      pids="$(port_pids "$port" | tr '\n' ' ')"
      ok "$label  端口 $port 运行中  PID: $pids"
    else
      warn "$label  端口 $port 未运行"
    fi
  done
  echo
  dim "后端: http://127.0.0.1:$BACKEND_PORT"
  dim "前端: http://127.0.0.1:$FRONTEND_PORT/#/xhs-workbench"
}

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# ------------------------------ 入口 --------------------------------------- #
mkdir -p "$LOG_DIR" "$RUN_DIR"

case "${1:-start}" in
  start|restart|"")
    start_backend || { err "后端启动失败，已中止"; exit 1; }
    start_frontend || { err "前端启动失败，已中止"; exit 1; }
    show_status
    printf '\n%s打开浏览器：%shttp://localhost:%s/#/xhs-workbench%s\n' \
      "$C_GREEN" "$C_BLUE" "$FRONTEND_PORT" "$C_OFF"
    dim "停止服务： ./start.sh stop"
    dim "查看日志： tail -f logs/backend.log logs/frontend.log"
    ;;
  stop)
    stop_all
    ;;
  backend)
    start_backend && show_status
    ;;
  frontend)
    start_frontend && show_status
    ;;
  status)
    show_status
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    err "未知参数：$1"
    echo
    usage
    exit 1
    ;;
esac
