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
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --native-pg -q -m 'not hosting_supabase'
# 上述显式排除真实 Auth/PostgREST；不是完整验收。没有 --live 时这些用例不会执行。

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
| `hosting_supabase` | 真正 GoTrue 创建/登录测试用户并核验 JWT；PostgREST 实测六张表的客户端读取拒绝、backend 只读、DML 拒绝和两项 RPC 的发布/重放/查询 | 无 auth mock，但 repository/receipt 仍是 owner fixture；不代表 admitted 应用服务、真实 S3、用户数据迁移或生产配置 |

**无需先合并到 qubits。** 运行器使用当前工作树的代码和 migrations，记录实际 commit、dirty 状态和 SQL 哈希；隔离测试通过才具备后续集成依据，不反过来依赖集成或部署才能测试。

`--live` 不使用开发数据库或线上数据，也不执行当前工作目录下的 `supabase db reset`。它只启停自己的临时栈。初始化失败会非零退出并记录基础设施错误；默认没有 `--live` 时，PG 用例明确跳过。多数数据库用例中的 OID 是合成值；新 ref 事务另以原生 Git 产生的 SHA-1/SHA-256 同树提交做 CAS/原子性对照。receipt 都由 fixture owner 插入，没有真实 S3 闭包验证。升级用例在所属临时栈内另外创建并清理空数据库，应用真实产品迁移，但 Auth 使用 stub（即使宿主是 Supabase）；不把它算作真实 Auth 升级验收。

现在能检查的关键结果：

- 首次 push 后 clone：文件字节、提交对象、执行权限、符号链接一致。
- 多分支和轻量 tag 能取回，创建分支不改 main。
- 浅克隆/补全历史；本地 squash/rebase/cherry-pick/revert/amend 后托管提交。
- 原 Git 历史经 Web、Agent 写入后仍可读取，老客户端可以继续 fetch 和 push。
- 过期 Web base、只读凭据、非法地址、损坏协议输入被拒绝后，旧数据仍在。
- 多人重叠写入不能丢掉已确认成功的独立文件；存储或发布失败不能破坏旧根。
- 旧 S3 loose 对象可被新 store 读取；新写入保留旧对象；损坏和超时不能伪装成正常空文件。
- 真实 PG 用例验证根 CAS、整笔事务回滚、重复提交事件、旧 RPC/新 RPC 与旧列/新列兼容。
- `integration/test_ref_authority*.py` 验证尚未接入流量的 refs/HEAD SQL 原语：旧 OID/符号目标/不存在状态 CAS、并发创建、多 ref 原子性、结果重放与查询、字节名称、receipt 边界、reflog/audit/outbox 同事务、角色 ACL 和旧写入 fence。
- 新增 Expand 前后对比既有用户/成员/项目/root/history/ref/audit/outbox 行及 RPC ACL；注入末尾 DDL 失败验证全部回滚，再应用原 migration 验证可重试。只验证合成存量 fixture，不代表已完成 07 的真实数据迁移。
- `20261003020000_harden_repository_authority_search_path.sql` 追加修复三项 SECURITY DEFINER 的 `pg_catalog, public, pg_temp` 路径，与既有 ISSUE-053 安全门禁一致，不改原 migration、数据或 ACL；验证带数据重试、失败回滚、函数身份/定义/权限不变。
- `integration/test_supabase_data_api.py` 的 44 项真实 HTTP 用例只连本工具的 loopback 栈，不走 shell 代理或重定向，不使用开发账号；有效 authenticated JWT 也不能读取/调用 backend-only authority。

### 本地 Supabase 启动故障

`docker version` 成功不代表 Docker 能执行容器。2026-10-03 的排查发现，DB 容器持续处于 `created`，PostgreSQL 尚未执行；最小 Alpine `/bin/true` 探针（包括 `--network none`）同样无法启动。该证据指向本机 Docker 启动链路，而不是需要合入 qubits 或放宽产品测试。

