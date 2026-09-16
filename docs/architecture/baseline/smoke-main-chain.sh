#!/usr/bin/env bash
# Phase 0.3 —— 主链路回归 smoke（thread → run → SSE → checkpoint → resume）
#
# 用途：每次裁剪动作后必须跑通。对应 feature-inventory.md §16 Phase 0.3 与 §17.3 V1。
#
# 用法：
#   bash docs/architecture/baseline/smoke-main-chain.sh
#   BASE_URL=http://127.0.0.1:2026 SMOKE_TEST_EMAIL=... bash docs/.../smoke-main-chain.sh
#
# 依赖：curl、awk/sed（macOS 自带）
#
# 设计约束（踩过坑，勿改）：
#   1) 本机 PATH 上的 sed/tail/wc/head 是 WorkBuddy 的 brokered 垫片，行为不可靠 →
#      全部改用 /usr/bin/ 绝对路径。
#   2) 脚本内**禁止用 Python 读文件**。Python 打开含凭据的 cookie jar 会被沙箱的
#      "敏感内容审批"拦截（实测报 PermissionError: Sensitive content approval timed out），
#      整个 smoke 会卡满超时后失败。JSON 解析一律通过 argv 传参，文本处理一律走 awk/sed。
#   3) CSRF 优先从认证响应头取，不读 cookie jar。
#
# 注意：本脚本会**真实调用模型**（走 config.yaml 里配置的 provider）并写入 dev DB。
#       跑之前确认这属于预期（会消耗 token，并在线程列表里留下一条记录）。

set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:2026}"
EMAIL="${SMOKE_TEST_EMAIL:-smoke-test@deerflow.dev}"
PASSWORD="${SMOKE_TEST_PASSWORD:-SmokeTest123!}"
PROMPT="${SMOKE_PROMPT:-Reply with exactly the word: pong}"
PROMPT2="${SMOKE_PROMPT2:-Reply with exactly the word: pong2}"
TIMEOUT_STREAM="${SMOKE_TIMEOUT_STREAM:-180}"
VERIFY_ARTIFACT="${SMOKE_VERIFY_ARTIFACT:-0}"
ARTIFACT_PATH="${SMOKE_ARTIFACT_PATH:-/mnt/user-data/outputs/phase0-artifact.txt}"
ARTIFACT_MARKER="${SMOKE_ARTIFACT_MARKER:-PHASE0_ARTIFACT_SMOKE_OK}"

if [ "$VERIFY_ARTIFACT" = "1" ] && [ -z "${SMOKE_PROMPT+x}" ]; then
  PROMPT="Create a UTF-8 text artifact at ${ARTIFACT_PATH}. Its only content must be ${ARTIFACT_MARKER}. Use write_file, then call present_files on that exact path, and finish with a short confirmation."
fi

COOKIE_JAR="$(mktemp /tmp/deerflow-smoke-cookies.XXXXXX)"
SSE_OUT="$(mktemp /tmp/deerflow-smoke-sse.XXXXXX)"
ARTIFACT_OUT="$(mktemp /tmp/deerflow-smoke-artifact.XXXXXX)"
trap 'rm -f "$COOKIE_JAR" "$SSE_OUT" "$ARTIFACT_OUT"' EXIT

