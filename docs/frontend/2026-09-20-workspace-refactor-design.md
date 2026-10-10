# Web 工作区导航与状态重构设计

日期：2026-09-20。状态：目标架构；导航、稳定 Files 容器、领域模块迁移及编辑会话已实施，具体交付与验证边界见[实施记录](./2026-09-20-workspace-refactor-implementation.md)。

依据：[导航调查](./2026-09-20-navigation-architecture-audit.md)、[项目切换调查](./2026-09-20-project-switch-investigation.md)与用户确认的现有交互。作用范围为 `puppyone-cloud/frontend`；桌面项目仅作为既有视觉参考。

## 1. 产品交互契约

重构以当前确定的交互为边界，不重新设计桌面界面或导航层级。

| 操作 | URL | 所属层 |
| --- | --- | --- |
| 切换项目 | `/projects/:projectId/data` | 路由 |
| Files / Git 切换 | `/data/...` / `/changes` | 路由；Files 恢复该项目最后访问的位置 |
| 打开文件、通过面包屑进入目录 | `/data/:path` | 路由 |
| 展开 / 收起树内文件夹 | 不变 | 文件树 UI |
| 打开 / 收起 Chat、Access | 不变 | 项目辅助侧栏 |
| 打开项目设置 | 不变 | 项目弹窗 |
| Git 筛选、选择提交 | 不变 | Git 会话 UI，保持当前语义 |
| resize、侧栏拖拽 / 折叠 | 不变 | 布局 |

Header 始终单行、保留下边框。文件树在左侧，Agent 在右侧；窄屏侧栏从 Header 下方覆盖工作区。已有开关动画、尺寸偏好、主题和桌面外观作为回归基线。布局模式变化不生成新页面，也不更换业务组件树。

## 2. 架构决定

继续使用 Next App Router、React、SWR 和 Zustand。所有项目、Files/Git、文件路径导航统一交给 Next；移除 Files 页面自行同步 `clientPath + history.pushState + popstate` 的路由实现。

稳定挂载通过 layout 的生命周期实现。路由身份从 Next 当前已提交的地址派生；不在另一个 store 中维护可写的 `currentProject/currentView/currentPath`。这符合 React 避免重复状态的原则，也利用了 Next 对共享 layout 的保留机制。

这里有两个不同概念：

- **当前路由**：Next 已提交的项目、视图和文件路径，决定 Header 当前项和正文身份。
- **导航请求**：用户希望前往的目标及其 pending 状态，只用于反馈、离开保护和处理异步确认；不能冒充当前路由。

`lastFilesLocation` 是离开 Files 后用于返回的书签，只由已提交的 Files 路由更新；它不能反向覆盖当前 URL。

## 3. 组件生命周期

```text
AppProviders                         身份 / 组织 / 主题 / 查询缓存
└─ WorkspaceShell                     应用级布局、项目列表
   └─ ProjectScope                    当前身份 + 组织 + 项目
      ├─ Project UI Session           侧栏 / Git 筛选 / 文件树状态
      ├─ Editor Sessions              草稿、保存任务、恢复
      └─ ProjectWorkspaceShell
         ├─ ProjectHeader             单行固定外壳
         ├─ Route Body
         │  ├─ FilesLayout            data/layout.tsx 的稳定容器
         │  │  ├─ ExplorerSidebar     切文件不卸载
         │  │  └─ FileRouteOutlet     文件 / 目录 / 加载 / 错误
         │  └─ GitWorkspace           历史列表 / 提交详情
         ├─ ProjectAuxiliarySidebar   Chat / Access
         └─ ProjectSettingsDialog
```

- 切文件：Files layout、文件树、项目 Header 和 Chat 保留；正文按文件身份更新。
- Files ↔ Git：项目外壳、侧栏和项目会话保留；Files/Git 的正文可以正常卸载，需恢复的数据由 session/query 层持有。
- 跨项目：明确更换 ProjectScope，取消旧项目不再需要的读取，防止旧回调修改新项目。草稿和已发出的保存具有独立文件身份。
- resize：只重新计算布局；不以断点给工作区加 key，也不切换两套 desktop/mobile 页面。

