# Constraints

这是本仓库的硬约束。违反任一条即视为 regression，必须先修复再交付。

## 1. 数据源唯一：只读 ccusage CLI
- 所有 token / cost / 模型 / 会话数字**必须**来自 ccusage CLI 的 JSON 输出。
- **禁止**在本仓库重算、覆盖、修正、折算任何价格或 token（不得内置 pricing table、不得做 cost override、不得乘除任何系数）。
- 展示层只允许透传与聚合求和，不得"修正"上游数字。
- 无新增运行时第三方依赖（仅 Python 标准库 + Node 标准库）。

## 2. 版本：必须跟随系统当前 ccusage
- 必须使用与 `npx ccusage` 相同的版本，即**可用的最新版本**。
- **禁止**引用本仓库内 bundled/pinned 的 `node_modules/ccusage`。
- **禁止**在 npx/bun 缓存中挑选任意版本；必须显式解析真实版本号并取最高。
- 版本来自 `package.json` 的 `version` 字段，**不得**从目录路径字符串推断。
- 新增/修改解析逻辑后，必须实测"解析结果 == `npx ccusage --version`"。

## 3. 验收
- 任何影响 ccusage 调用路径的改动，必须附上：解析到的版本、`npx ccusage --version`、两者一致。
- 金额改动必须给出"改前/改后"对照，并说明差异仅来自 ccusage 版本，而非本仓库计算。