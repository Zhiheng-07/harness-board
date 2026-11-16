---
name: harness-board
description: Opens a local live board in your browser that shows where the current Claude Code or Codex session is. Manual only — /harness-board in Claude Code or $harness-board in Codex to open, add stop to stop. 打开本地实时执行看板，显示当前 Claude Code 或 Codex 会话走到哪一步；Claude Code 里 /harness-board、Codex 里 $harness-board 打开，后面加 stop 停止。
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/board.py *)
metadata:
  version: "0.4.0"
---
!`python3 ${CLAUDE_SKILL_DIR}/scripts/board.py --session=${CLAUDE_SESSION_ID} $ARGUMENTS`

Relay the output above to the user exactly as it is. Do nothing else.

In Codex only: if the output says Codex's sandbox blocked the board, run the same command once more with escalated sandbox permissions, then relay that output the same way.