运行器现在区分镜像拉取/服务就绪超时与容器未启动：仅检查自建 DB 的安全状态字段，连续 `created` 达 60 秒即失败（通常约 65 秒），整体启动仍有 300 秒上限。诊断写入 `run.json.supabase_startup`，不记录 Env、CLI 密钥、原始日志或 health 输出；中断会回收自身 CLI 子进程，由外层清理所属栈。CLI 和测试子进程均不继承环境中的 Supabase/S3 凭据。`--live` 还要求 pgTAP 执行标记、退出码、非空 PASS 汇总和未报告 skip/TODO，不能只有 pytest 成功就算通过。

需要重启共享 Docker Desktop 时先协调/取得授权；工具不自行重启 daemon、不 prune 镜像或数据卷、不修改其他容器。启动失败保留 NOT_VERIFIED，不改成 native-PG 或替身“验收通过”。

2026-10-03 获用户授权后恢复：普通重启仍阻塞；内部 gRPC 等待与进程继承的代理变量相关，清空本次 Docker 启动进程的代理环境后，Alpine `/bin/true` 约 0.22 秒成功。全局代理设置未改，原有 10 个容器、20 个卷、36 个镜像保留。ECR 的 pg_prove 拉取超时后，运行器与既有数据库 CI 对齐，显式使用官方 Docker Hub registry，不继承任意 registry/云凭据。

真实 pgTAP 曾暴露新函数 search_path 不符合 ISSUE-053（由前向 migration 修复），以及 Python 大量租户污染全局 billing claim 队列。现在先用一个合成 org 让 GC smoke probe 必定运行，再执行全部原 SQL 文件，最后跑 Python；不放宽原 SQL 断言。9 个文件 / 329 项 pgTAP 与定向 147 项测试（含 44 项真实 Auth/PostgREST）已通过；均非全量托管、真实 S3 或部署验收。

额外观察：本机 Git 2.50.1 的 prefix-ref 并发创建会偶发两个请求都失败（100 次独立试验中 6 次，reflog 目录冲突，无新 ref）。原 `test_ref_prefix_create_race_has_one_winner` 断言未放宽，仅增加 stderr 诊断；不能用某次重跑通过宣称该 oracle 已稳定。

已知缺口由测试里的 `hosting_gap` 标记逐项说明。普通模式使用 **strict XFAIL**：能力修好后出现 XPASS，要求移除标记；`--target` 则直接作为失败报告。初始化/清理错误不会被缺口标记隐藏。

目前目标断言会暴露：删除 ref、强推 main、merge commit、附注 tag、blob tag、notes、多 ref 原子推送，以及空提交被确认却未保存。对象层的非 UTF-8 文件名字节往返、Git 字节排序、tag 的 GC 可达性、gitlink 的外部对象边界已有正向回归；还覆盖嵌套 tag→commit/tree/blob 的 native fsck、损坏图禁止 GC、缓存复制中断后重试及浅缓存不能充当完整闭包。以上只证明当前 SHA-1 对象层，不启用尚未实现的 native refs 或 SHA-256 托管。旧运行时 PG 层仍有“文件树相同但 head 不同”的失败目标测试；新 SQL 原语通过同树旧 OID 对照，不等于旧 RPC/transport 已切换。完整目标门禁继续保留这项失败，不能以新增局部测试替换。

原 `tests/conflicts/cases.py` 的 117 条不是 117 条现成测试。本运行器执行其中 **100 条**，采用相同起点、固定发布顺序制造 CAS 重试；真正并发另在 `concurrency/` 和 PG 用例验证。其余 **17 条未算作覆盖**，原因在 `harness/catalog_scope.py::EXCLUDED`：有些依赖旧 scope 所有权模型，有些需要不同入口或尚未搭好的删除/移动竞态。原样本未改。5 条现有样本与实际实现的差异也单独标为 XFAIL，不能据此直接判定是新的 Git 规范要求。

这些测试不是“Git 所有命令、所有参数和所有故障都已穷尽”的承诺。新架构尚未落地，因此未来数据迁移的全量回填、切换、回滚、并发旧新版本共存，仍必须用实际迁移实现再做验收。

CI：`repository-hosting-tests.yml` 在相关 PR 跑普通回归；手动触发可选真实 PG 和严格目标验收。整个 workflow 不需要线上 secrets。
