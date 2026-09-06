#!/bin/bash
# =============================================================================
# OrangeServer 健康检查脚本
# =============================================================================
# 用法：
#   ./ops/healthcheck.sh                                # 物理机: http://127.0.0.1:28000/local/health
#   OGS_PORT=8080 ./ops/healthcheck.sh                # Compose: http://127.0.0.1:8080/local/health
#   OGS_HOST=10.0.1.5 ./ops/healthcheck.sh
#   OGS_USE_HTTPS=1 ./ops/healthcheck.sh                # HTTPS 部署
#   OGS_INSECURE=1 ./ops/healthcheck.sh                  # HTTPS 跳过证书校验 (自签证书)
#
# ⓘ Compose 部署时后端仅 expose (不发布宿主机端口), 经前端 nginx 访问:
#   OGS_PORT=8080 ./ops/healthcheck.sh   # 或 make docker-health
# =============================================================================

set -e

OGS_HOST="${OGS_HOST:-127.0.0.1}"
OGS_PORT="${OGS_PORT:-28000}"
OGS_USE_HTTPS="${OGS_USE_HTTPS:-0}"
OGS_INSECURE="${OGS_INSECURE:-0}"

if [ "$OGS_USE_HTTPS" = "1" ]; then
    SCHEME="https"
    CURL_K=""
    [ "$OGS_INSECURE" = "1" ] && CURL_K="-k"
else
    SCHEME="http"
    CURL_K=""
fi
URL="${SCHEME}://${OGS_HOST}:${OGS_PORT}/local/health"

echo ">>> 健康检查 ${URL}"
HTTP_CODE=$(curl ${CURL_K} -s -o /tmp/ogs_health.json -w "%{http_code}" --max-time 5 "${URL}" || echo "000")

if [ "${HTTP_CODE}" = "200" ]; then
    BODY=$(cat /tmp/ogs_health.json)
    echo "[OK] HTTP ${HTTP_CODE}  body=${BODY}"
    # /local/health 有两个文档化的健康状态 (见 DEPLOY.md)，HTTP 均为 200:
    #   初始化向导阶段  {"status":"setup","setup_required":true}
    #   初始化完成后    {"status":"ok", ...}
    # 两者都说明服务活着，都不能判失败；否则全新安装跑 make health 会误报。
    if echo "${BODY}" | grep -qE '"status": ?"ok"'; then
        echo "[OK] status=ok"
        exit 0
    elif echo "${BODY}" | grep -qE '"status": ?"setup"'; then
        echo "[OK] status=setup（服务正常，初始化向导尚未完成）"
        exit 0
    else
        echo "[FAIL] 响应缺少可识别的 status（既不是 ok 也不是 setup）"
        exit 1
    fi
else
    echo "[FAIL] HTTP ${HTTP_CODE}（服务不可达）"
    exit 1
fi
