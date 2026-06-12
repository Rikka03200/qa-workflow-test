"""强模型无头审计端口（Claude Agent SDK · 可选）。

把 .claude/workflows/qa-spot-check.js 与 qa-resolve.js 的编排逻辑用 Agent SDK
（Python）重写。SDK 未安装或未配置 ANTHROPIC 端点时整体降级——路由检测
`runner.availability()` 后给出「复制命令贴回 Claude Code」的永久兜底。
"""
