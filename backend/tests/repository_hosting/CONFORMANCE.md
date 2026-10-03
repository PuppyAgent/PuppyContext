# Git conformance：可执行合同与尚未覆盖的门禁

状态：实施中，**不是完整 Git 支持声明**。Owner：pi-issue-062。
需求来源：ISSUE-062 的 G01–G66、M01–M20、07/08；不减少原验收范围。

## 1. 执行原则

`transport/test_git_conformance.py` 的 **78 个工作流**使用同一份 recipe：

1. 在独立原生 bare Git 上执行，形成参考结果。
2. 通过 stock Git → HTTP → 生产 transport/Version Engine 执行同样操作。
3. 删除该测试专属的服务端 Git view cache。
4. 新客户端 `clone --mirror`，执行 `fsck --full --strict`。
5. 比较全部用户 refs、HEAD/symref，以及全部可达对象的 **OID、类型、原始字节**。
   遍历关闭 replace-object 覆盖，不能让 replace ref 改写原始图的校验。

原生 oracle 在 fixture setup 运行；recipe 或 oracle 失败不得隐藏为 Cloud
XFAIL。声明的命令必须确实被执行，否则 setup 失败。JUnit 记录工作流、G 编号、
声明/实际命令、profile 和存储证据层。G 编号是**关联**，不是整项已经通过。

所有新完整托管 workflow 都使用 **Project / SHA-1 target**。旧 Scope 继续由
`test_legacy_scope_profile.py` 验证受限兼容合同，不为全托管目标删除其拒绝断言。

**这些仍是组件测试**：HTTP 和 Git 子进程是真的，生产写入代码是真的；Auth、
控制面是替身，对象存物理磁盘。清掉 view cache 不是进程重启、真实 S3 或双实例
PG/S3 证明。native oracle 使用 file://（否则 depth/filter 可能被本地 clone 忽略），
并明确开启参考服务器的 filter capability；不更改 PuppyOne 的 capability。

## 2. 本轮 recipe

权威枚举是 `harness/{history,ref,network}_workflows.py`，不另维护漂移的测试 ID 清单。

| 家族 | 场景 | 核心验证 |
|---|---|---|
| index / worktree | add、restore --staged/worktree、status、clean、mv、rm、commit | index/工作区状态正确，再发布确认内容 |
| merge | ff-only、squash、no-ff、octopus | 原始父节点顺序、图和 OID；不是只比文件 |
| cherry-pick / revert | range、--no-commit、连续 revert | 生成的新提交和所有历史可取回 |
| patch | format-patch → am --3way；binary diff → apply --index | 补丁提交链、二进制字节 |
| rebase | 普通、--onto、interactive --autosquash | 重写后的图与被保留/丢弃的改动 |
| conflict sequencer | merge/rebase/cherry-pick/revert/am 的 abort、continue；支持 skip 的四种命令 | 真实冲突阶段、未合并 index、恢复/解决后继续提交和推送 |
| reset / recovery | soft、mixed、hard；reflog 找回并发布丢失 tip | HEAD/index/worktree 区别，恢复精确 OID |
| workspace | stash -u、linked worktree、sparse-checkout、detached HEAD 显式目标 | 未完成工作保留，稀疏视图不丢树外数据 |
| pull | ff-only、rebase、merge；分叉 ff-only 拒绝后显式恢复 | 两客户端历史收敛，拒绝不清掉本地工作 |
| objects | 外部 gitlink、原始 commit headers/encoding/签名字段 | 不要求外部 submodule OID；保留原始字节，签名字段样本不代表验证签名真实性 |
| branches | update、copy、orphan、atomic rename、delete、fetch --prune | main 不被旁支操作覆盖，原子/删除语义 |
| rewrites | --force、显式精确 --force-with-lease、+refspec、过期 lease、已发布 amend | 显式改写与旧 OID 约束，不能跳过 CAS |
| tags / generic refs | annotated、nested、blob/tree、overwrite/delete；notes、replace、custom refs | 所有目标类型和自定义 namespace 完整交换 |
| multi-ref | --all、--tags、--atomic、--mirror；atomic/非 atomic 混合结果 | 不是仅一条变更；正确逐 ref 状态与原子性 |
| negotiation | v0/v1/v2、depth/deepen/unshallow、shallow push、filter/promisor lazy fetch | 检查真实 packet negotiation；禁止静默降级或下载全部对象冒充 filter 支持 |
| queries | 新 clone 的 log/show/diff/blame/merge-base/rev-list/describe/range-diff/grep/bisect | 基于取回的历史实际运行，不只推断“本地 Git 能做” |
| client export / maintenance | mirror 后 bundle 或 fast-export/import、archive、repack/pack-refs/commit-graph/multi-pack-index/verify-pack/reflog expire/gc/fsck | **客户端副本**逻辑图不变；不冒充权威 PG/S3 运维或配对备份 |

