# Database baselines

```text
supabase/
├── migrations/
│   ├── 20260926000000_baseline_b1.sql  # 唯一可执行 B1
│   └── <timestamp>_<change>.sql       # B1 之后只追加新迁移
├── data_migrations/                 # B1 之后新增的数据任务
├── archive/before_b1/
│   ├── migrations/                  # 102 个旧 SQL，内容不变
│   └── data_migrations/             # 8 个旧数据任务，整目录原样归档
└── baselines/b1/                     # 来源清单、校验值、验证记录
```

Supabase 只自动执行 `migrations/*.sql`。B1 是普通的时间戳迁移，
`baseline_b1` 只是名称。归档供追溯、旧版本升级和回归测试；Supabase 不会自动扫描归档。
不维护第二份 baseline SQL，也不另外维护 current-schema snapshot。

## 数据任务也一起归档

B1 之前的 8 个数据任务全部放进 `archive/before_b1/data_migrations/`，
包括 SQL、Python、manifest、verify、测试文件和原说明。新的任务才放在
`data_migrations/`；该目录里的 README、格式定义和历史校验元数据不是待执行任务。

执行器按任务编号同时查找当前目录和归档目录。搬家不改任务编号、内容和校验值，
因此数据库里已有的执行记录继续有效，已完成的任务不会重跑。
归档不等于完成：旧库尚未做过的转换仍要做；B1 已删除相关旧表的 4 个任务会被
明确拒绝，仍兼容的 4 个任务可以按原编号校验或执行。当前的存储盘点发布指针
继续引用归档任务；外部存储工作仍由人工执行，CI 只读取数据库验证结果。

## 新库

要求已有 Supabase PostgreSQL 17 的 Auth、标准角色及平台基础设施。
运行普通 `supabase start` / `supabase migration up --local`，或使用正式的
`supabase db push` 部署到空的 Supabase 项目。无需逐一运行旧 SQL。

## 已有库：先验证，再切换历史

连接通过 `DATABASE_URL` 环境变量提供，不把密码放进命令参数。

```bash
python3 scripts/database_history.py check
python3 scripts/database_history.py adopt
```

`check` 不提交变化。`adopt` 必须先核对全部 102 个历史版本、业务结构、
函数、索引、触发器、RLS 和权限；不一致就停止。核对通过后，在同一事务中：

1. 把原迁移记录完整保存在 `public.migration_log` 的 `schema_baseline_b1` 记录中。
2. 将覆盖范围内的迁移历史替换为 B1 的版本记录。
3. 保留业务数据和原有数据任务凭证；不执行 B1 的建表 SQL。

正式部署 workflow 在 `db push` 之前调用此步骤。重复运行不会重复改写历史或
新建业务表。Supabase 原生 GitHub 集成不会调用我们的前置步骤；已有分支必须
先通过受保护的发布流程完成 adoption，再重试原生集成，不能强行 include-all。
本 PR 已在隔离 CI 和独立的临时 Supabase Preview 验证；Qubits、生产库未切换。
仓库切换不表示生产库已切换，具体证据见 [B1 验证记录](b1/README.md)。

## 更早版本的库

缺少任何被 B1 覆盖的迁移时，adoption 返回 `BASELINE_UPGRADE_REQUIRED`。
公开仓库自带完整的旧升级链，不需要 PuppyPay 私有代码：

```bash
python3 scripts/database_history.py stage --output /tmp/puppyone-before-b1
```

该命令只生成临时升级目录，不连接数据库。使用此目录与原有的分阶段升级流程
补齐 schema 和必要数据任务，再执行 `check` / `adopt`。历史凭证转换仍需原来的
密钥和验证；不能仅因为结构相似就补盖“已完成”的标记。

## 验证和维护

```bash
python3 scripts/database_baseline.py verify --candidate supabase/baselines/b1
```

验证器在独占临时 Supabase 栈中比较“归档历史重放”和“B1 新建”，并验证带数据
升级、历史切换、拒绝结构/权限漂移及之后的增量迁移。CI 不使用生产凭据。

旧 SQL 和已启用基线保持不可修改；新变更只追加。到稳定版本节点再评估 B2，
不按月自动删文件。Docker 的独立迁移任务与 CI 使用同一套 B1 + 后续迁移，
目标为 Supabase PostgreSQL 17。已有 PG15 数据卷仍需单独做大版本升级，
不能直接换镜像并复用旧数据卷。

每次合并 main，CI 都从空环境执行已提交的 B1 和后续迁移，并运行整套安装测试。
这是“用 baseline 重建并验证数据库”，不是“重新生成或修改 baseline”。
用户应执行所下载版本的完整 `migrations/`，不能永远只执行 B1 而漏掉之后的变更。
详见[安装与升级验证](../../docs/architecture/14-self-hosted-installation.md)。