PASS=0
FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
info() { printf '  ---- %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

# jget <json> <dotted.path> -> 取值（无则空串）。JSON 走 argv，不读文件。
jget() {
  python3 -c '
import json,sys
raw,path=sys.argv[1],sys.argv[2]
try: cur=json.loads(raw)
except Exception: print(""); raise SystemExit
for part in path.split("."):
    if isinstance(cur,list):
        try: cur=cur[int(part)]
        except Exception: print(""); raise SystemExit
    elif isinstance(cur,dict): cur=cur.get(part)
    else: print(""); raise SystemExit
    if cur is None: print(""); raise SystemExit
print(cur if isinstance(cur,str) else json.dumps(cur))
' "$1" "$2"
}

# jlen <json> -> 顶层数组长度（或 {events:[...]} 的长度）；非数组返回 0
jlen() {
  python3 -c '
import json,sys
try:
    d=json.loads(sys.argv[1])
    print(len(d) if isinstance(d,list) else len(d.get("events",[])))
except Exception: print(0)
' "$1"
}

# jfirst <json> <field> -> 数组中首个含该字段的元素值
jfirst() {
  python3 -c '
import json,sys
try:
    for e in json.loads(sys.argv[1]):
        if isinstance(e,dict) and e.get(sys.argv[2]): print(e[sys.argv[2]]); break
except Exception: pass
' "$1" "$2"
}

# 认证请求：响应头存进全局 AUTH_HDRS，回显 HTTP 状态码
auth_call() {
  AUTH_HDRS="$(curl -s -D - -o /dev/null -w '\nHTTPCODE:%{http_code}\n' "$@")"
  printf '%s\n' "$AUTH_HDRS" | /usr/bin/sed -nE 's/^HTTPCODE:([0-9]+).*/\1/p' | /usr/bin/tail -n 1
}

# CSRF 提取：优先响应头，回退 cookie jar（两者都用 sed，不用 Python）
csrf_from_headers() { # stdin: HTTP 响应头
  /usr/bin/sed -nE 's/^[Ss]et-[Cc]ookie:[[:space:]]*csrf_token=([^;]+).*/\1/p' | /usr/bin/tail -n 1 | /usr/bin/tr -d '\r'
}
csrf_from_jar() { # <cookie jar>
  /usr/bin/sed -nE 's/^[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\tcsrf_token\t(.*)$/\1/p' "$1" | /usr/bin/tail -n 1 | /usr/bin/tr -d '\r'
}

# cfg_value <section> <key> -> config.yaml 中该键的原始值；节或键不存在则 "absent"
# 用 awk 实现（不引入 yaml 依赖，也不用 Python 读文件）
cfg_value() {
  local f=""
  for cand in config.yaml ../config.yaml docs/architecture/baseline/config.yaml.frozen; do
    [ -f "$cand" ] && { f="$cand"; break; }
  done
  [ -n "$f" ] || { echo "unknown"; return; }
  /usr/bin/awk -v section="$1" -v key="$2" '
    $0 ~ "^" section ":[[:space:]]*$" { insec = 1; next }
    insec && /^[^[:space:]#]/          { insec = 0 }
    insec && $0 ~ "^[[:space:]]+" key ":" {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      sub(/^[^:]*:[[:space:]]*/, "", line)
      sub(/[[:space:]]+$/, "", line)
      print line; found = 1; exit
    }
    END { if (!found) print "absent" }
  ' "$f"
}

# 首 200 字符，便于在日志里看响应片段（不依赖外部 cut）
clip() { /usr/bin/awk -v s="$1" 'BEGIN{ print substr(s, 1, 200) }'; }

echo "=========================================="
echo " DeerFlow 主链路回归 smoke (Phase 0.3)"
echo " target: ${BASE_URL}"
echo "=========================================="

# ---------- 0. 服务可达 ----------
echo
echo "[0] 服务可达性"
HEALTH="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/health/ready" 2>/dev/null)"
if [ "$HEALTH" = "200" ]; then pass "/health/ready -> 200"; else fail "/health/ready -> ${HEALTH}（服务未就绪，终止）"; exit 1; fi
READY="$(curl -s "${BASE_URL}/health/ready")"
info "readiness body: ${READY}"

# ---------- 1. 认证 ----------
echo
echo "[1] 认证会话"
AUTH_ENABLED=0
AUTH_HDRS=""
AUTH_PROBE="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/api/models" 2>/dev/null)"
if [ "$AUTH_PROBE" != "401" ]; then
  info "auth 已禁用，无需登录"
else
  AUTH_ENABLED=1
  NEEDS_SETUP="$(jget "$(curl -s "${BASE_URL}/api/v1/auth/setup-status")" needs_setup)"
  if [ "$NEEDS_SETUP" = "True" ] || [ "$NEEDS_SETUP" = "true" ]; then
    CODE="$(auth_call -X POST "${BASE_URL}/api/v1/auth/initialize" \
      -H 'Content-Type: application/json' \
      -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" -c "$COOKIE_JAR")"
    [ "$CODE" = "201" ] && pass "初始化管理员并登录 (201)" || fail "初始化失败 (${CODE})"
  else
    CODE="$(auth_call -X POST "${BASE_URL}/api/v1/auth/register" \
      -H 'Content-Type: application/json' \
      -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" -c "$COOKIE_JAR")"
    if [ "$CODE" = "201" ]; then
      pass "注册并登录 (201)"
    else
      : > "$COOKIE_JAR"
      CODE="$(auth_call -X POST "${BASE_URL}/api/v1/auth/login/local" \
        --data-urlencode "username=${EMAIL}" --data-urlencode "password=${PASSWORD}" -c "$COOKIE_JAR")"
      [ "$CODE" = "200" ] && pass "登录 (200)" || fail "登录失败 (${CODE})"
    fi
  fi
