#!/usr/bin/env bash
# 分角色 HTTP 验收：权限、证据失效传播、版本比较、环路拒绝、评审发布、审计与重启恢复。
# 用法：先启动服务（uvicorn app.main:app --port 8432），再执行 bash scripts/acceptance.sh
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8432}"
PASS='Accept!23456'
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

register() { # username display -> "user_id token"
  local uid
  uid=$(curl -sf -X POST "$BASE/api/users" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$1\",\"display_name\":\"$2\",\"password\":\"$PASS\"}" | jq -r .id \
    || curl -sf -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"$PASS\"}" >/dev/null)
  local token
  token=$(curl -sf -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"$PASS\"}" | jq -r .token)
  echo "$uid $token"
}

say "0. 健康检查"
curl -sf "$BASE/api/system/health" | jq -c .

say "1. 创建五种角色并登录"
read -r UID_OWNER T_OWNER <<< "$(register acc_owner 负责人)"
read -r UID_RES   T_RES   <<< "$(register acc_res 研究员)"
read -r UID_REC   T_REC   <<< "$(register acc_rec 记录员)"
read -r UID_REV   T_REV   <<< "$(register acc_rev 评审员)"
read -r UID_VIEW  T_VIEW  <<< "$(register acc_view 观察员)"
AUTH_OWNER="Authorization: Bearer $T_OWNER"; AUTH_RES="Authorization: Bearer $T_RES"
AUTH_REC="Authorization: Bearer $T_REC";    AUTH_REV="Authorization: Bearer $T_REV"
AUTH_VIEW="Authorization: Bearer $T_VIEW"

say "2. 负责人建项目并加成员"
PID=$(curl -sf -X POST "$BASE/api/projects" -H "$AUTH_OWNER" -H 'Idempotency-Key: acc-p1' -H 'Content-Type: application/json' \
  -d '{"code":"ACCEPT","name":"阶段性认识论证","site_name":"示例遗址"}' | jq .id)
for m in "$UID_RES:researcher" "$UID_REC:recorder" "$UID_REV:reviewer" "$UID_VIEW:viewer"; do
  curl -sf -X POST "$BASE/api/projects/$PID/members" -H "$AUTH_OWNER" -H 'Content-Type: application/json' \
    -d "{\"user_id\":${m%%:*},\"role\":\"${m##*:}\"}" | jq -c .
done

say "3. 权限：观察员不能登记资源、不能提论点（403）"
curl -s -o /dev/null -w 'viewer create resource -> %{http_code}\n' -X POST "$BASE/api/projects/$PID/resources" \
  -H "$AUTH_VIEW" -H 'Content-Type: application/json' -d '{"kind":"sample","code":"S-X","title":"x","data":{}}'
curl -s -o /dev/null -w 'viewer create claim   -> %{http_code}\n' -X POST "$BASE/api/projects/$PID/claims" \
  -H "$AUTH_VIEW" -H 'Content-Type: application/json' -d '{"code":"C-X","title":"x","statement":"y"}'

say "4. 记录员登记样品 v1，研究员建论点并钉版引用"
RID=$(curl -sf -X POST "$BASE/api/projects/$PID/resources" -H "$AUTH_REC" -H 'Content-Type: application/json' \
  -d '{"kind":"sample","code":"S-001","title":"H1 陶片","data":{"layer":"H1","weight":12.5}}' | jq .id)
CA=$(curl -sf -X POST "$BASE/api/projects/$PID/claims" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d '{"code":"C-1","title":"层位分期","statement":"H1 属于第一期"}' | jq .id)
CB=$(curl -sf -X POST "$BASE/api/projects/$PID/claims" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d '{"code":"C-2","title":"遗迹性质","statement":"H1 为储藏坑"}' | jq .id)
curl -sf -X POST "$BASE/api/claims/$CA/edges" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d "{\"edge_type\":\"supports\",\"target_resource_id\":$RID}" | jq -c '{pinned:.target.pinned_version,stale:.target.stale}'
curl -sf -X POST "$BASE/api/claims/$CB/edges" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d "{\"edge_type\":\"supports\",\"target_claim_id\":$CA}" | jq -c '{edge:.id,type:.edge_type}'

say "5. 环路拒绝：C-1 再依赖 C-2 将成环，返回 409 与环路路径"
curl -s -X POST "$BASE/api/claims/$CA/edges" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d "{\"edge_type\":\"supports\",\"target_claim_id\":$CB}" | jq -c '{code:.error.code,path:.error.details.path_codes}'

say "6. 证据失效：样品替换为 v2，C-1 与下游 C-2 被标记受影响（历史不删除）"
curl -sf -X POST "$BASE/api/resources/$RID/versions" -H "$AUTH_REC" -H 'Content-Type: application/json' \
  -d '{"data":{"layer":"H1","weight":13.1,"note":"复称"}}' >/dev/null
