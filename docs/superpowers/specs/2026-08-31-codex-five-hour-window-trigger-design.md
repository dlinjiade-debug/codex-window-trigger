# Codex 五小时窗口云端触发器设计

日期：2026-08-31
状态：规格已通过；2026-08-31 用户另行同意改用专用公开仓库免费方案

> 2026-09-04 实施修订：网页端事件任务无法在分配运行前表达全部可信过滤器，
> 因此改为由同仓库 GitHub Actions 在可信 PR 后发布一条幂等、非评审
> `@codex` 评论，直接启动 Codex Cloud chat。状态只有在 PR 与精确 bot 评论
> 均可观察后才落盘；Cloud 环境无 secrets、关闭 internet，自动代码评审关闭。
> 本修订取代下文关于 ChatGPT Web PR 事件监听器的交付描述。

## 1. 目标

当公开预测表明 Codex 可能即将发生全局用量重置时，让 **Codex/ChatGPT Work 云端事件任务启动一次**。该事件运行本身就是一次 Codex 用量触碰，用于尝试让尚未开始的五小时窗口提前计时；它不再派生第二个任务，也不负责把额度耗尽。

用户选择的概率阈值为 **30%**。系统必须在电脑关机、桌面应用未运行和手机未连接 VPN 时仍能工作。

OpenAI 公开文档只确认本地消息与云端任务共享五小时窗口，并未保证“首次任务一定重新锚定窗口”，也未说明一次全局自动重置是否会保留原窗口的自然到期时间。因此，本功能是 best-effort 的“窗口触碰”，不能承诺一定得到两段完整五小时额度。

## 2. 非目标

- 不把五小时额度消耗到上限。
- 不兑换 banked reset，不购买 credits，不启用 auto-reload。
- 不使用 OpenAI API key；API 用量与 ChatGPT 套餐五小时窗口不是同一计费路径。
- 不恢复任何现有已暂停、且不能稳定联网的旧版本地监控任务。
- 不声称能够通过未公开接口读取或修改个人账户的实时五小时计时器。
- 不因单独的 banked credit、模型 Juice、服务事故或历史已确认事件触发。

## 3. 方案选择

### 采用：GitHub Actions 探针 + GitHub PR 事件 + 单次云端 Work/Codex 运行

1. 一个专用公开仓库 `codex-window-trigger` 承载只读预测探针；用户已同意代码、公共预测数据及触发记录公开。
2. GitHub Actions 每五分钟读取公开预测接口。
3. 条件首次满足时，工作流在专用仓库创建一个带唯一事件 ID 的触发 PR。
4. ChatGPT Web 上的 GitHub 事件任务监听该仓库的新 PR，同时精确限制作者为 `github-actions[bot]`、标题前缀为 `[codex-5h-touch]`；评论、提交更新、评审、合并和关闭事件均不匹配。公开标题本身不是身份认证。
5. 匹配后，GitHub 事件任务自身启动一次云端 Work/Codex 运行；该次运行就是“触碰”动作，不再启动任何额外工作。

选择此方案的原因：探测过程不使用 Codex 配额，只有真正命中后才启动 Codex；所有部分都运行在云端，不依赖用户电脑在线。GitHub 官方文档说明公开仓库的标准 GitHub-hosted runner 免费，计划任务最短间隔为五分钟；ChatGPT 官方文档说明符合条件的网页任务可以由 GitHub PR 活动触发。不得改用收费的 larger runner。工作流默认关闭，验证完成后才启用。这里的免费仅指探针基础设施；触碰仍使用 Codex 套餐额度，并受第 7 节 credits 风险边界约束。

### 未采用：直接使用现有告警邮件

`notify.codex-reset.com` 只承诺发送服务方判定的高可信预测，无法保证在 30% 时产生邮件，因而不能实现用户指定阈值。

### 未采用：Codex 自身定时轮询

每一次轮询本身都会产生 Codex/Work 用量，可能持续提前启动窗口，与“仅在 30% 命中时触碰一次”的目标冲突。

### 未采用：本地 CLI/Windows 任务

可以检查公开接口，但电脑关机后无法运行，不满足既有离线要求。

## 4. 触发判定

每次探针同时读取：

- `https://codex-reset.com/api/forecast`
- `https://codex-reset.com/api/feed`
- `https://codex-reset.com/api/timeline`