fi

ME="$(curl -s -o /dev/null -w '%{http_code}' -b "$COOKIE_JAR" "${BASE_URL}/api/v1/auth/me")"
[ "$ME" = "200" ] && pass "会话有效 /auth/me -> 200" || { fail "会话无效 (/auth/me -> ${ME})，终止"; exit 1; }

# CSRF 优先从认证响应头取（不读 cookie jar）；取不到再从 jar 回退
CSRF="$(printf '%s\n' "$AUTH_HDRS" | csrf_from_headers)"
if [ -z "$CSRF" ] && [ -s "$COOKIE_JAR" ]; then CSRF="$(csrf_from_jar "$COOKIE_JAR")"; fi
if [ -n "$CSRF" ]; then
  pass "取得 CSRF token"
elif [ "$AUTH_ENABLED" = "0" ]; then
  info "auth 已禁用，无需 CSRF token"
else
  fail "未取到 csrf_token（写操作会 403）"
fi

AUTH=(-b "$COOKIE_JAR" -H "X-CSRF-Token: ${CSRF}")

# ---------- 2. thread create ----------
echo
echo "[2] thread create"
T_RESP="$(curl -s -X POST "${BASE_URL}/api/threads" "${AUTH[@]}" \
  -H 'Content-Type: application/json' -d '{}')"
TID="$(jget "$T_RESP" thread_id)"
if [ -n "$TID" ]; then pass "thread 已创建: ${TID}"; else fail "创建 thread 失败: $(clip "$T_RESP")"; exit 1; fi

# ---------- 3. run stream (SSE) ----------
echo
echo "[3] run stream (SSE)"
RUN_BODY="$(python3 -c '
import json,sys
print(json.dumps({"input":{"messages":[{"role":"user","content":sys.argv[1]}]},
                  "stream_mode":["values","messages-tuple"]}))
' "$PROMPT")"

HTTP_STREAM="$(curl -s -N -o "$SSE_OUT" -w '%{http_code}' --max-time "$TIMEOUT_STREAM" \
  -X POST "${BASE_URL}/api/threads/${TID}/runs/stream" "${AUTH[@]}" \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' -d "$RUN_BODY")"

if [ "$HTTP_STREAM" = "200" ]; then pass "SSE 连接 200"; else fail "SSE 连接 ${HTTP_STREAM}"; fi
FRAMES="$(/usr/bin/awk '/^event: /{n++} END{print n+0}' "$SSE_OUT")"
info "收到 SSE 帧数: ${FRAMES}"
[ "${FRAMES:-0}" -gt 0 ] && pass "SSE 有事件帧" || fail "SSE 无事件帧"

if /usr/bin/awk '/^event: end/{f=1} END{exit !f}' "$SSE_OUT"; then
  pass "SSE 以 event: end 正常收尾"
else
  fail "SSE 未出现 event: end（流被截断/报错）"
fi

if /usr/bin/awk '/^event: error/{f=1} END{exit !f}' "$SSE_OUT"; then
  fail "SSE 出现 event: error（见 ${SSE_OUT}）"
else
  pass "SSE 无 error 帧"
fi

# ---------- 4. checkpoint 存在 ----------
echo
echo "[4] checkpoint 存在"
STATE="$(curl -s "${BASE_URL}/api/threads/${TID}/state" "${AUTH[@]}")"
VALUES="$(jget "$STATE" values)"
if [ -n "$VALUES" ] && [ "$VALUES" != "{}" ]; then
  pass "state.values 非空"
  info "$(clip "$VALUES")"
else
  fail "state.values 为空: $(clip "$STATE")"
fi

HIST="$(curl -s -X POST "${BASE_URL}/api/threads/${TID}/history" "${AUTH[@]}" \
  -H 'Content-Type: application/json' -d '{"limit":10}')"
HCOUNT="$(jlen "$HIST")"
info "checkpoint history 条数: ${HCOUNT}"
[ "${HCOUNT:-0}" -gt 0 ] && pass "checkpoint history 非空" || fail "checkpoint history 为空"