`transport/test_git_failure_recovery.py` 另有 **8 项**（Project 与 legacy Scope 各 4 项）：

- 对象写入失败、发布失败：拒绝 push、旧确认数据可冷读、本地工作仍在，恢复后重推成功。
- 发布后丢失应答：fresh fetch 发现已提交 OID，重复 push 不产生第二次发布。
- 两客户端从同一个 head 同时推送：恰好一个成功，另一个的本地工作仍在。

故障通过生产路径的注入点模拟，不是实际 S3/PG 宕机或 TCP 断开。并发同步在客户端
启动点，不在服务端已持有互斥锁的发布区人为设置 barrier；服务端可以在 SQL 前拒绝
过期写入。跨实例原子性仍必须由真实 PG/S3 场景证明。

## 3. 现有矩阵与缺口（不得据 recipe 数量宣布完成）

| 能力 | 已有可执行证据 | 尚需补齐的完整目标 |
|---|---|---|
| G01 | 原生 Git init；组件 fixture 初始化 | 真正空仓库/unborn HTTP clone、首次多 ref 发布、生命周期 |
| G02–G04 | object unit、HTTP 二进制/mode/symlink 往返 | 极限尺寸、全产品入口和真实 S3 保真 |
| G05–G11 | object graph、原始对象/DAG/tag recipes；空提交身份、显式 root/Scope head CAS、冷读与竞争已修复 | 修复所有目标失败；完整生产发布/冷读/GC |
| G12 | 原生 SHA-1/SHA-256、PG ref 对照 | per-repo SHA-256 的对象存储、协议、跨实例全链路 |
| G13–G14 | opaque header、gitlink/.gitmodules 字节 | 真正签名及用户 trust 验证、attributes/filter 执行隔离 |
| G15–G23 | SQL ref 事务、原/新增 branch/tag/generic ref recipes | 广告快照、任意字节 refs、策略、跨实例 CAS、API 接入 |
| G24 | SQL symbolic/detached/unborn HEAD 原语 | 默认分支管理、实际 clone 行为和生命周期入口 |
| G25–G29 | rewrite/multi-ref recipes、SQL atomic/verify/HEAD 测试 | 完整 wire 发布、admitted API、部分结果及权威 rename |
| G30 | 本地 reflog 恢复、SQL reflog 原语 | 服务端 retention、删除/强推后恢复、GC 协调 |
| G31–G38 | 上述提交与历史生成 recipes | 所有目标失败、rebase-merges、已发布 squash/rebase、产品消费者 |
| G39–G43 | fresh-clone queries 和 conflict sequencer | 云 read API 固定 snapshot/pagination、云 Workspace 生命周期与 rerere |
| G44 | 真实组件 HTTP、原权限/非法协议测试 | 真实 PG/S3 联合服务与兼容客户端版本矩阵 |
| G45 | file:// native oracle | Git-over-SSH；不把 sandbox SSH 当托管 SSH |
| G46–G49 | 真实 negotiation、shallow/filter recipes | 修复 v1/v2/filter 缺口、不完整 canonical 历史模型、授权边界 |
| G50 | 已有 pack/HTTP 组件测试 | 大/thin/delta/chunked pack、取消、资源上限与压力实服矩阵 |
| G51 | 无完整闭环证据 | push options、signed push certificate 协商和事务关联 |
| G52–G54 | 客户端导出/恢复/fsck、对象图校验 | 权威导入导出/运维任务、upload-archive profile |
| G55–G58 | 现有 GC/moto、SQL 原语；客户端维护 recipe | 真实 S3 pin/epoch/GC 并发、配对 PG+S3 备份恢复、容量/性能 |
| G59–G61 | 上述真实客户端 workspace 操作 | 云/桌面 session、branch/upstream、dirty state、失败保留的全部集成 |
| G62–G64 | helper 不读取用户 Git 配置或 hooks | 受控云 config/隔离运行合同；任意宿主 hooks、邮件/外部系统不属于本轮 bare 服务 |
| G65 | 原代码仍限制 LFS pointer | 普通 pointer blob 的目标合同测试；LFS transfer 服务是独立扩展 |
| G66 | 明确非本轮目标 | 不因支持 tag/ref 宣称交付 PR/Actions/release 产品 |

