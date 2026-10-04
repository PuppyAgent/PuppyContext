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
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --native-pg -q -m 'not hosting_supabase and not hosting_s3'
# 上述显式排除真实 Auth/PostgREST/S3；不是完整验收。

# 本地 Linux Docker：Python、生产适配器、stock Git 也在隔离容器执行
# 不需要合入 qubits，不触发 CI/CD；首次构建从 uv.lock 安装 Linux 依赖
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --docker --live --s3 --target -q

# 宿主机客户端 + Docker 内实服务（不是整个运行时都在 Docker）
# 当前测试范围尚不完整，以下任一入口都不能单独批准迁移
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --live --s3 --target -q
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
| `hosting_component` | 生产对象/写入/路由代码；真实 Git HTTP；控制面用内存替身，对象用磁盘或 moto | 不代表真实 PG/S3 联合事务、生产耐久性已验收 |
| `hosting_live` | 临时 PostgreSQL 的真实产品迁移、RPC、并发 CAS、回滚、旧新命名兼容；`--live` 使用 Supabase，`--native-pg` 使用 auth stub，run.json 区分环境 | native PG 不代表 Supabase Auth/PostgREST/安装器验收；两者都不代表真实 S3 或未来迁移已安全 |
| `hosting_supabase` | 真正 GoTrue 创建/登录测试用户并核验 JWT；PostgREST 实测六张表的客户端读取拒绝、backend 只读、DML 拒绝和两项 RPC 的发布/重放/查询 | 无 auth mock，但 repository/receipt 仍是 owner fixture；不代表 admitted 应用服务、真实 S3、用户数据迁移或生产配置 |

`hosting_s3` 是独立实服务层：生产 Native Git adapter / RefTransactionService / S3 backend → 所属 Supabase Storage 的 S3-compatible API → 真实 PostgREST/PG。`--live --s3` 才启用；要求精确 owned loopback endpoint/bucket，缺少通过的 S3 用例不能报该层成功。不是 MinIO、AWS 生产或真实用户授权证据。

**无需先合并到 qubits。** 运行器使用当前工作树的代码和 migrations，记录实际 commit、dirty 状态和 SQL 哈希；隔离测试通过才具备后续集成依据，不反过来依赖集成或部署才能测试。

`--live` 不使用开发数据库或线上数据，也不执行当前工作目录下的 `supabase db reset`。它只启停自己的临时栈。初始化失败会非零退出并记录基础设施错误；默认没有 `--live` 时，PG 用例明确跳过。多数数据库用例中的 OID 是合成值；新 ref 事务另以原生 Git 产生的 SHA-1/SHA-256 同树提交做 CAS/原子性对照。原 SQL authority 用例的 receipt 由 fixture owner 插入，不构成 S3 闭包证明；新增 native publication/S3 用例改由生产 verifier + pin/seal RPC 产生 receipt。升级用例在所属临时栈内另外创建并清理空数据库，应用真实产品迁移，但 Auth 使用 stub（即使宿主是 Supabase）；不把它算作真实 Auth 升级验收。

### 本地 Docker 运行时与环境边界

`--docker` 要求 `--live --s3`，只连接本次创建的 Supabase network。
依赖镜像仅复制 pyproject/uv.lock，不复制源码、宿主机虚拟环境或密钥。
运行时源码只读挂载，拒绝含 `.env` 的工作树；不挂载 Docker socket，
不继承宿主机业务凭据、Git 配置或代理。容器具有 4 GiB 内存、4 CPU、
1024 PID、2 GiB 临时盘的测试边界；这些不是生产大仓库能力保证。

容器的 loopback TCP 转发仅指向本次 owned DB/Kong，并保持原始 API/S3
origin：改用另一个容器内端口会与 Kong 提供给 Storage 的 SigV4 origin
不一致，已实际复现六项 S3 签名失败。保留 origin 后首轮 69 项通过，
另有 9 个文件 / 329 项 pgTAP；不是 canonical 业务链路或全量目标通过。

`container-environment.json` 只记录软件版本、环境变量名称、真实生产
Supabase client 启动检查和认证未绕过的事实，不记录密钥值。`run.json`
区分 Linux 执行版本与宿主机编排版本；缺少容器证据、SQL 证据、要求的
S3 层或出现失败/skip/XFAIL 均不能获得严格验收。只清理本次 owned 容器，
不重启 Docker、不 prune、不操作其他已运行的应用栈。