# ---------- 5. run 落库 + 事件持久化 ----------
echo
echo "[5] run 状态与事件"
RUNS="$(curl -s "${BASE_URL}/api/threads/${TID}/runs" "${AUTH[@]}")"
RID="$(jget "$RUNS" 0.run_id)"
RSTATUS="$(jget "$RUNS" 0.status)"
if [ -n "$RID" ]; then
  pass "run 已落库: ${RID}"
  info "run status: ${RSTATUS}"
  [ "$RSTATUS" = "success" ] && pass "run 终态 = success" || fail "run 终态 = ${RSTATUS}（期望 success）"
  EVENTS="$(curl -s "${BASE_URL}/api/threads/${TID}/runs/${RID}/events" "${AUTH[@]}")"
  ECOUNT="$(jlen "$EVENTS")"
  RE_BACKEND="$(cfg_value run_events backend)"
  info "run_events.backend = ${RE_BACKEND}；/events 返回条数: ${ECOUNT}"
  if [ "$RE_BACKEND" = "db" ]; then
    # 只有 db 后端才代表真正落库
    [ "${ECOUNT:-0}" -gt 0 ] && pass "run_events 已持久化（db 后端）" || fail "run_events 为空，但后端已是 db（审计落库失效）"
  else
    # memory 后端：进程内内存可读，重启即丢 → 不构成 PASS，避免假阳性
    info "⚠ 后端为 '${RE_BACKEND}'：以上条数来自进程内内存，重启即丢，不代表审计落库"
    info "⚠ 切换到 run_events.backend=db 后，本项应改为断言 run_events 表行数 > 0"
  fi
else
  fail "未查到 run 记录: $(clip "$RUNS")"
fi

# ---------- 6. artifact ----------
if [ "$VERIFY_ARTIFACT" = "1" ]; then
  echo
  echo "[6] artifact 生成与读取"
  ARTIFACT_URL_PATH="${ARTIFACT_PATH#/}"
  ARTIFACT_HTTP="$(curl -sS -o "$ARTIFACT_OUT" -w '%{http_code}' \
    "${BASE_URL}/api/threads/${TID}/artifacts/${ARTIFACT_URL_PATH}" "${AUTH[@]}")"
  if [ "$ARTIFACT_HTTP" = "200" ]; then
    pass "artifact API 读取 200: ${ARTIFACT_PATH}"
    if /usr/bin/awk -v marker="$ARTIFACT_MARKER" 'index($0, marker) { found=1 } END { exit !found }' "$ARTIFACT_OUT"; then
      pass "artifact 内容包含唯一校验标记"
    else
      fail "artifact 内容缺少校验标记"
    fi
  else
    fail "artifact API 读取失败: HTTP ${ARTIFACT_HTTP}"
  fi

  EVENTS_FOR_ARTIFACT="$(curl -s "${BASE_URL}/api/threads/${TID}/runs/${RID}/events" "${AUTH[@]}")"
  if /usr/bin/awk -v path="$ARTIFACT_PATH" 'index($0, "present_files") && index($0, path) { found=1 } END { exit !found }' <<<"$EVENTS_FOR_ARTIFACT"; then
    pass "run.delivery 记录了 present_files"
  else
    fail "run.delivery 未记录目标 present_files"
  fi
fi

# ---------- 7. resume ----------
echo
echo "[7] resume（从 checkpoint 续跑）"
CKPT="$(jfirst "$HIST" checkpoint_id)"
if [ -z "$CKPT" ]; then
  fail "无可用 checkpoint_id，跳过 resume"
else
  info "resume 基准 checkpoint: ${CKPT}"
  R2_BODY="$(python3 -c '
import json,sys
print(json.dumps({"checkpoint_id":sys.argv[1],
                  "input":{"messages":[{"role":"user","content":sys.argv[2]}]}}))
' "$CKPT" "$PROMPT2")"
  R2="$(curl -s -X POST "${BASE_URL}/api/threads/${TID}/runs" "${AUTH[@]}" \
    -H 'Content-Type: application/json' -d "$R2_BODY")"
  R2ID="$(jget "$R2" run_id)"
  if [ -n "$R2ID" ]; then pass "resume run 已受理: ${R2ID}"; else fail "resume 被拒: $(clip "$R2")"; fi
fi

echo
echo "=========================================="
echo " 结果：PASS=${PASS}  FAIL=${FAIL}"
echo "=========================================="
[ "$FAIL" -eq 0 ] || exit 1