不要求所有页面永远留在 DOM 中。只有交互明确需要保留的 Chat 等组件保持现有挂载策略；文件树展开、筛选、草稿等可以通过有作用域的状态恢复。

### Agent 辅助工作区

Header、Access 和文件关联 Agent 的入口都打开项目级 `workspace_chat`，由
`ProjectChatPanel` 挂载 `features/agent/workbench`。文件 inspector 不再拥有另一套
聊天视图。入口可以指定 `agentId`；已有相应标签时激活它，否则创建一个本地空标签。

```text
ProjectAuxiliarySidebar
└─ AgentWorkbenchController            用户 / 项目作用域
   ├─ Header                          多标签、排序、关闭、窄栏折叠
   ├─ Launcher / History              整页选择、搜索、返回、刷新
   └─ 每个 chat tab
      ├─ WebAgentController           独立 session、草稿、提交回执、run 观察
      └─ ChatRuntimeView              保留 DOM、输入和阅读位置
```

标签栏和历史页遵循 Desktop auxiliary workbench 的几何契约：38px 标签栏、28px
标签和图标按钮、104–144px 展开标签、3px 间隔；空间不足时先压缩非当前标签，再进入
溢出菜单，当前标签始终可见。历史页使用 40px 工具栏和 32px 会话行；颜色来自宿主主题。

新建和切换空标签只改变本地 UI，不创建远端 session 或沙盒。历史目录由 workbench
集中按 Agent 查询，多个标签复用目录；只在打开历史页或主动刷新时读取目录，恢复已打开
的历史页也执行一次读取。只有选择会话才加载其 runs。打开中的会话不在历史列表重复显示。

标签身份、顺序、当前选择与各自草稿/提交回执按用户和项目隔离，保存在浏览器当前会话。
服务端仍是聊天历史和执行结果的权威。切标签保留运行观察；隐藏整个侧栏释放观察，重新打开
从现有 run 恢复，不重新提交任务。关闭标签不等于停止 run；Stop 是明确的独立操作。
提交结果尚未确认时保留标签及幂等回执，避免用户丢失重试入口。远端权限由原有 Agent API
执行，标签和 Agent 选择不构成授权。

## 4. 状态所有权

| 状态 | 所有者 | 生命周期与约束 |
| --- | --- | --- |
| 项目 / 视图 / 路径 | Next route + 纯解析函数 | 刷新、深链接、返回均从路由恢复 |
| 导航意图 / pending | navigation adapter | 短暂状态；提交或请求结束后收敛 |
| 项目、目录、文件、Git 历史 | SWR domain queries | 明确 query key；不再镜像进 UI Context |
| 树展开、Git 筛选、侧栏选择 | Project UI Session | 按身份 / 组织 / 项目隔离；组件只订阅需要的字段 |
| 最后 Files 位置 | Project UI Session | 仅记忆已提交位置，作为返回目标 |
| 草稿、脏状态、保存结果 | Editor Session | 按身份 / 项目 / 文件身份隔离；卸载不等于丢弃草稿 |
| 面板首选宽度 / 实际分配宽度 | layout store | 两者分开；resize 不覆盖用户偏好 |
| hover、菜单开关等 | 局部组件 state | 无跨页面要求时不提升到全局 |

现有模块全局的文件树状态需迁移到有明确作用域的所有者。需要跨项目恢复的现有偏好使用有容量限制的记录；不把旧项目的整个运行实例无限保留。退出账户后释放内存缓存，避免另一账户读到前一账户的 UI 或查询状态。

## 5. 统一导航入口

新增 workspace navigation 模块，提供：