后续冻结版本全量首次执行得到 964 passed / 43 failed；比宿主机多出的
8 项来自 Docker tmpfs 默认 `noexec` 导致拒绝 hook 未执行，另 1 项是 Git
2.39.5 在 clone 原生空 SHA-256 bare 仓库时同样错误建立 SHA-1 仓库。
现在显式启用**测试临时盘** exec 并启动时探测，保持原拒绝断言；镜像改用
校验官方 tarball SHA-256 后编译的 stock Git 2.50.1。不是修复旧 Git，也
不宣称所有客户端版本通过；未把这九项失败重标为 skip/XFAIL。

`hosting_application` 是新增独立层：Docker 内真实 `src.main` 进程、
本次 owned Redis、GoTrue 密码登录所得 JWT、实际 Git credential 签发，
随后原生 Git push → API 读写 → 拒绝过期 base/未授权用户 → 进程重启与
cache 清除 → 冷 Git fetch/fsck → 撤销 credential。没有依赖替身，也没有
把 secret 放进 remote URL。应用日志使用可写测试目录，而源码保持只读。
Docker 严格验收还要求这一层确实执行通过。

该应用用例覆盖现有 SHA-1 compatibility profile；Native 全功能正式入口、
quota、异步 worker、长 I/O 与崩溃恢复仍须各自验收。此本地 profile 明确
关闭可选外部 AI、Billing、ETL 和 Scheduler，不把未配置的外部服务算作
已验证环境。Git/应用/环境修复选择：13 passed plus 329 pgTAP，非全量。

`4e9887e4` 冻结全量再次真实失败：855 passed / 155 failed；应用、Auth、
SQL、S3 层通过，但后段出现 `fork failed: Resource temporarily unavailable`。
不将其记成验收通过或直接推定为 Git 缺陷。补充 `--init` 子进程回收与
cgroup v2 PID/内存计数、峰值进程/线程诊断（不记录 argv/凭据）；保持
1024 PID、4 GiB 内存上限不变。缺失资源证据、PID/内存耗尽均使严格验收
失败。短选择通过不能排除长时资源累积。

后续 clean `e6271b9e` 全量：**985 passed / 34 failed**，9 个 pgTAP 文件 /
329 tests 全通过；失败名称与 `61cc117e` 的原始 34 项完全一致。各层：
原生 Git 18、组件 595/34、PostgreSQL 176、Auth/PostgREST 62、S3 133、
真实应用 1。PID/内存耗尽计数均为零，观察 PID 峰值 40、内存峰值
707457024 bytes。控制实验 40 次 stock Git `maintenance --auto --detach`
在无 init 时留下 40 个 zombie，有 init 时为零。配置/进程回收问题已修复；
这不是 34 项产品能力、Native canonical 路由或 ISSUE-062 完成的证明。

原生技术容量新增显式 Org/Project unique-object body bytes / object-count
预留与账本：PUT 前拦截，整闭包计量后封存，SQL 拒绝旧 issuer 绕过。
这不改变客户 `storage.logical_bytes` 计费语义。GC 不因 producer 到期或
被杀便返还额度；重复对象上传也保留独立未结 I/O claim。真实 S3 用例
验证非默认 tag 配额拒绝零 PUT、旧 ACK 冷读、已知 I/O 静止后的删除/
SQL 故障恢复及无物理对象的预留回收。新增容量选择 98 passed + 329
pgTAP（非 clean 全量），包括真实 Auth 客户端拒绝和 populated rollback。
后续复核复现同 pin 误结算两个失败；现改为每次存储调用独立 I/O ID，
pin 封存/释放不代替结算。原生 PUT/DELETE 使用隔离 single-attempt client，
不改共享 retry/proxy 设置、不隐式 multipart，也不把模糊 HEAD 失败当不存在。
真实 S3 丢失 PUT/DELETE ACK 后不自动重试，并保留额度/GC fence；恢复只基于
测试中实际观察到的 I/O 完成。新 broad 选择 295 passed +329 pgTAP；客户端
切换/最后 I/O 选择 18 passed +329 pgTAP，仍非 clean 全量。
Canonical Git/API、计费结算、生命周期退役及独立进程恢复仍未交付。

