"""qa-workflow v3 Web 前端（FastAPI + Jinja2 + HTMX）。

设计前提（详见 docs/frontend-implementation-plan.md，用户已拍板）：
- 单实例独占仓库工作树作为唯一真源，无数据库；多人通过浏览器访问。
- 后端三类调用：① 瞬时只读（选单/校验/读产物）= 直接 import scripts/；
  ② 分钟级弱模型批量 = 子进程跑 run_sprint.py；③ 强模型审计 = Agent SDK 异步（可选）。
- 安全：登录鉴权 + 工单级写锁 + 乐观并发 + 操作者留痕；密钥绝不进浏览器。
"""

__all__ = ["config"]