curl -sf "$BASE/api/projects/$PID/claims?affected=true" -H "$AUTH_RES" | jq -c '[.data[]|{code,affected}]'

say "7. 版本比较：v1 与 v2 的字段级差异"
curl -sf "$BASE/api/resources/$RID/diff?from=1&to=2" -H "$AUTH_REC" | jq -c '.changes'

say "8. 重新钉版到 v2 并解除受影响标记"
EID=$(curl -sf "$BASE/api/claims/$CA" -H "$AUTH_RES" | jq '.edges[0].id')
curl -sf -X POST "$BASE/api/edges/$EID/repin" -H "$AUTH_RES" -H 'Content-Type: application/json' -d '{"version":2}' | jq -c '{pinned:.target.pinned_version,stale:.target.stale}'
curl -sf "$BASE/api/projects/$PID/claims?affected=true" -H "$AUTH_RES" | jq -c '[.data[]|{code,affected}]'

say "9. 评审策略 quorum=2，论证稿草拟->送审（快照策略）"
curl -sf -X POST "$BASE/api/projects/$PID/review-policies" -H "$AUTH_OWNER" -H 'Content-Type: application/json' \
  -d '{"name":"双评审","quorum":2,"reviewer_roles":["owner","reviewer"]}' | jq -c '{name,quorum}'
AID=$(curl -sf -X POST "$BASE/api/projects/$PID/arguments" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d "{\"title\":\"第一期遗存论证\",\"summary\":\"基于层位与样品\",\"claim_ids\":[$CA,$CB]}" | jq .id)
curl -sf -X POST "$BASE/api/arguments/$AID/submit" -H "$AUTH_RES" | jq -c '{status,snapshot:.policy_snapshot.quorum}'

say "10. 作者不能批准自己的稿件（403 self_review）"
curl -sf -X POST "$BASE/api/projects/$PID/review-policies" -H "$AUTH_OWNER" -H 'Content-Type: application/json' \
  -d '{"name":"全员","quorum":2,"reviewer_roles":["owner","researcher","reviewer"]}' >/dev/null
AID2=$(curl -sf -X POST "$BASE/api/projects/$PID/arguments" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d "{\"title\":\"自审拦截演示\",\"summary\":\"\",\"claim_ids\":[$CA]}" | jq .id)
curl -sf -X POST "$BASE/api/arguments/$AID2/submit" -H "$AUTH_RES" >/dev/null
curl -s -X POST "$BASE/api/arguments/$AID2/reviews" -H "$AUTH_RES" -H 'Content-Type: application/json' \
  -d '{"decision":"approve"}' | jq -c .error

say "11. 法定人数按送审快照计算：送审后策略虽改为全员，该稿仍按快照 quorum=2 计票"
curl -sf -X POST "$BASE/api/arguments/$AID/reviews" -H "$AUTH_OWNER" -H 'Content-Type: application/json' -d '{"decision":"approve"}' | jq -c '{status:.argument_status}'
curl -sf -X POST "$BASE/api/arguments/$AID/reviews" -H "$AUTH_REV" -H 'Content-Type: application/json' -d '{"decision":"approve"}' | jq -c '{status:.argument_status}'
echo "重复回调（同一评审人同一结论）:"
curl -sf -X POST "$BASE/api/arguments/$AID/reviews" -H "$AUTH_REV" -H 'Content-Type: application/json' -d '{"decision":"approve"}' | jq -c '{replayed,status:.argument_status}'

say "12. 发布（幂等：重复发布返回同一记录）+ 发布包结构"
PUB1=$(curl -sf -X POST "$BASE/api/arguments/$AID/publish" -H "$AUTH_OWNER")
PUB2=$(curl -sf -X POST "$BASE/api/arguments/$AID/publish" -H "$AUTH_OWNER")
echo "$PUB1" | jq -c '{id,content_hash,replayed}'
echo "$PUB2" | jq -c '{id,replayed}'
PUBID=$(echo "$PUB1" | jq .id)
echo "$PUB1" | jq -c '.package|{claims:[.claims[].code],evidence:[.evidence[].resource_code],sections:(.manifest.sections|keys)}'

say "13. 命令行校验：导出发布包并离线校验、审计链校验"
python3 -m app.cli export-publication "$PUBID" --out /tmp/acc-package.json >/dev/null
python3 -m app.cli verify-package /tmp/acc-package.json | jq -c '{valid}'
python3 -m app.cli verify-publication "$PUBID" | jq -c '{valid,stored:.stored_hash_matches}'
python3 -m app.cli verify-audit | jq -c '{valid,checked}'

say "14. 撤回发布：历史保留，发布包仍可读"
curl -sf -X POST "$BASE/api/arguments/$AID/retract" -H "$AUTH_OWNER" | jq -c '{status}'
curl -sf "$BASE/api/arguments/$AID/publication" -H "$AUTH_OWNER" | jq -c '{content_hash}'

say "验收完成"