只有下列条件全部成立时才允许创建触发 PR：

1. 预测分数 `signal_percent >= 30`。
2. feed 明确为 `stale: false`，且 feed 的 `fetched_at` 与 forecast 的 `updated_at` 距当前均不超过十分钟。timeline 用于交叉核对事件语义；如果它提供接口级更新时间，该更新时间也必须在十分钟内。原始帖子本身可以早于十分钟，只要预测仍新鲜且目标时间尚未到达。
3. 信号明确指向 Codex/ChatGPT Work 的全局或广泛用户用量 reset；必须能关联到公开事件或帖子 ID。
4. 排除 `credits`、`banked`、`referral`、`juice`、单一账户故障、OpenAI Status 可用性事故和已经结束的历史事件。
5. 如果预测给出目标时间，目标必须位于当前时间之后且不超过五小时；如果没有目标时间，只允许在一个此前从未处理过的新 reset episode 首次被探针观察到 `signal_percent >= 30` 时触发。
6. 同一事件未触发过，且距上一次任意成功触发至少 24 小时。

30% 是较激进的概率阈值，因此“明确 reset 语义、新鲜度、未来窗口、去重和冷却”均为强制保护，不能只按数字单独触发。

## 5. 事件身份与去重

优先使用 forecast 的 `alert_event_id`。若不存在，则由关联的 X status/event ID、预测目标时间和 UTC 日期生成稳定的 episode key。

触发分支命名为 `trigger/<episode-key 的前 12 位十六进制哈希>`，避免外部 ID 中的字符或长度形成非法分支名。PR 标题为：

```text
[codex-5h-touch] <episode-key>
```

工作流创建前查询所有 open/closed PR：

只信任同一专用仓库的工作流机器人 PR；第三方或 fork 中模仿标题的 PR 不得影响去重和冷却。若网页端无法精确配置仓库、作者及新 PR 事件过滤，则不启用公开仓库的云端触碰。

- 已存在相同 episode key：退出，不重复创建。
- 最近 24 小时已有 `[codex-5h-touch]` PR：退出并记录冷却原因。
- 其他情况：创建一次新分支和 PR。

触发 PR 保持开启，避免“自动关闭”形成第二次 PR 活动并误触发。它们只作为低频审计记录存在；清理由用户手动进行，清理前先暂停 ChatGPT 事件任务。

## 6. 云端事件运行

真正为零工作的 agentic run 不存在：事件任务至少需要读取触发事件并结束。这里不安排代码检查、分析、图片生成、仓库修改或其他“轻量任务”；GitHub 事件任务自身只运行一次，输出一条固定格式的最短回执后立即结束：

- episode key；
- 命中概率；
- 预测目标时间或“未提供”；
- 触发时间（北京时间和 UTC）；
- 来源链接；
- `已启动一次云端五小时窗口触碰；并非已确认额度窗口重锚。`

任务不得：

- 派生第二个任务、循环、生成图片、启用 Fast mode 或主动扩大上下文；
- 修改仓库、创建后续任务或重复调用自身；
- 兑换重置、购买或充值 credits；
- 在本次运行结束后继续消耗额度。

如果界面允许选择模型，优先选择当时可用的最轻量 Codex 模型；否则使用事件任务默认模型。无论模型如何，该事件运行都必须保持单轮、固定短输出，并在回执后立即结束。

## 7. Credits 风险边界

OpenAI 没有为个人账户提供可供此云端探针读取的实时五小时余量 API。若命中时套餐额度已经耗尽，而账户仍有 flexible credits，单次云端任务可能使用已有 credits；若开启 auto-reload，还存在自动购买风险。

因此启用前必须满足：

- auto-reload 已关闭；
- 用户确认账户没有希望保留且不能被本任务使用的 credit balance；
- 若以后启用 auto-reload 或购买 credits，应先暂停本事件任务。

代码和任务提示词不能假装能够程序化保证“绝不扣 credits”。该限制必须在 README 和上线检查表中明确保留。

## 8. GitHub 权限与安全

仓库必须为专用公开仓库，不存放用户其他项目代码、邮件地址、Cookie、OpenAI 登录信息或 API key。公开内容限于监控程序、合成测试数据、公开预测数据、触发记录和去重状态；本地执行账本、账号检查结果与浏览器信息不得上传。

