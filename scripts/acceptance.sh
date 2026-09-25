#!/usr/bin/env bash
# 研究论证与发布服务验收脚本：
#   分角色 HTTP 请求验收权限、证据失效传播、版本比较、评审流转、发布包与审计；
#   命令行验收离线校验与服务重启恢复。
# 用法: scripts/acceptance.sh [端口]   （默认端口 8432，需先安装 .venv 依赖）
set -euo pipefail

PORT="${1:-8432}"
BASE="http://127.0.0.1:${PORT}"
WORK="$(mktemp -d)"
export ARCHAEOLOGY_DATABASE_PATH="${WORK}/acceptance.db"
PY="$(dirname "$0")/../.venv/bin/python"
[ -x "$PY" ] || PY="python3"
SERVER_PID=""

cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$WORK"; }
trap cleanup EXIT

json_get() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$2" <<<"$1"; }

# req METHOD PATH TOKEN BODY -> 全局 RESP_CODE / RESP_BODY
req() {
  local method="$1" path="$2" token="${3:-}" body="${4:-}"
  local args=(-s -o "${WORK}/resp.json" -w "%{http_code}" -X "$method" "${BASE}${path}" -H "Content-Type: application/json")
  [ -n "$token" ] && args+=(-H "Authorization: Bearer ${token}")
  [ -n "$body" ] && args+=(-d "$body")
  RESP_CODE="$(curl "${args[@]}")"
  RESP_BODY="$(cat "${WORK}/resp.json")"
}

expect() { # expect ACTUAL EXPECTED LABEL
  if [ "$1" != "$2" ]; then echo "FAIL: $3 (期望 $2, 实际 $1)"; echo "$RESP_BODY"; exit 1; fi
  echo "ok: $3"
}

start_server() {
  "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level warning &
  SERVER_PID=$!
  for _ in $(seq 1 50); do
    curl -sf "${BASE}/api/system/health" >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  echo "FAIL: 服务启动失败"; exit 1
}

stop_server() { kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; SERVER_PID=""; }

echo "== 初始化数据库并启动服务 =="
"$PY" -m app.cli init-db >/dev/null
start_server

echo "== 准备用户与项目（owner/researcher/recorder/reviewer/reviewer2/viewer） =="
declare -A TOK
for u in owner researcher recorder reviewer reviewer2 viewer; do
  curl -s -X POST "${BASE}/api/users" -H "Content-Type: application/json" \
    -d "{\"username\":\"${u}\",\"display_name\":\"${u}\",\"password\":\"Pass-${u}!2345\"}" >/dev/null
  TOK[$u]="$(curl -s -X POST "${BASE}/api/sessions" -H "Content-Type: application/json" \
    -d "{\"username\":\"${u}\",\"password\":\"Pass-${u}!2345\"}" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['token'])")"
done
req POST /api/projects "${TOK[owner]}" '{"code":"ACC","name":"验收项目","site_name":"验收遗址"}'
expect "$RESP_CODE" 201 "创建项目"
PID="$(json_get "$RESP_BODY" 'd["id"]')"
for u in researcher recorder reviewer reviewer2 viewer; do
  U_ID="$(curl -s -X POST "${BASE}/api/sessions" -H "Content-Type: application/json" -d "{\"username\":\"${u}\",\"password\":\"Pass-${u}!2345\"}" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['user_id'])")"
  role="$u"; [ "$u" = "reviewer2" ] && role="reviewer"
  req POST "/api/projects/${PID}/members" "${TOK[owner]}" "{\"user_id\":${U_ID},\"role\":\"${role}\"}"
  expect "$RESP_CODE" 200 "添加成员 ${u}(${role})"
done

echo "== 权限验收 =="
req POST "/api/projects/${PID}/claims" "${TOK[viewer]}" '{"claim_code":"CX","title":"x","statement":"x"}'
expect "$RESP_CODE" 403 "viewer 不能创建论点"
req GET "/api/projects/${PID}/claims" "${TOK[viewer]}"
expect "$RESP_CODE" 200 "viewer 可以读取论点"
req POST "/api/projects/${PID}/evidence" "${TOK[reviewer]}" '{"kind":"sample","ref_code":"E0","summary":"x"}'
expect "$RESP_CODE" 403 "reviewer 不能登记证据"

echo "== 证据与论点、依赖边、环路拒绝 =="
req POST "/api/projects/${PID}/evidence" "${TOK[researcher]}" '{"kind":"sample","ref_code":"EV-1","summary":"陶片样本","content":{"weight":10}}'
expect "$RESP_CODE" 201 "登记证据 EV-1"
req POST "/api/projects/${PID}/evidence" "${TOK[researcher]}" '{"kind":"statistic","ref_code":"SEC-1","summary":"内部统计","content":{"n":7},"visibility":"restricted"}'
expect "$RESP_CODE" 201 "登记受限证据 SEC-1"
for c in A B C; do
  req POST "/api/projects/${PID}/claims" "${TOK[researcher]}" "{\"claim_code\":\"${c}\",\"title\":\"论点${c}\",\"statement\":\"陈述${c}\"}"
  expect "$RESP_CODE" 201 "创建论点 ${c}"