- `routes`：类型化目标、URL 解析和生成，统一路径编码及既有 `type` 查询提示；type 只作提示，不能替代服务端文件事实。
- `useWorkspaceLocation`：从 Next hooks 派生当前身份；不通过 effect 复制成另一份可写位置。
- `WorkspaceLink`：保留真实 href、预取、键盘和新标签页行为；接入统一离开保护。
- `useWorkspaceNavigation`：供菜单、创建后跳转等命令式入口使用，同样经过离开保护。
- `NavigationBoundary`：协调当前编辑器和需要保护的 panel，集中处理一次导航的确认与 pending 反馈。

导航流程为：用户意图 → 汇总离开保护 → 确认一次 → Next 导航 → 已提交路由驱动界面 → 目标视图读取数据。

取消发生在导航发出前。请求序号用于防止我们自己的异步确认、创建回调、兼容入口等把用户带回过时目标；不能把请求序号包装成能够取消 Next 内部一切导航的承诺。提交状态用官方 pathname/search hooks 观察，不依赖私有 router tree 或虚构的 App Router events。

连续 Git → Files 点击必须处理完整；即使 Files 仍是当前路由，也不能简单忽略用户的新目标而留下 Git pending。导航反馈和数据加载反馈分开，后台 API 失败不能使 Header 永久停在 pending。

**浏览器历史的边界**：`popstate` 已发生时，不能像点击事件那样通过 `preventDefault` 取消历史移动。默认保留当前返回/前进行为，并保证切走前的草稿可恢复；刷新/关闭使用 `beforeunload`。站内点击的离开确认统一执行一次。若以后要求浏览器 Back 也必须先确认，应另立交互与浏览器兼容性验收，不能靠偷偷补 history 条目实现。

## 6. Header、加载、错误

当前项、项目名称和路径由同一个已提交路由身份派生。路径标签需要元数据时可补充查询，但元数据迟到不能改变路由身份。Header 的页面操作插槽可以继续使用 portal；贡献者必须与当前 route identity 对应，不能拿旧页面贡献充当新页面标题。

点击 Git 后分两个阶段：

1. Next 尚未提交：当前 Files 仍是 active，Git 提供 pending 反馈。
2. Git 已提交：Header 和正文共同进入 Git；数据未到时显示 Git 自己的加载态，失败时显示 Git 错误与重试。

加载边界只包对应正文。项目 Header、Chat 和项目导航不属于正文 fallback。删除当前 `ProjectPageLoadingShell` 在项目正文中重复渲染 Header 占位的用法。Files 和 Git 各有正文 loading/error；单文件读取失败不会把整个项目替换为全屏加载。

## 7. 数据读取与编辑保存

现有读取层已有取消和过期响应保护，重构保留这些保证并统一查询身份：身份作用域、projectId、文件路径、版本和相关筛选必须进入对应 query key。共享同一资源的组件订阅同一个查询，不再由多个 Context 各自 fetch 后保存副本。

首屏的文件树或 Git 主内容不等待 Access 统计等非关键读取。项目切换不以全部项目元数据成功返回为前置条件。mutation 和 WebSocket 通知通过领域失效规则更新相关查询，不在页面里散落全局刷新。

Editor Session 独立管理读取到的基线、草稿、dirty、保存快照和冲突：

- A 文件的晚到读取不能覆盖 B 文件。
- 保存捕获当时的文件身份和内容版本；导航取消的是不再需要的读取，不把已发送写请求当成可以安全撤销的读取。
- 保存完成后，只更新对应文件和对应草稿版本；用户后来输入的内容仍保持 dirty。
- 已有草稿持久化键升级需兼容旧草稿，不通过重构直接丢弃本地未保存内容。

前端一致性和后端读取速度分开验收。已有调查显示的授权/元数据串行读取需要独立优化；路由重构不会自动消除这些数据库往返。

## 8. 文件与依赖边界

