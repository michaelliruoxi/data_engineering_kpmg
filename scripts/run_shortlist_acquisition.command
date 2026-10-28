#!/bin/bash
# Run after confirming config/shortlist.json with Emma. Local acquisition only.
set -u
task_repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  printf '未找到 uv，请先按项目文档配置 Python 环境。\n'
  exit 1
fi
if [ -z "${EDGAR_IDENTITY:-}" ]; then
  printf '请输入你自己用于 SEC 请求的姓名和真实联系邮箱（姓名 邮箱）：\n'
  IFS= read -r task_identity
  export EDGAR_IDENTITY="$task_identity"
fi
export PYTHONPATH="$task_repo/src${PYTHONPATH:+:$PYTHONPATH}"
task_output="$task_repo/outputs/shortlist-20261028"
task_command=(uv run --project "$task_repo" --locked python "$task_repo/scripts/shortlist_acquisition.py")
task_scope=(--config "$task_repo/config/shortlist.json" --output "$task_output")
"${task_command[@]}" discover "${task_scope[@]}"
task_result=$?
if [ "$task_result" -ne 0 ]; then
  printf '发现阶段停止，结果已保留。请检查终端错误。\n'
  exit "$task_result"
fi
"${task_command[@]}" download "${task_scope[@]}" --limit 10 --archive-root "$task_repo"
task_result=$?
"${task_command[@]}" inventory "${task_scope[@]}" --archive-root "$task_repo" --archive-root "$task_output"
task_inventory_result=$?
printf '结果保存在：%s\n' "$task_output"
printf '退出码 2 表示还有待下载或待审核项，请查看 download_status.json。下载完成不代表共享库已入库。\n'
if [ "$task_result" -ne 0 ]; then
  exit "$task_result"
fi
exit "$task_inventory_result"