done
req POST "/api/projects/${PID}/edges" "${TOK[researcher]}" '{"source_claim_code":"A","relation":"supports","evidence_ref":"EV-1","evidence_version":1}'
expect "$RESP_CODE" 201 "A 引用 EV-1@v1"
req POST "/api/projects/${PID}/edges" "${TOK[researcher]}" '{"source_claim_code":"B","relation":"supports","target_claim_code":"A"}'
expect "$RESP_CODE" 201 "B 依赖 A"
req POST "/api/projects/${PID}/edges" "${TOK[researcher]}" '{"source_claim_code":"C","relation":"supports","target_claim_code":"B"}'
expect "$RESP_CODE" 201 "C 依赖 B"
req POST "/api/projects/${PID}/edges" "${TOK[researcher]}" '{"source_claim_code":"A","relation":"supports","target_claim_code":"C"}'
expect "$RESP_CODE" 409 "拒绝依赖环"
CYCLE="$(json_get "$RESP_BODY" 'd["error"]["details"]["cycle_path"]')"
expect "$CYCLE" "['A', 'C', 'B', 'A']" "返回成环路径"

echo "== 版本比较（HTTP 与命令行） =="
req POST "/api/projects/${PID}/evidence/EV-1/versions" "${TOK[researcher]}" '{"summary":"陶片样本复测","content":{"weight":12}}'
expect "$RESP_CODE" 201 "追加 EV-1 v2"
req GET "/api/projects/${PID}/evidence/diff?ref_code=EV-1&from_version=1&to_version=2" "${TOK[researcher]}"
expect "$RESP_CODE" 200 "HTTP 版本比较"
json_get "$RESP_BODY" '[i["field"] for i in d["changed"]]' >/dev/null
"$PY" -m app.cli evidence-diff --project-id "$PID" --ref-code EV-1 --from-version 1 --to-version 2 | grep -q '"summary"' \
  && echo "ok: 命令行版本比较" || { echo "FAIL: 命令行版本比较"; exit 1; }

echo "== 证据失效沿依赖图传播 =="
req POST "/api/projects/${PID}/evidence/EV-1/withdraw" "${TOK[recorder]}" '{"reason":"样本污染"}'
expect "$RESP_CODE" 403 "recorder 不能撤回证据"
req POST "/api/projects/${PID}/evidence/EV-1/withdraw" "${TOK[researcher]}" '{"reason":"样本污染"}'
expect "$RESP_CODE" 200 "researcher 撤回证据"
AFFECTED="$(json_get "$RESP_BODY" 'd["affected_claims"]')"
expect "$AFFECTED" "['A', 'B', 'C']" "失效沿依赖图传播到 A/B/C"
req GET "/api/projects/${PID}/claims/A" "${TOK[viewer]}"
expect "$(json_get "$RESP_BODY" 'd["status"]')" "affected" "论点 A 标记为受影响（历史结论保留未删除）"

echo "== 受限证据的安全投影 =="
req POST "/api/projects/${PID}/claims" "${TOK[researcher]}" '{"claim_code":"D","title":"论点D","statement":"依赖内部统计"}'
req POST "/api/projects/${PID}/edges" "${TOK[researcher]}" '{"source_claim_code":"D","relation":"supports","evidence_ref":"SEC-1","evidence_version":1}'
req GET "/api/projects/${PID}/edges?claim_code=D" "${TOK[viewer]}"
expect "$(json_get "$RESP_BODY" 'd["data"][0].get("evidence_redacted", False)')" "True" "viewer 看到受限证据占位"
expect "$(json_get "$RESP_BODY" '"evidence_summary" in d["data"][0]')" "False" "viewer 看不到受限证据摘要"
req GET "/api/projects/${PID}/edges?claim_code=D" "${TOK[researcher]}"
expect "$(json_get "$RESP_BODY" 'd["data"][0].get("evidence_summary","")')" "内部统计" "researcher 可见受限证据摘要"

