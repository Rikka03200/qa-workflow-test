#!/usr/bin/env bash
# scripts/load-config.sh
#
# 可选工具：把 config/config.local.yaml 解析的凭证设为**当前 shell 进程**的环境变量。
# 用法：source ./scripts/load-config.sh

set -e

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

output=$(python "$SCRIPT_DIR/_load_env.py")
status=$?
if [ $status -ne 0 ]; then
    echo "$output" >&2
    return $status 2>/dev/null || exit $status
fi

count=0
while IFS= read -r line; do
    if [[ "$line" =~ ^([A-Z_]+)=(.*)$ ]]; then
        export "${BASH_REMATCH[1]}"="${BASH_REMATCH[2]}"
        count=$((count + 1))
    fi
done <<< "$output"

echo "已加载 $count 个进程级环境变量（关闭终端即清除）："
[ -n "$JIRA_URL" ]              && echo "  ✓ JIRA_URL = $JIRA_URL"
[ -n "$JIRA_PERSONAL_TOKEN" ]   && echo "  ✓ JIRA_PERSONAL_TOKEN = (len=${#JIRA_PERSONAL_TOKEN})"
[ -n "$JIRA_USERNAME" ]         && echo "  ✓ JIRA_USERNAME = $JIRA_USERNAME"
[ -n "$CONFLUENCE_URL" ]        && echo "  ✓ CONFLUENCE_URL = $CONFLUENCE_URL"
[ -n "$CONFLUENCE_PERSONAL_TOKEN" ] && echo "  ✓ CONFLUENCE_PERSONAL_TOKEN = (len=${#CONFLUENCE_PERSONAL_TOKEN})"
[ -n "$READ_ONLY_MODE" ]        && echo "  ✓ READ_ONLY_MODE = $READ_ONLY_MODE"

echo
echo "提示：这些变量仅本 shell 进程有效；Windows 全局环境未被修改。"