```text
app/(main)/projects/[projectId]/
  layout.tsx                         组装 ProjectScope / Shell
  loading.tsx / error.tsx             项目正文边界
  data/layout.tsx                    组装 FilesLayout
  data/[[...path]]/page.tsx           薄路由入口
  changes/page.tsx                   薄 Git 入口
  history/, access/, settings/        仅既有 URL 兼容入口

features/workspace/
  navigation/                        路由契约、入口、保护协调
  session/                           项目 UI 状态及 selectors
  layout/                            现有几何分配、响应式、动画
features/files/
  FilesLayout.tsx
  FileRouteView.tsx
  explorer/                          文件树
  editor/                            编辑 session 与编辑器集成
  queries/                           文件领域读取
features/git/
  GitWorkspace.tsx
  components/
  queries/
components/project/                  项目外壳和通用 Header 组合
lib/                                 API transport、通用请求设施
```

这是职责目标，已有合理模块可以保留原文件名，避免纯搬家。重点是把当前约千行的 Files page、Git page 里的编排和业务逻辑移出 route entry，并让它们按生命周期拆分。

依赖规则：app 组装 features；features 不反向 import app 的实现；跨 feature 通过公开接口；共享 UI 不决定业务导航或请求。工作区导航由一个模块封装，普通业务组件不自行调用原生 history 或 document capture 拦截。

内部 Access / Settings 操作直接打开侧栏 / 弹窗。旧 URL 只在边界做一次兼容转换，并验证目标项目仍然有效；移除业务里“先导航到兼容页，再 effect 跳回来”的依赖。`/changes` 为 Git 主入口，`/history` 保留兼容。

## 9. 实施顺序与完成标准

按可以独立验证的完整流程迁移，最终删除旧入口，避免新旧系统长期并行：

1. 固化现有交互与视觉基线；把三个已复现缺陷改写为期望正确行为的回归用例。
2. 一起迁移路由契约、Header、Files 路径和稳定 Files layout。撤掉 pendingView 冒充 active、clientPath 镜像和页面 popstate。
3. 迁移所有站内导航和编辑离开保护；验证按钮、链接、面包屑、创建后跳转都经过同一流程。
4. 收口项目 UI / editor / query 所有权、加载错误边界及旧 URL 适配；移除失效旧代码和重复订阅。
5. 类型检查、相关单测、production build，以及使用真实 Next Router 的浏览器验收。更新架构文档和可自动检查的依赖边界。

必须验证的真实浏览器场景：

- Files → Git → Files，冷路由、慢路由、快速反向点击。
- 文件 A → 文件 B → Git → Back / Forward；直接刷新深层文件 URL。
- 未保存时点击项目、面包屑、Git：确认一次，取消后所有位置一致。
- 离开后返回草稿；保存进行中切文件；旧读取 / 写入响应迟到。
- Git API 慢或失败：显示 Git 加载 / 错误，不能显示 Files 冒充 Git。
- A / B 项目连续切换，旧请求不覆盖新项目；账号切换隔离。
- Chat 打开、设置弹窗打开时 resize；桌面 / 窄屏 / 折叠屏模拟宽度下组件身份、动画和当前样式符合基线。
- 老 `/history`、`/access`、`/settings` 链接仍有明确结果。

分别记录点击反馈、路由提交、目标正文身份、数据就绪时间。DOM mount 计数只用于定位生命周期；正常 React rerender 不等于卸载或全屏重建。

完成标准是：同一事实只有一个所有者；所有受影响入口完成迁移；真实历史导航、异步竞态、草稿和布局场景通过。文件数量或单测数量本身不能作为架构完成的证明。

## 参考

- [React：Choosing the State Structure](https://react.dev/learn/choosing-the-state-structure)
- [Next 15：layout](https://nextjs.org/docs/15/app/api-reference/file-conventions/layout)
- [Next 15：useRouter](https://nextjs.org/docs/15/app/api-reference/functions/use-router)
- [Next 15：loading](https://nextjs.org/docs/15/app/api-reference/file-conventions/loading)
- [Next：Native History API](https://nextjs.org/docs/app/getting-started/linking-and-navigating#native-history-api)
