# Constraints

## 1. 数据来源仅来自 ccusage CLI
- dashboard 所有数字（token / cost / 模型 / 会话）必须直接来自 ccusage CLI，例如：
  ```bash
  npx ccusage daily --breakdown --since $(date +%Y-%m-%d)
  ```
- 必须使用系统当前解析到的同一个 ccusage（等价于用户执行 `npx ccusage ...`），**禁止**扫描缓存目录自选版本、**禁止**内置或 pin 任何 ccusage 副本。
- **禁止**在本仓库重算、覆盖、修正、折算任何价格或 token；展示层只允许透传与聚合求和。
- 无新增运行时第三方依赖（仅 Python 标准库 + Node 标准库）。