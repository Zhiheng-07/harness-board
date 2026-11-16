# harness-board

**A live view of what Claude Code or Codex is doing right now.** Type `/harness-board` in a Claude Code session, or `$harness-board` in a Codex session, and a local page opens in your browser. While it works, the page lights up each step as it happens: your prompt coming in, context being assembled, model calls, permission checks and hooks, tool calls, subagents, and the end of the turn.

[中文说明](README.zh-CN.md)

![harness-board while the model is working](docs/images/en-working.png)

> Screenshots in this README come from the page's built-in demo replay (made-up data). The session title is shown exactly as it was typed, so it can be in another language.

## What you see

The page has three parts:

- **Now.** One plain sentence about what Claude Code is doing right now, such as "Model thinking", "Awaiting approval" or "Turn ended", plus six readings: context size and compactions, turn time, model calls, tool calls, permission mode and connection state, and task-list progress when one is in use. In a Codex session, the context reading shows how much is left (such as "98% left") and the mode reading shows the approval policy's raw name (such as `on-request`).
- **The flow.** Sources (your input, plus memory, skills and MCP loaded automatically) → context → model → gates (permission, hooks) → execution (tools, subagents). The path that is active right now has particles moving along it. When tools finish, the flow loops back to the model, and the counter shows how many times this has happened.
- **Record.** Where this turn's time went (model, tools, waiting for you, gaps) and the latest events. Click any card, or **All events**, to open a drawer with the details.

Colors have fixed meanings. **Amber** means Claude Code is waiting for you, for example an approval dialog in the terminal or the desktop app. **Red** means a step did not complete as planned: it failed, was denied, or you pressed Esc. **Gray, frozen** means the connection was lost, Claude Code has exited, or Codex has closed the session.

![Waiting for approval](docs/images/en-approval.png)

The layout rearranges itself for wide, regular, half-screen and narrow windows; it does not just scale down. A button in the top-right corner switches the page between English and Chinese. By default the page follows your browser's language.

<img src="docs/images/en-narrow.png" alt="Narrow window" width="280">

## Requirements

- **macOS**, with either of:
  - **Claude Code** 2.1.69 or later, running **in a terminal** or **in the Claude desktop app** (its Code feature). The record parser has been checked against Claude Code 2.1.258 to 2.1.322.
  - **Codex**: the Codex CLI in a terminal, or Codex in the ChatGPT desktop app. The record parser has been checked against Codex 0.130 to 0.167. The desktop app writes its session records to the same place as the terminal, so the board reads them the same way; it was tested through the desktop app's bundled `codex app-server`, not by clicking through its window.
- Linux may work but is untested. Windows is not supported: the script refuses to run there.
- **`python3`, version 3.9 or later.** Only the standard library is used, so there is nothing to `pip install`. On macOS, `python3` comes with the Command Line Tools: run `xcode-select --install` if it is missing.
- A browser.

It is **not** for the regular chat in the Claude desktop or web apps, and not for Claude Code cloud sessions. The page reads the session records that Claude Code writes on your own machine, and those don't write them there.

## Install

```bash
git clone https://github.com/Zhiheng-07/harness-board.git
mkdir -p ~/.claude/skills
cp -R harness-board/harness-board ~/.claude/skills/
```

Copy the **inner** `harness-board` folder (the one that contains `SKILL.md`). You should end up with `~/.claude/skills/harness-board/SKILL.md`.

