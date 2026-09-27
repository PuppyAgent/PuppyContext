# B1 基线

- 可执行文件：[`../../migrations/20260926000000_baseline_b1.sql`](../../migrations/20260926000000_baseline_b1.sql)。
- 原始 SQL：[`../../archive/before_b1/migrations/`](../../archive/before_b1/migrations/)，102 个文件原样归档。
- 原始数据任务：`../../archive/before_b1/data_migrations/`，8 个任务整目录原样归档。
- 覆盖版本：`20260306085814` 至 `20260923000000`；75 张 public 表及其函数、索引、触发器、权限和 RLS。
- `manifest.json`：原始文件校验值、基线校验值、归档路径及数据任务兼容情况。
- `verification.json`：临时 Supabase 验证证据，不代表生产部署。
- `required_data.sql`：基线生成用的必要初始化记录，不是第二个部署入口。

基线从空的 Supabase PostgreSQL 17 重放归档 SQL 生成，不含生产/测试用户数据，
也不含 PuppyPay 私有表。存储盘点状态仍是未完成，没有伪造数据任务完成记录。

Supabase 原生读取时间戳 `20260926000000`，不需要认识“B1”这个概念。
新库执行此文件；旧库必须完成[历史衔接](../README.md)，不能直接重跑建表 SQL。

[隔离 Supabase 验证](https://github.com/puppyone-ai/puppyone-cloud/actions/runs/36245147876)
已通过：新建结构与完整历史重放一致，带用户数据升级和 B1 历史切换保留原数据，
缺失历史、字段/权限/RLS 漂移会被拒绝，事务末尾失败会完整回滚，重复执行以及
切换后的下一条增量 SQL 均经过验证。CLI 为 2.107.0，PostgreSQL 为 17.6；
`manifest.json` 中的结构指纹来自此次隔离验证，不从生产库反向生成。

PR #1378 的独立 Supabase Preview（`codex/database-baseline-b1`）也已完成验证：
102 条旧版本记录切换为 B1，原记录完整保存在切换凭证内，用户、项目和数据任务
记录数量未变。这里只改了 PR 的临时预览库；Qubits 和生产库未切换，仍须在
合并前通过受保护的发布流程执行历史衔接。