工作流只使用 GitHub 自动提供且仅限本仓库的 `GITHUB_TOKEN`。权限收窄为：

```yaml
permissions:
  contents: write
  issues: write
  pull-requests: write
```

其余权限为 `none`。仓库需单独允许 GitHub Actions 创建 PR。ChatGPT GitHub 连接只授权此仓库。

预测接口响应只作为不可信数据解析：限定响应大小、设置超时、拒绝非 JSON、验证字段类型，不执行响应中的文本或 URL。

## 9. 失败处理

- 网络失败、超时、非 2xx、JSON 无效或字段缺失：fail closed，不创建 PR。
- 数据超过十分钟：fail closed。
- 三个接口对事件语义明显冲突：fail closed，只保留 Actions 日志。
- GitHub PR 创建失败：本轮失败，下一轮可重试；episode key 保证最终只创建一次。
- ChatGPT GitHub 事件未触发：保留 PR 作为证据，并通知用户人工检查连接；不得用 Codex 定时轮询兜底。
- 如果 canary 证明 ChatGPT GitHub 连接会忽略由 `GITHUB_TOKEN` 创建的 PR，则上线被阻塞；不得未经新批准改用 PAT、GitHub App 私钥或邮件密钥绕过。
- GitHub 定时工作流允许存在平台调度延迟；五分钟是请求的最短周期，不是严格实时 SLA。

## 10. 验证方案

### 本地与 CI 测试

将触发判定实现成无副作用的纯函数，并覆盖：

- 29% 不触发、30% 触发；
- stale feed 不触发；
- banked/credits/juice 不触发；
- 目标时间已过或超过五小时不触发；
- 无目标时间时，只接受此前未处理过的新 episode 首次被观察到至少 30%；
- 相同 episode key 去重；
- 24 小时冷却；
- 三源冲突和畸形 JSON fail closed。

### 上线验证

1. 用 fixture 和 `workflow_dispatch` dry-run，确认不会创建 PR。
2. 创建一个明确标记为测试的 canary PR，确认 ChatGPT 事件任务自身只运行一次、输出固定回执且不派生其他任务。该步骤会产生一次很小的 Codex 用量，执行前再次向用户确认。
3. 确认关闭桌面应用后，GitHub Action 仍可运行，事件任务仍出现在 ChatGPT Scheduled 中。
4. 保留 canary PR 作为审计记录，并确认真实 episode key 仍可独立触发；需要清理时先暂停事件任务，再关闭 PR。GitHub 不提供普通的删除 PR 操作。

## 11. 可观测性与回滚

GitHub Actions 日志记录每次检查的时间、数据新鲜度、分数、episode key 和“触发/跳过”原因，不记录任何账户或登录信息。

回滚顺序：

1. 暂停 ChatGPT GitHub 事件任务；
2. 禁用 GitHub Actions workflow；
3. 关闭触发 PR；
4. 如不再使用，断开 ChatGPT 对专用仓库的授权；删除仓库属于独立破坏性操作，需再次确认。

## 12. 验收标准

- 电脑和手机均离线时，GitHub 探针仍按五分钟周期运行。
- 符合所有保护条件且新 episode 首次被观察到至少 30% 时，只创建一个触发 PR。
- 相同事件与 24 小时冷却内不重复触发。
- ChatGPT 事件任务收到测试 PR 后只启动一次云端事件运行、输出固定回执并立即结束。
- 不存在 OpenAI API key、Gmail 密钥、PAT 或其他长期密钥。
- 无效、陈旧或语义不一致的数据绝不触发。
- 文档明确说明窗口重锚与“两段五小时”均无法由公开接口保证。

## 13. 参考资料

- [OpenAI：Codex/Work 定价与五小时共享窗口](https://learn.chatgpt.com/docs/pricing#what-are-the-usage-limits-for-my-plan)
- [OpenAI：Scheduled tasks 与 GitHub PR 事件触发](https://learn.chatgpt.com/docs/automations#trigger-tasks-from-app-events)
- [GitHub：Workflow schedule 与最短五分钟周期](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule)
- [GitHub：GITHUB_TOKEN 权限与事件行为](https://docs.github.com/en/actions/concepts/security/github_token)
- [GitHub：仓库 Actions 权限设置](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)
- [GitHub：公开仓库标准运行器免费](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