完整服务测试还必须包括：掉电/进程崩溃、真实存储中断、已确认写入后 cacheless
第二实例读取、对象丢失/损坏、GC 与 publication 交错、租约/代际失效、恢复演练、
旧客户端和持续写入中的迁移。这些未完成项不会以空 `pass`、skip 或 native oracle
计为已覆盖；现有 07/08、A6–A11 与 M01–M20 仍然约束最终关闭。

### 版本身份与发布竞争修复

空提交不再因 tree 相同被吞掉。兼容 SQL 先锁 Project，再检查 root/Scope 的显式
expected head（包括不存在），原发布点双客户端竞争断言保留并扩到 Scope。
无 expected head 的旧 RPC 仍维持原合同；新客户端使用 `_checked` 入口，旧 schema
不得静默降级。升级不重写旧数据、原函数身份/default/ACL 不变，失败回滚和重试有
实际 PG 证据；生产 history adapter 经真实 SDK/PostgREST 验证 checked/用量路径。

receive 的不可变私有对象快照不再把 cache lease 持有到 SQL 发布，缓存删除也不影响
在途请求。放开真正竞争后，另修复 Scope 旧可见 alias 导致 stale CAS retry 自动合并
并错误确认的问题。相关对象测试仍是磁盘，不等于实际 S3 receipt/pin/GC 完成。

托管 prefix-ref oracle 校正为 stock **bare** Git，原恰一成功断言未放宽。
独立 100 次对照：bare 100 次恰一成功，worktree 19 次因文件 reflog 目录竞态双失败。
该 worktree/Git 版本问题继续记录，不能宣称上游 Git bug 已被本任务修复。

## 4. 运行与判读

```bash
# 从 Cloud 仓库根运行：新 Git workflow + 故障回归，严格暴露目标缺口
backend/.venv/bin/python scripts/testing/run_repository_hosting.py \
  --target -q -k 'native_git_workflow or git_failure_recovery'

# 辅助真实 PostgreSQL；不是 Supabase/Auth/S3 验收
backend/.venv/bin/python scripts/testing/run_repository_hosting.py \
  --native-pg --target -q -m 'not hosting_supabase'

# 完整现有实服入口（仍不等于尚未实现的全部真实 S3/迁移场景）
backend/.venv/bin/python scripts/testing/run_repository_hosting.py --live --target -q
```

日常模式沿用 strict XFAIL 标记已知缺口；**严格目标模式仍必须失败**，不算支持。
新增失败数不能解释成新增同等数量的独立产品缺陷：多个命令会触发同一缺失的 ref
事务/策略。初次执行发现的 revert-range 误拒、gitlink 冷读失败和祖先 commit tag
缺失对象已按原断言修复：receive POST 使用完整广告历史的增量对象缓存，gitlink
健康检查不要求外部 commit 本地存在。广告仍不下载对象，产品写入不调用历史遍历。

接收广告和隔离仓库现在包含已有命名 refs，stock Git 拒绝不能因对象已存在被覆盖。
因此两条 mixed-batch recipe 也通过；**这里过期 ref 被客户端预检排除/拒绝，不能
据此宣称服务端已支持多命令部分提交或原子事务**。`push-all/tags/atomic/mirror`
仍失败。实际 SQL 发布前还缺命名 ref CAS、跨实例与新 authority 接入。

`transport/test_existing_history.py` 增加 22 条 Project/Scope 回归，包含接收前及
回读前缓存删除、复用旧 blob、普通丢失 blob 仍判损坏、过期写入保留本地工作、
stock Git 拒绝新/已有对象、广告/接收 refs 查询失败不得当空 namespace。存储仍是
磁盘，控制面为替身；不是实服耐久性证明。现行本机 oracle 是 Git 2.50.1；
Linux/Windows 与版本范围尚不能由这次执行代替认证。