clean `1d54c3d1` Docker 全量为 **1086 passed / 35 failed / 1 teardown error**，
pgTAP 329 通过。原始 34 项仍在，另有 `workspace-stash` 真实 S3 冷 mirror
30 秒超时及 HTTP 服务未退出；资源耗尽计数为零，内存峰值 692703232 bytes。
同 revision 的隔离复测 4 passed，但这不是修复或全量通过。Git 超时现在追加
worker 线程栈（不含 argv/locals），仍保留原 30 秒预算和失败。补充 backend
回归 2773 passed / 27 skipped / 76 deselected，也不替代 strict Docker 验收。
后续干净冻结 `8defc2cb` 全序复验 **1089 passed / 原 34 failed**、pgTAP 329：
额外超时未复现，但诊断代码不算修复。原生 18、组件 625/34、PG 214、Auth 86、
S3 145、真实应用 1；PID 峰值 47、内存峰值 703975424 bytes，耗尽计数为零。

逻辑计费正在单独接入：原生发布的可选 checked wrapper 将实际默认 ref 的
before/after OID、`storage.logical_bytes`、当前 entitlement revision、actor/lease、
ref/audit/outbox 放进同一事务。失败不改 refs 或账单，rewind 按新事务计量，
原结果只读重放不重复计费。一次真实 PG red case 复现旧全量对账器把 native 用量
归零，现阻止该旧路径覆盖；native-aware 对账/生命周期结算仍待实现。
定向 Docker **43 passed + 329 pgTAP**（PG 17、Auth 8、S3 2、组件 15、legacy
应用 1），包含 populated Expand rollback、等待过期、同 Org 竞争及 S3 冷读/恢复。
随后组合 admission/capacity/snapshot/transaction 回归 **161 passed + 329 pgTAP**，
无 skip/error/资源耗尽（PID 峰值 22、内存峰值 413999104 bytes）；组件集 337 通过。
这不是完整 file/ref policy、真实 Native HTTP 或外部 Billing 集成验收；原 34
项失败、完整 canonical/消费者/迁移门禁仍在。

`defff62e` 冻结全量：**1131 passed / 原 34 failed + 329 pgTAP**，无新失败或
资源耗尽；补充 backend 2773 passed / 27 skipped / 76 deselected（不是严格验收）。
后续 checked 对账已接 application scheduler：完整 SQL inventory、200-row pages、
native 当前树物理读（不读 commit history）、legacy placement 兼容、最终双向
inventory CAS、等待后过期回滚和原结果重放。失败取消只释放测量元数据，不代表
存储 I/O quiescence。单 Org 未结束 inventory 背压及 bounded cleanup 防止重试膨胀。
定向 owned Docker **82 passed + 329 pgTAP**，包括混合 SHA-1 legacy / SHA-256
native 冷测量、lost ACK、202-Project 分页和 populated migration rollback。
早期 receipts 保留：SQL local-variable 错误、到期拒绝原因/跨 fixture cleanup 断言、
psql boolean JSON 解码夹具错误，以及选中过 Auth 的 native-PG run 的 16 skips；
修正后 strict native-PG 10 passed，再运行上述 82-case 实服 selection 全绿。

后续 file admission 使用新的空 Expand 和 optional checked publisher：新 blob
单文件限制在 PUT 前拒绝，已发布历史可 grandfather，但 rejected receipt / allocation
不构成发布证明；当前树 rename 保留 multiplicity，额外超限 copy 拒绝。每次 I/O
claim 检查当前 actor/lease/entitlement；uploading pin 不能借用另一重试的新 lease。
Admitted metadata advertisement / `ls-refs` 在 SQL 等待后重新检查 credential，仍为
零 object I/O。无 canonical routing/activation，Scope/consumer/recovery/migration 未关闭。

File-policy 首轮实服 94 passed / 4 failed：两条零 delta event 计数夹具断言错误，
以及两种格式真实的 sealed-retry 回归（未发布 root 错送入 read snapshot）。修复通过
原 publication pin 重新验证 incoming graph，不扩大 reader authority；后续实服
**98 passed + 329 pgTAP**。再后 broad run **267 passed / 2 failed + 329 pgTAP**
暴露对账测试错误复用 admitted user control；改为 application scheduler 同一个
backend-only factory，未给 user reader 增加特权。该 broad run 期间有源码修改，
不是 frozen acceptance。metadata guard 最终 strict native-PG **47 passed**，组件
及 storage billing **365 passed**，不把以上 selection 合并计数。

