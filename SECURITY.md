# Security policy

harness-board runs a small HTTP server on your own machine (127.0.0.1, random port, random token, read-only endpoints). It reads Claude Code's session records, so any bug that leaks those records to another process or another machine is a security problem.

## Reporting a vulnerability

Please report it privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**. Please don't open a public issue for it.

Include the version (`metadata.version` in `harness-board/SKILL.md`), your OS and Claude Code version, and the steps to reproduce. Don't include real session content; describe its shape instead.

This is a personal project maintained on a best-effort basis. You'll get an acknowledgement, and fixes are released as new versions.

---

# 安全问题

harness-board 会在你本机起一个小的 HTTP 服务（只监听 127.0.0.1，端口和令牌都是随机的，接口都是只读）。它读取 Claude Code 的会话记录，所以任何可能把这些记录泄露给别的进程或别的电脑的问题，都算安全问题。

请通过 GitHub 私下报告：打开本仓库的 **Security** 标签，选择 **Report a vulnerability**，不要开公开的 issue。报告里请写明版本（`harness-board/SKILL.md` 里的 `metadata.version`）、系统和 Claude Code 版本，以及复现步骤。不要附真实会话内容，描述它的格式就够了。这是个人项目，尽力维护。
