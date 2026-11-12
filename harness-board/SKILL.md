---
name: harness-board
description: Opens a local live board in your browser that shows where the current Claude Code session is. Manual only — /harness-board to open, /harness-board stop to stop. 打开本地实时执行看板，显示当前 Claude Code 会话走到哪一步；/harness-board 打开，/harness-board stop 停止。
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/board.py *)
metadata:
  version: "0.2.0"
---
!`python3 ${CLAUDE_SKILL_DIR}/scripts/board.py --session ${CLAUDE_SESSION_ID} $ARGUMENTS`

Relay the output above to the user exactly as it is. Do nothing else.
