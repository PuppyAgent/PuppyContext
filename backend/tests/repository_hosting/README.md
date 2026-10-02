# 仓库托管测试

ISSUE-062 的隔离实施从 `qubits@dc273f48` 开始；初始测试复制自原工作区已有、未提交的测试底座，本分支核验并继续修改，原文件未被覆盖。此目录不是 M01–M20 已全部交付的声明。

这里是可执行测试，不是迁移方案。测试直接调用现有 Version Engine；Git HTTP 测试会启动本机服务，再用系统 Git 实际 clone / push / fetch。

从项目根目录运行（先在 `backend` 执行 `uv sync --locked`）：

```bash
# 日常回归：已知缺口显示 XFAIL，新错误导致失败
backend/.venv/bin/python scripts/testing/run_repository_hosting.py -q

# 完整目标验收：已知缺口也必须通过；当前会失败
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --target -q

# 加真实数据库：需要 Docker、Supabase CLI、psql
# 创建独立临时栈、应用仓库真实 migrations、跑 Python + pgTAP、清理临时栈
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --live -q

# 补充：独立本机 PostgreSQL 17，auth schema 为 stub，没有 Supabase/S3 服务
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --native-pg -q

# 迁移前的严格目标入口（当前测试范围尚不完整，不能独自批准迁移）
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --live --target -q
```

结果写到 `backend/.hosting-test-results/junit.xml` 和 `run.json`。后者记录 commit、工作区是否 dirty、Git/Python/数据库环境、schema 校验值、选择参数与时间，并按执行层统计通过、失败、已知缺口和跳过，不把 XFAIL / SKIP 计为支持。严格模式遇到跳过、xfail、缺失/空/损坏 JUnit 或服务初始化失败必须非零退出。`--output` 可以另指定目录；后面的 `-k`、`-m` 等参数传给 pytest。Git HTTP 测试需要允许监听本机端口。

```text
tests/repository_hosting/
├── contracts/     地址格式、身份校验、HTTP 入口
├── unit/          Git 对象字节、哈希、树、父提交、GC 可达性
├── workspace/     原生 Git 的冲突、abort/continue、reset、stash、worktree
├── transport/     原生 Git → HTTP → 托管内核 → 再 clone 检查
├── integration/   Web API；独立真实 PG 的事务与旧客户端兼容
├── conflicts/     冲突样本、策略选择、人工解决
├── concurrency/   原生 ref 事务；实际重叠的引擎写入
├── recovery/      对象/发布故障、丢失应答、旧 S3 对象读取
└── harness/       Git 驱动、冲突运行器、隔离 PG 辅助工具
```

| 测试层 | 实际验证什么 | 不代表什么 |
|---|---|---|
| `hosting_native` | 系统 Git 的工作区、bare ref 事务、SHA-1/SHA-256、mirror/bundle 行为 | 不代表 Cloud 已经支持 |
| `hosting_component` | 生产对象/写入/路由代码；真实 Git HTTP；控制面用内存替身，S3 用 moto | 不代表真实 PG/S3 联合事务、生产耐久性已验收 |
| `hosting_live` | 临时 PostgreSQL 的真实产品迁移、RPC、并发 CAS、回滚、旧新命名兼容；`--live` 使用 Supabase，`--native-pg` 使用 auth stub，run.json 区分环境 | native PG 不代表 Supabase Auth/PostgREST/安装器验收；两者都不代表真实 S3 或未来迁移已安全 |

`--live` 不使用开发数据库或线上数据，也不执行当前工作目录下的 `supabase db reset`。它只启停自己的临时栈。初始化失败会非零退出并记录基础设施错误；默认没有 `--live` 时，PG 用例明确跳过。数据库用例中的 OID 是合成值，验证的是事务控制面；完整对象图由其他层验证。

现在能检查的关键结果：

- 首次 push 后 clone：文件字节、提交对象、执行权限、符号链接一致。
- 多分支和轻量 tag 能取回，创建分支不改 main。
- 浅克隆/补全历史；本地 squash/rebase/cherry-pick/revert/amend 后托管提交。
- 原 Git 历史经 Web、Agent 写入后仍可读取，老客户端可以继续 fetch 和 push。
- 过期 Web base、只读凭据、非法地址、损坏协议输入被拒绝后，旧数据仍在。
- 多人重叠写入不能丢掉已确认成功的独立文件；存储或发布失败不能破坏旧根。
- 旧 S3 loose 对象可被新 store 读取；新写入保留旧对象；损坏和超时不能伪装成正常空文件。
- 真实 PG 用例验证根 CAS、整笔事务回滚、重复提交事件、旧 RPC/新 RPC 与旧列/新列兼容。

已知缺口由测试里的 `hosting_gap` 标记逐项说明。普通模式使用 **strict XFAIL**：能力修好后出现 XPASS，要求移除标记；`--target` 则直接作为失败报告。初始化/清理错误不会被缺口标记隐藏。

目前目标断言会暴露：删除 ref、强推 main、merge commit、附注 tag、blob tag、notes、多 ref 原子推送，以及空提交被确认却未保存。对象层的非 UTF-8 文件名字节往返、Git 字节排序、tag 的 GC 可达性、gitlink 的外部对象边界已有正向回归；还覆盖嵌套 tag→commit/tree/blob 的 native fsck、损坏图禁止 GC、缓存复制中断后重试及浅缓存不能充当完整闭包。以上只证明当前 SHA-1 对象层，不启用尚未实现的 native refs 或 SHA-256 托管。PG 层仍有“文件树相同但 head 不同”的 CAS 目标测试。

原 `tests/conflicts/cases.py` 的 117 条不是 117 条现成测试。本运行器执行其中 **100 条**，采用相同起点、固定发布顺序制造 CAS 重试；真正并发另在 `concurrency/` 和 PG 用例验证。其余 **17 条未算作覆盖**，原因在 `harness/catalog_scope.py::EXCLUDED`：有些依赖旧 scope 所有权模型，有些需要不同入口或尚未搭好的删除/移动竞态。原样本未改。5 条现有样本与实际实现的差异也单独标为 XFAIL，不能据此直接判定是新的 Git 规范要求。

这些测试不是“Git 所有命令、所有参数和所有故障都已穷尽”的承诺。新架构尚未落地，因此未来数据迁移的全量回填、切换、回滚、并发旧新版本共存，仍必须用实际迁移实现再做验收。

CI：`repository-hosting-tests.yml` 在相关 PR 跑普通回归；手动触发可选真实 PG 和严格目标验收。整个 workflow 不需要线上 secrets。
