# 架构

WitX 面向个人自部署，一个部署供一位 Owner 使用。租户字段与 PostgreSQL 行级安全仍作为数据访问边界。

## 组件

| 组件 | 代码 | 职责 |
| --- | --- | --- |
| Web | `apps/web` | 对话、审批、设备、人工接管、定时、记忆和自定义 Skill / Plugin |
| API | `services/cloud/witx/api` | HTTP API、OIDC / 会话 / CSRF、SSE、MCP 与 Gateway 接入 |
| Hub | `services/cloud/witx/hub.py` | Gateway WebSocket 会话、动作派发、结果和接管转发 |
| Worker / Harness | `services/cloud/witx/worker.py`、`harness` | Run 领取、模型与工具循环、风险判定、上下文压缩和目标检查 |
| Services / Store | `services/cloud/witx/services`、`store` | 事务内状态转换、数据库访问、迁移、留存与恢复 |
| Domain | `services/cloud/witx/domain` | 状态类型与纯函数规则 |
| Gateway | `services/gateway` | 本机设备发现、Appium 会话、SQLite journal、输入前检查和执行 |
| Contracts | `packages/contracts` | Python 协议与 Web API 合同，生成 TypeScript 类型 |

API 和 Hub 同进程，Worker 独立运行。Gateway 主动连接云端，手机不开放外部端口。PostgreSQL 管理权威状态；私有对象存储保存截图与恢复账本。Worker 启动时、此后每天会独立清扫没有观察记录认领的截图，失败后每小时重试；最后一次清扫结果按租户保存在数据库中，供 `/metrics` 和告警读取。

## 分层

```text
cli → api → hub / worker → harness → services → store / models / objects → domain
```

上层只依赖下层，import-linter 检查依赖方向。Gateway 不导入云端代码，共享协议不依赖两者。

租户业务表启用 PostgreSQL 行级安全。登录前需要读取的身份表不依赖该机制：应用角色只读身份信息，会话表另有写入和更新权限。测试检查除身份表 `memberships` 外，每张带 `tenant_id` 的表都启用 RLS 并具有策略。

## 动作生命周期

1. 模型根据截图提出输入及预期效果。
2. 独立模型调用判定风险；纯函数规则结合模式、效果、App 和输入内容决定是否审批。风险调用失败按敏感处理。
3. 准入检查设备就绪、最新截图、租约 epoch、前序动作和 UNKNOWN 状态。
4. 敏感输入绑定参数摘要审批；派发时重新计算当前参数摘要，要求它与动作和审批存储的摘要一致，再检查任务控制版本、租约和批准有效性。
5. Gateway 按 action_id 去重，核对当前设备与屏幕几何 / 前台 App，紧接着再次复核租约 epoch、停止屏障和截止时间，随后写入 journal intent，再执行并记录结果；输入前检查出现意外异常时拒绝动作并返回结果。iOS 文字输入可按驱动写入、逐键输入或输入框菜单粘贴三种方式执行，仍共用准入、策略和单次执行记录。
6. 执行后上传新截图。失败可能产生 UNKNOWN，只有人凭更新证据裁定后才能继续。

同一 action_id 最多执行一次；重新提出一个动作是另一个 ID，仍需遵守准入和策略。完成需要晚于最后输入的新截图及已结束的动作，但不构成对业务结果的绝对保证。

## 控制与恢复

停止和暂停是屏障：任务先进入过渡态，等待 Gateway 确认后完成。恢复会更新 epoch，使旧输入失效。Run 领取的 epoch 阻止旧 Worker 覆盖新状态。人工接管与 Agent 共用设备写入锁，只能在允许的设备状态下进行；接管输入不进入模型上下文。

Worker 的步骤与停止流程按相同顺序锁定任务、再锁定 Run，避免互相等待。连续 3 次步骤未完成时，Run 等待用户继续，避免持续付费重试。

恢复账本记录删除、停用和吊销。`witx admin recover` 在恢复数据库后重放账本、停止未完成任务，并将无法确认的在途输入保留为 UNKNOWN。账本需要独立保全，完整恢复要求数据库、对象存储与账本均可用。

## Web 加载与任务历史

设备、定时、自定义和设置页按需加载，链接有打开意图时提前加载；更新后旧代码加载失败会尝试刷新一次。截图接近视野时加载，同图共用请求与内容，离开远处时释放；同时最多 3 个请求，未显示的缓存上限为 48 MB。

任务列表按游标分页。长任务按轮次边界加载最近的片段，每段不超过约 200 步和 20 轮，至少一轮；向上滚动加载更早的片段，并保持当前位置。未结束或 UNKNOWN 输入的证据始终加载。事件到来后按序号获取变化，重连或页面重新可见时重取已加载区间；历史事件只回放界面需要的类型。

工作区通过 `/api/v1/workspace/events` 接收按租户隔离的 SSE 通知。API 使用独立的 PostgreSQL LISTEN 连接接收事务提交后的变化，页面只刷新涉及的列表；连接恢复时整份刷新，另有每 60 秒一次的兜底刷新。通知不包含任务正文；积压或重连丢失通知时要求重新获取状态。

## Skill 与 Plugin

Plugin 只含文本和元数据，不执行代码、不授予权限。安装锁定 commit 与文件摘要；不兼容当前工具或效果的 Skill 不加载。本人写的 Skill 通过本机 marketplace 保存，Agent 可以一次起草多个，由用户检查和保存。复杂任务结束后可在结果下提供保存为 Skill 的按钮，不再为此打断任务完成。同名草稿只允许最新版本保存；长上下文压缩会保留已加载 Skill 的名字和版本，提示 Agent 在继续依照它操作前重新加载。

内置 marketplace 名称是 `witx-preview`，来源为 `https://github.com/FoLAWy-py/witx-plugins-preview.git`；未指定来源的 `witx plugin install basics` 使用它。安装和更新仍要求用户检查并确认。

已知安全边界和 Preview 限制见 [README](../README.md#preview-的已知限制)。