echo "== 评审流转：法定人数快照、自审禁令、重复回调 =="
req PUT "/api/projects/${PID}/review-strategy" "${TOK[owner]}" '{"quorum":2,"eligible_roles":["reviewer"]}'
expect "$RESP_CODE" 200 "设置评审策略 quorum=2"
req POST "/api/projects/${PID}/manuscripts" "${TOK[researcher]}" '{"title":"阶段认识论证稿","claim_codes":["A","B","C","D"]}'
expect "$RESP_CODE" 201 "创建稿件"
MID="$(json_get "$RESP_BODY" 'd["id"]')"
req POST "/api/projects/${PID}/manuscripts/${MID}/submit" "${TOK[researcher]}"
expect "$RESP_CODE" 200 "送审"
req PUT "/api/projects/${PID}/review-strategy" "${TOK[owner]}" '{"quorum":1,"eligible_roles":["reviewer"]}'
expect "$RESP_CODE" 200 "送审后修改策略（不影响快照）"
req POST "/api/projects/${PID}/manuscripts/${MID}/reviews" "${TOK[researcher]}" '{"decision":"approve","comment":"自审"}'
expect "$RESP_CODE" 403 "作者不能批准自己的稿件"
req POST "/api/projects/${PID}/manuscripts/${MID}/reviews" "${TOK[reviewer]}" '{"decision":"approve","comment":"同意"}'
expect "$(json_get "$RESP_BODY" 'd["status"]')" "in_review" "第一票后仍在评审（快照 quorum=2）"
req POST "/api/projects/${PID}/manuscripts/${MID}/reviews" "${TOK[reviewer]}" '{"decision":"approve","comment":"重复回调"}'
expect "$(json_get "$RESP_BODY" 'd["duplicate"]')" "True" "重复回调幂等返回"
req POST "/api/projects/${PID}/manuscripts/${MID}/reviews" "${TOK[reviewer2]}" '{"decision":"approve","comment":"同意"}'
expect "$(json_get "$RESP_BODY" 'd["status"]')" "approved" "第二票达到法定人数"

echo "== 发布：并发/重复不发布两次，发布包可离线校验 =="
req POST "/api/projects/${PID}/manuscripts/${MID}/publish" "${TOK[reviewer]}"
expect "$(json_get "$RESP_BODY" 'd["duplicate"]')" "False" "首次发布"
HASH1="$(json_get "$RESP_BODY" 'd["manifest_hash"]')"
req POST "/api/projects/${PID}/manuscripts/${MID}/publish" "${TOK[reviewer]}"
expect "$(json_get "$RESP_BODY" 'd["duplicate"]')" "True" "重复发布返回既有结果"
expect "$(json_get "$RESP_BODY" 'd["manifest_hash"]')" "$HASH1" "发布哈希一致（未发布两次）"
req GET "/api/projects/${PID}/manuscripts/${MID}/publication" "${TOK[owner]}"
expect "$RESP_CODE" 200 "获取发布包"
expect "$(json_get "$RESP_BODY" '[c["claim_code"] for c in d["package"]["sections"]["claims"]]')" "['A', 'B', 'C', 'D']" "发布包论点稳定排序"
"$PY" -m app.cli export-publication --manuscript-id "$MID" --out "${WORK}/pkg" >/dev/null
"$PY" -m app.cli verify-publication --package "${WORK}/pkg/package.json" --manifest "${WORK}/pkg/manifest.json" >/dev/null \
  && echo "ok: 发布包离线校验通过" || { echo "FAIL: 发布包离线校验"; exit 1; }
"$PY" - "$WORK" <<'EOF'
import json, sys
work = sys.argv[1]
pkg = json.load(open(f"{work}/pkg/package.json", encoding="utf-8"))
pkg["sections"]["claims"][0]["title"] = "被篡改"
json.dump(pkg, open(f"{work}/pkg/package.json", "w", encoding="utf-8"), ensure_ascii=False)
EOF
if "$PY" -m app.cli verify-publication --package "${WORK}/pkg/package.json" --manifest "${WORK}/pkg/manifest.json" >/dev/null 2>&1; then
  echo "FAIL: 篡改后的发布包不应通过校验"; exit 1
fi
echo "ok: 篡改后的发布包被离线校验拒绝"

echo "== 审计哈希链 =="
"$PY" -m app.cli verify-audit >/dev/null && echo "ok: 审计链校验通过" || { echo "FAIL: 审计链校验"; exit 1; }

echo "== 服务重启恢复 =="
stop_server
start_server
req GET "/api/projects/${PID}/manuscripts/${MID}/publication" "${TOK[owner]}"
expect "$RESP_CODE" 200 "重启后发布包仍可读"
expect "$(json_get "$RESP_BODY" 'd["manifest_hash"]')" "$HASH1" "重启后发布哈希一致"
"$PY" -m app.cli restart-check >/dev/null && echo "ok: 重启恢复检查通过" || { echo "FAIL: 重启恢复检查"; exit 1; }

echo "== 撤回已发布稿件 =="
req POST "/api/projects/${PID}/manuscripts/${MID}/withdraw" "${TOK[researcher]}" '{"reason":"新发掘区发现矛盾证据"}'
expect "$RESP_CODE" 200 "撤回已发布稿件"
req GET "/api/projects/${PID}/manuscripts/${MID}/publication" "${TOK[owner]}"
expect "$(json_get "$RESP_BODY" 'd["manuscript_status"]')" "withdrawn" "撤回后发布包仍可审计"

echo ""
echo "全部验收通过。"
