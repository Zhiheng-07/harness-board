# Changelog

[中文在下方](#更新记录)

## 0.2.1 — 2026-11-15

- **Fix (#1).** When the board window is narrow (for example shrunk to the browser's minimum width), the status text in each readings cell (e.g. "Ended", "Connected") is now right-aligned to the cell's edge instead of leaving a large gap. Other window sizes are unchanged.

## 0.2.0 — 2026-11-12

- **Platform check.** The script runs only on macOS and Linux (Linux is untested) and exits right away elsewhere. On Windows its process check would have interrupted running Claude Code sessions.
- **English and Chinese.** The page follows your browser's language by default and has a button to switch. Terminal messages follow the system language (on macOS, the preferred language in System Settings).
- **License.** The package now ships a `LICENSE`: MIT, except the embedded fonts, which stay under the OFL.
- **Sandbox.** With Claude Code's sandbox on, `/harness-board` cannot start. It now prints one line with the reason and how to fix it, instead of a stack trace.
- **Fixes.** The browser tab title is now `harness-board`. Headless sessions whose input starts with a slash command are no longer misread.

## 0.1.0 — 2026-11-12

First release. Type `/harness-board` in a Claude Code session and a local page opens in your browser, showing live what the session is doing: your prompt, context, model calls, permission checks and hooks, tool calls, subagents, and the end of the turn. It only reads the session records and changes no configuration. macOS and Claude Code in a terminal only.

---

## 更新记录

### 0.2.1（2026-11-15）

- **修正（#1）：** 窗口较窄时（比如浏览器缩到最窄），读数区每格右侧的状态字（「已结束」「已连接」等）改成贴右边框，不再空出一大段。其他窗口宽度不变。

### 0.2.0（2026-11-12）

- **平台检查：** 只在 macOS 和 Linux 上运行（Linux 未实测），其他系统直接退出。在 Windows 上，它检查进程的方式会打断正在运行的 Claude Code。
- **中英文：** 页面默认跟浏览器语言，可以用按钮切换；终端提示跟系统语言（macOS 上看系统设置里的首选语言）。
- **授权：** 包里带 `LICENSE`：MIT，内嵌字体除外，字体仍按 OFL。
- **沙箱：** 开着 Claude Code 沙箱时 `/harness-board` 启动不了，现在只给一句话说明原因和办法，不再甩出整段报错。
- **修正：** 浏览器标签标题改成 `harness-board`；无头会话里以斜杠命令开头的输入不再被误判。

### 0.1.0（2026-11-12）

第一版。在 Claude Code 会话里输入 `/harness-board`，浏览器里会打开一个本地页面，实时显示这个会话在做什么：你的输入、上下文、模型调用、权限检查和 hook、工具调用、子 agent，以及回合结束。只读会话记录，不改任何配置。只支持 macOS 和在终端里运行的 Claude Code。
