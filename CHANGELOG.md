# Changelog

[中文在下方](#更新记录)

## 0.3.0 — 2026-11-15

- **Claude desktop app (#3).** harness-board now works for Claude Code in the Claude desktop app (its Code feature) on macOS. Sessions there never write the record that marks the end of a turn, so the board used to stay on "Model thinking" and then count seconds without new records. A turn now ends when the Stop hooks have run, when Claude Code goes idle, or when the next turn starts. Headless sessions (`claude -p` and other SDK hosts) are fixed the same way.
- **Where to go.** While Claude Code waits for you, the page says where: "in the terminal" for terminal sessions, "in the desktop app" for desktop sessions, and no place when the source is unknown. When the banner is too narrow for the full text, it shows the short version and the line below still names the place.
- **Fixes.** In desktop and headless sessions the model card now lights up when a request is sent, not when the reply arrives. Input from SDK hosts that wraps its text in custom tags now counts as your input. In terminal sessions, "Dialog open in terminal" no longer runs off the edge of a 320-pixel-wide window.

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

### 0.3.0（2026-11-15）

- **Claude 桌面应用（#3）：** 支持 macOS 上 Claude 桌面应用里的 Claude Code（Code 功能）。那里的会话每一轮都不写标记回合结束的记录，看板以前会停在「模型在想」，然后一直读秒。现在 Stop hook 跑完、Claude Code 空闲、或下一轮开始时就算这一轮结束。无头会话（`claude -p` 和其他 SDK 宿主）一起修好了。
- **去哪里处理：** 等你的时候，页面写明在哪里：终端会话写「终端」，桌面会话写「桌面应用」，认不出来源时不提地点。横幅放不下完整说法时换成短说法，地点由下面那行说明交代。
- **修正：** 桌面和无头会话里，模型卡在请求发出时就亮，不再等回复到了才亮；SDK 宿主用自定义标签包着的输入也算你的输入；终端会话的英文「Dialog open in terminal」在 320 像素宽的窗口里不再超出画面。

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