随后清洁冻结 `b652330b` 全量 **1220 passed / 原 34 failed + 329 pgTAP**；相对
`defff62e` 失败集合无增减，无 skip/error/资源耗尽（PID 峰值 46、内存峰值
716460032 bytes）。补充 backend **2773 passed / 27 skipped**，2800 JUnit cases，
无 failure/error；不是严格 hosting 验收。此结果不适用于后续 dirty 初始化修复。

初始化修复先保留三条 red：已有 ACK 在 storage probe 失败时被置为 empty tree，
缺 checked capability 仍调用旧 setter。新的空 Expand/checked RPC 在锁内识别真正
未初始化的 legacy SHA-1 Project；已有合法 root 不探测对象、不改写；缺 root 但有
accepted history/Scope state/refs 属于损坏，不是空库。首初始化要求有效生命周期和
等待后仍有效的 Project lease；native authority 拒绝走旧初始化。真实 PostgREST、
anon/JWT denial、populated rollback/retry、publication race/queued expiry 的定向
Docker **24 passed + 329 pgTAP**（Auth 3、PG 8、legacy 应用 1、S3 2、组件 10）。
该 selection 不证明 native 生命周期完成，也不自动恢复物理缺失的 ACK。后续损坏
root/删除生命周期回归 strict native-PG **23 passed**，组件/billing/deep scenarios
**412 passed**；先前误选 Auth 的 PG receipt 含 3 skips，保留并按 strict 失败记录。

### Canonical native Git 源码接入（仅合成 enrollment）

正式 Git router 现按 fresh PG authority 选择实现；native 强制 admitted control +
capacity + logical billing + file policy，无缺能力 fallback。旧 cached root 不得
作为 native 当前状态。既有项目不自动 enroll/activate；未映射 legacy locator / Scope
不能扩大为 full repo。Product/Scope/自动写入及生命周期/消费者/迁移仍未完成。

新增实际 `src.main` 测试：先 JWT 创建 Project/credential，测试 owner 显式设置空
native authority 和 synthetic entitlement，不伪造 grant/物理 receipt。SHA-1/SHA-256
验证非 main HEAD、多 ref/typed tag atomic push、单文件限制拒绝、冷进程重启、v2
fetch/fsck、精确 refs/bytes，以及只读/匿名/跨 Project/撤销 credential 拒绝。首轮
**16 passed + 329 pgTAP**，加强后 **41 passed + 329 pgTAP**（application 3、S3 2、
Auth 3、PG 13、组件 20），无 skip/error/gap/资源耗尽；不是外部 PuppyPay 或 native
产品 Save/API 验收。组件/router 回归 **503 passed**。补充 backend 首轮 2772 passed /
1 failed / 27 skipped：mixed-protocol MagicMock 没声明 legacy selection；保持原断言，
补齐夹具明确 legacy authority，不给生产 lookup 添加 fallback。随后 mixed-write /
admission / selector 定向 **21 passed**，原并发、恢复和延迟断言未改；未冒充全量重跑。

干净冻结 `a8dd06c7` 随后全序 **1246 passed / 52 failed + 329 pgTAP**：原 34 项
保留，另有 18 项 native GC 消费者回归。旧 facade 的 authority guard 正确拒绝 native，
但 GC worker/测试仍使用该当前树入口，暴露维护入口未适配；不是全量通过。补充
backend **2773 passed / 27 skipped / 76 deselected**，不抵消 hosting 失败。

### 当前准入续 pin 与维护专用 GC inventory

续 pin 先保留 **4 failed / 1 passed**：撤销 credential、过期 publication lease 或排队期间
到期仍可延长 pin。新 backend-only RPC 在 primitive renewal 前后检查当前 actor，
publication 另检查绑定 lease；read pin 不需要写权限/写 lease。失败回滚延长，无旧 RPC
fallback；独立 backend maintenance primitive 保留。Expand 不改数据、不登记、不解除
任何 uncertain I/O claim。严格 native-PG/组件 **13 passed**，覆盖 populated rollback/retry。

