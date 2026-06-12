"""只读 Jira 服务：用「按用户 PAT」的 env 实时拉工单摘要（按需）。

selection.run_selection 已直接用 env 调 select_sprint.plan；本模块给工单页一个
「实时刷新 Jira」动作。所有调用方需用 deps.invoke() 包（jira_fetch 用 SystemExit 报错）。
"""

from __future__ import annotations

from . import scripts_loader


def live_summary(env: dict, key: str) -> str:
    jf = scripts_loader.jira_fetch()
    issue = jf.get_issue(key, env)
    return jf.summarize(issue)