- If you set `CLAUDE_CONFIG_DIR`, copy the folder to `"$CLAUDE_CONFIG_DIR/skills/"` instead.
- You can also download the zip from [Releases](https://github.com/Zhiheng-07/harness-board/releases). It unpacks to a `harness-board/` folder; move that folder into `~/.claude/skills/`. Each release lists the zip's SHA-256.

**For Codex**, copy it into `~/.agents/skills/` instead. To use it in both, copy it into `~/.agents/skills/` and give Claude Code a symlink to it:

```bash
mkdir -p ~/.agents/skills
cp -R harness-board/harness-board ~/.agents/skills/
ln -s ~/.agents/skills/harness-board ~/.claude/skills/harness-board   # only if you use both
```

Then start a new Claude Code or Codex session.

## Use

| What | How |
|-|-|
| Open the board | Type `/harness-board` in a Claude Code session; in Codex, type `$harness-board` (or pick it from `/skills`) |
| Stop it | `/harness-board stop`; in Codex, `$harness-board stop` |
| Open it while Claude is busy | Commands typed during a turn only run when the turn ends. To open the board right away, run `python3 ~/.claude/skills/harness-board/scripts/board.py pick` in **another terminal**. It lists the running sessions and lets you pick one (or opens the only one in the current folder). This does not use any tokens. It lists running Codex sessions too; if you only use Codex, use `~/.agents/skills/harness-board/scripts/board.py` as the path. |
| Check whether it is running | `/harness-board status` |
| Print the address instead of opening the browser | add `--no-open` |

You only need to open the board; after that, use Claude Code as usual. Running `/harness-board` again reuses the running board. The board stops by itself when Claude Code exits, or when no page has been open and the session has been quiet for 30 minutes. If you `/clear` or `/resume` into another session in the same terminal, the board follows.

**In Codex:** Codex doesn't run a skill's command directly the way Claude Code does; the model runs it, so this turn uses a few more tokens. The board needs to open a local port and write a cache folder, which Codex's sandbox doesn't allow, so the first run is blocked and the model asks to run it once more outside the sandbox. If approvals come to you, approve it; with auto-review on, auto-review decides. The board stops by itself when Codex closes the session, and follows you if you start a new one with `/new` in the same terminal.

Terminal messages follow your system language: on macOS this is the preferred language in System Settings; on other systems, `LC_ALL`, `LC_MESSAGES` or `LANG`. Set `HARNESS_BOARD_LANG=en` or `zh` to choose.

## How it works

```
Claude Code ──writes──▶ session record (~/.claude/projects/…/<session>.jsonl)
                        process file    (~/.claude/sessions/<pid>.json)
                              │ read-only: new lines every 0.1 s, process state every 0.25 s
                              ▼
            board.py: follow the records → parse → work out the current step
                              │ server-sent events, 127.0.0.1 only, random port + random token
                              ▼
                        your browser
```

Codex sessions work the same way with the files Codex writes: session records in `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (auto-review and subagents each get their own), and lock files in `~/.codex/thread-writer-locks/` (to tell which process owns the session and whether it has closed). If `CODEX_HOME` is set, the board reads from there.

- **Read-only.** It only reads files that Claude Code or Codex already writes. It doesn't change their settings, doesn't read Codex's `auth.json`, doesn't use hooks or plugins, and doesn't proxy API traffic.
- **Local only.** The server listens on 127.0.0.1 on a random port, and every request needs a random token. The page loads nothing from the internet: fonts are embedded and the Content-Security-Policy is `default-src 'none'`.
- **Small.** One Python script and one HTML file, with no dependencies.

## What it cannot see

Claude Code's records don't contain everything, so the page shows what it can and says what it can't. The **What's not visible** tab in the drawer lists the gaps:

1. **Thinking and text while they stream.** They are written when the message ends (seen up to 35 s late).
2. **Tools that finish within about 3 seconds.** They appear only after they end, marked "just now".
3. **The content of approval and question dialogs.** In some versions, even the tool's name is recorded only after you approve.
4. **Why a permission was granted or denied.** This is known only when the tool finishes.
5. **The start of an automatic compaction.** Only its end is recorded. A manual `/compact` is visible when it starts.
6. **Why a request was retried.** Only the retry itself is recorded.
7. **The context window limit.** It is not in the records, so context shows usage only, not a percentage.
8. **The model's internal decisions and what happens on the server.**

**In a Codex session**, the list in the drawer is different:

1. **What it is thinking.** Encrypted; only the number of reasoning blocks is known, not how long.
2. **When a model call starts.** Only the end is recorded; "Model thinking" is inferred from whose turn it is.
3. **Waiting for your approval.** Codex writes nothing while it waits, so the board can only infer it from "this step asked to run outside the sandbox" and the title says "May be waiting for your approval".
4. **Which step inside a script is running.** Codex wraps tool calls in a script; while it runs, the board reads the script text, and exact commands and timings come when it ends.
5. **Hooks running.** Not in the records; the Hook station says "Not recorded by Codex".
6. **Approvals asked for other reasons.** The "ask every time" mode and edits outside the workspace also ask; nothing in the records, so the step just shows as running.
7. **When auto-compaction starts.** Only its completion is recorded.
8. **The model's internal decisions and what happens on the server.**

The page can lag a few seconds behind; it tells you when no new records have arrived for a while. The record formats of Claude Code and Codex are internal and can change in any release. If it changes, the page may show less, and it adds a gray note next to the session info instead of breaking.

## Privacy and security

- Everything stays on your machine. The page shows your prompts, file names, paths and commands, so keep that in mind when you share your screen.
- The board's address contains a token. When the browser opens normally, the address is not printed. With `--no-open`, or if the browser can't be opened, it is printed in the terminal (and so ends up in the session's context).
- The token is also passed as a command-line argument to the background process, so other users on the same machine could see it in the process list.

To report a security problem, see [SECURITY.md](SECURITY.md).

## Known limitations

- When the page loads or reloads, it rebuilds its state from the existing records. In those records, a tool you declined in an approval dialog looks the same as one you interrupted with Esc, so it shows as "interrupted".
- When it starts, it reads the whole session record into memory. A 140 MB record peaks at about 490 MB.
- Install it in your personal skills folder (`~/.claude/skills/`). Other locations are untested, and a path that contains spaces is likely to break the launch command.
- With Claude Code's sandbox turned on, `/harness-board` can't start on its own. See Troubleshooting.
- In Codex, opening the board always needs one run outside the sandbox (approved by you or by auto-review). What you see if you decline that step in Codex has not been tested yet.
- The Codex desktop app's window was not tested directly, only its bundled `codex app-server`. In the desktop app, the board stays on the conversation it was opened from; it does not follow you when you switch conversations in the app.

## Troubleshooting

| Symptom | What to do |
|-|-|
| `/harness-board` replies with `[shell command execution disabled by policy]`, or the script is not allowed to run | Your organization's managed settings may turn off shell commands in skills (`disableSkillShellExecution`) or ignore the skill's pre-approved command (`allowManagedPermissionRulesOnly`). Run `python3 ~/.claude/skills/harness-board/scripts/board.py pick` in a terminal instead. |
| With the sandbox on, `/harness-board` says it has no permission to access `~/.cache/harness-board/…` | Claude Code's sandbox also covers the command in the skill, and it only allows writes inside the working folder. Either run `python3 ~/.claude/skills/harness-board/scripts/board.py pick` in another terminal, or let this one command run outside the sandbox: add it to `sandbox.excludedCommands` in your settings, using the full path, for example `"excludedCommands": ["python3 /Users/you/.claude/skills/harness-board/scripts/board.py *"]`. Commands on that list are not sandboxed, so only do this if you're fine with that. |
| In Codex, `$harness-board` says Codex's sandbox blocked the board | That is the expected first step. The model then asks to run the same command outside the sandbox; approve it. If the model doesn't ask, tell it to rerun the same command outside the sandbox. |
| In Codex, the page says "May be waiting for your approval" but there is no dialog | Codex writes nothing while it waits for approval, so the board infers it from "asked to run outside the sandbox and hasn't started yet". It goes away as soon as the command starts. |
| The page says "Waiting for the first record" | A new session writes its first record when you send its first message. The page picks it up automatically after that. |
| `python3: command not found` | Install the Command Line Tools: `xcode-select --install`. |
| The page seems stuck on "Model thinking" while a dialog is open | The page reads the waiting state from `~/.claude/sessions/<pid>.json`. If that file can't be read, the title says so and the middle and bottom parts turn gray. |

## Uninstall

Run `/harness-board stop` (`$harness-board stop` in Codex) in each session where the board is open, then:

```bash
rm -rf ~/.claude/skills/harness-board ~/.agents/skills/harness-board ~/.cache/harness-board
```

## Repository layout

```
harness-board/        the skill. Copy this folder into ~/.claude/skills/ or ~/.agents/skills/
  SKILL.md            the /harness-board command ($harness-board in Codex)
  agents/openai.yaml  for Codex: display name, and manual invocation only
  scripts/board.py    command line and local server
  scripts/cc_records.py  Claude Code record parser and state machine
  scripts/cx_records.py  Codex record parser and state machine
  scripts/pointer_check.py  packaging self-check used during development; not needed at runtime
  assets/board.html   the page (single file, fonts embedded)
  assets/字体授权/     font licenses ("font licenses")
  LICENSE
tests/                tests with synthetic records: python3 tests/test_cc_records.py; python3 tests/test_cx_records.py; python3 tests/test_board.py
docs/images/          screenshots
```

## License

MIT, see [LICENSE](LICENSE). The fonts embedded in `board.html` (Montserrat, JetBrains Mono, Noto Sans SC) are not covered by the MIT License; they are licensed under the SIL Open Font License 1.1. See `harness-board/assets/字体授权/`.

This is a personal project. It is not made by or affiliated with Anthropic or OpenAI. Claude and Claude Code are trademarks of Anthropic; Codex is a trademark of OpenAI.