`get_gc_repo` 是维护专用 inventory，不暴露旧 current-tree/head 或 publication facade；
实际 scheduled GC 已选择它。全部旧 history/Scope heads/refs/view-index/outbox/conflict/
shadow roots 仍是保守 retention 输入，不冒充 native 当前状态。metadata 故障不得降级，
缺完整 scope inventory 不得当空。原 18 项的拒绝、物理 bytes/refs、fence/恢复断言不改，
改用同一个生产维护入口；另有两种格式实际 worker/PG run-record 回归。

工作树 connected 选择先 **77+329**，扩大为 **181+329**，包含原 18 项全部转绿；最终
inventory 接口收窄后 **103+329**（无 skips/errors/gaps/耗尽），组件/GC/system **432 passed**。
这些是选择证据，不替代冻结完整验收，也不修复原 34 项或完成其它消费者/迁移。

### Git 命令符合性与测试驱动实施

新增 [CONFORMANCE.md](CONFORMANCE.md) 明确命令、原生 oracle、托管 profile、证据层及 G01–G66 尚未覆盖的门禁。`transport/test_git_conformance.py` 的 78 个工作流对同一 recipe 分别执行原生 bare Git 和生产 HTTP，然后删除测试专属 Git cache、新 mirror/fsck，比较 refs、HEAD 和全部可达对象的 OID/类型/原始字节。原生 recipe 在 fixture setup 验证，不能被 Cloud 缺口 XFAIL 隐藏；JUnit 保留命令/能力/profile，声明命令未执行会报错。

另有 8 项 Project/Scope Git 客户端故障/竞争回归：对象写入或发布失败后保留旧数据和本地工作、恢复重推、丢应答后 fetch 对账且重推不重复发布、同 base 两客户端恰一成功。控制面仍为替身，对象在磁盘，不把这些当作真实 PG/S3 故障或多实例证明。新增矩阵暴露的失败必须驱动后续实现；原 14 项失败不因加用例而解决，也不为“全绿”删除原断言或放开 Scope。

`transport/test_existing_history.py` 另有 22 条 Project/Scope 回归。修复 receive
只缓存当前 tree 导致的 revert/祖先 tag 误拒、gitlink 冷读 409，以及已有对象掩盖
stock Git 拒绝的问题。receive 广告/隔离仓库包含已有命名 refs，严格读取失败不得
当空 namespace；广告仍 refs-only。五条既有 workflow 转绿（其中两条 mixed-batch
依赖客户端对过期 ref 的预检，不代表服务端多 ref 事务已实现）。新增测试还覆盖
缓存删除后接收、冷读、旧 blob 复用、普通对象缺失、拒绝和恢复；不扩大 legacy
Scope 合同，也不接通 dormant SQL authority。

### Native 内部实服务 profile（与上方正式入口证据分开）

`integration/test_native_s3_transport.py` 对真实 S3/PostgREST 执行同一组 78 个
recipe；另验证 SHA-1/SHA-256 HTTP 空库/首推、冷 clone、typed tags、rewrite 和
删除。`test_native_s3_transactions.py` 在 stock admission **之后**插入 SQL
竞争：atomic 全部拒绝，普通 batch 拒绝过期 main 但发布合法 side；不是客户端
预检。`test_native_s3_history.py` 验证广告之后 force update 仍能取回旧 OID，
以及被拒绝提案的 receipt 不会变成可读 ref。保持精确 refs/HEAD/对象字节比较。

生产 collector 已接 native sweep/read pins；实测物理丢失不能被热 cache 掩盖，
未知 DELETE 保持非过期 fence，dry-run 不推进 epoch，SHA-256 隔离期和删除正常，
fence 期间冷读仍可用。升级/末尾 DDL 失败/原 SQL 重试保持合成存量数据及 ACL。
read-back 必须来自 canonical Project namespace，不接受跨 Project 的 backend 或
指向其他 namespace 的 location；旧兼容读取不因此获得 native receipt。

该 ASGI fixture 显式提供 grant，**没有**替代正式凭据解析、授权、配额或生命周期
准入；该历史 profile 没接正式 Git router。后续正式 Git 源码和独立 application
证据见上节；产品/Scope/自动写入及 Desktop 仍未完成。
原路由失败继续保留，不能因新 profile 转绿就称这些目标已修复。完整资源约束、
长上传续租、多进程/重启/恢复、消费者、迁移和部署门禁仍未完成，不得激活真实仓库。

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

原生 oracle 校正：此前误用 worktree 默认开启的文件 reflog 对照托管 ref 事务，Git 2.50.1 存在 prefix/reflog 目录竞争，曾 6/100 双失败。新独立对照中 worktree 19/100 双失败、bare 默认 100/100 恰一成功。`test_ref_prefix_create_race_has_one_winner` 现在明确使用 stock bare Git，保留原恰一成功与最终 ref 断言；没有 skip、重试或放宽断言。**这不是修复 stock Git 的 worktree bug**，客户端版本/profile 门禁仍需跟踪它；PG reflog 事务由真实 SQL 用例另行验证。

已知缺口由测试里的 `hosting_gap` 标记逐项说明。普通模式使用 **strict XFAIL**：能力修好后出现 XPASS，要求移除标记；`--target` 则直接作为失败报告。初始化/清理错误不会被缺口标记隐藏。

目前目标断言会暴露：删除 ref、强推 main、merge commit、附注 tag、blob tag、notes、多 ref 原子推送；空提交丢失与显式 source-head CAS 已由下述兼容修复纠正。对象层的非 UTF-8 文件名字节往返、Git 字节排序、tag 的 GC 可达性、gitlink 的外部对象边界已有正向回归；还覆盖嵌套 tag→commit/tree/blob 的 native fsck、损坏图禁止 GC、缓存复制中断后重试及浅缓存不能充当完整闭包。以上旧路由测试只证明相应 SHA-1 对象层，不因新增 native 实服务 profile 而自动获得完整 refs 或 SHA-256 能力。`20261003030000_fix_legacy_publication_head_cas.sql` 追加修复 root/Scope 显式 expected-head，并先锁 Project 来串行化不存在的 Scope 行。原目标测试保持成功/拒绝断言，显式提供同一个旧 head；没有旧 head 的 legacy RPC 继续树 CAS，不能宣称它自动获得 OID CAS。生产 adapter 对带 head 的写入使用新增 `_checked` RPC（含用量路径），旧 schema 缺此入口必须失败，禁止静默回退。空提交现在是真实新版本；receive 私有对象快照释放缓存锁后再发布，保留原发布点竞争断言；同时修复 Scope 旧可见 alias 在重试中越过新 canonical head 的问题。新增 SQL/升级、实际 SDK/PostgREST/JWT 与 HTTP 冷读/竞争用例；该兼容修复本身没有接通 native authority、S3 receipts 或命名 refs 原子事务；后续独立 native profile 如上所述。

原 `tests/conflicts/cases.py` 的 117 条不是 117 条现成测试。本运行器执行其中 **100 条**，采用相同起点、固定发布顺序制造 CAS 重试；真正并发另在 `concurrency/` 和 PG 用例验证。其余 **17 条未算作覆盖**，原因在 `harness/catalog_scope.py::EXCLUDED`：有些依赖旧 scope 所有权模型，有些需要不同入口或尚未搭好的删除/移动竞态。原样本和断言未改。C04（同源重命名）与 F12（待审提案 ID 碰撞）现已修复并移除对应缺口标记；剩余 A04/B11/C01 的预期与现行产品 LWW 策略不一致，继续保留失败，不提交 conflict markers 或反转现行删除策略来凑全绿。

`conflicts/test_operation_recovery.py` 补充 19 项组件回归：重命名从引擎实际首轮快照恢复文件/目录；无客户端 commit 的提案身份绑定 proposed tree、base、actor、channel 与 policy；Git 既有 ID 算法不变。另复现并修复 Project/Scope 的重试提案只在写入 batch、未 flush 就返回 pending 的缺陷，以及账本失败仍返回 pending 的问题。测试绕过进程缓存重新读取物理磁盘，并注入 flush/账本失败；控制面为替身，这不是 S3 耐久 receipt、GC 协调或跨实例实服验收。既有待审行不重写。

这些测试不是“Git 所有命令、所有参数和所有故障都已穷尽”的承诺。新架构尚未落地，因此未来数据迁移的全量回填、切换、回滚、并发旧新版本共存，仍必须用实际迁移实现再做验收。

CI：`repository-hosting-tests.yml` 在相关 PR 跑普通回归；手动触发可选真实 PG 和严格目标验收。整个 workflow 不需要线上 secrets。
