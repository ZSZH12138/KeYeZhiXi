# 架构说明

## 架构定位

课业智析保持 M0—M9 十个责任域，不新增 M10。核心是进程内的 Python
模块化单体：模块之间传递 Pydantic 契约对象，不用内部 HTTP，
`AppCoordinator` 只编排公开服务，不直接读任何模块的业务表。

Django 是 M0 的系统外层，负责页面、会话、鉴权和表单入口；它不是
独立业务模块。目标持久化是 PostgreSQL，M2 的目标向量检索后端是
pgvector。LLM 只允许通过 DeepSeek API 边界进入 M7 与 M9。

当前交付是“可运行架构 + 空实现”：不启动 Django 服务，不连接
PostgreSQL/pgvector，不读取或调用 DeepSeek API，不执行 DINA/BKT/IRT
计算。所有新入口都返回显式 `empty`/`skipped`/`insufficient_data`
结果，用于先锁定多人开发边界。

## M0—M9 能力归属

```text
M0 Django 外层/鉴权/提交契约
           │
           ▼
M1 课程版本与分块 ──► M2 RAG（词法 + pgvector 目标）──┐
           │                                        │
           └──► M3 知识包/Q 矩阵/题目标定依据         ▼
                         │                       M7 DeepSeek
                         ▼                       评分/反馈
                 M4 任务与版本编排                      │
                         │                             ▼
                         ▼                       M8 测评/评分/IRT/
                 M5 DINA 认知诊断 + BKT             自适应选题/在线标定
                         │                             │
                         ▼                             ▼
                 M6 诊断驱动的辅导状态机 ──► M9 DeepSeek 叙述/
                                                       模型质量/教师审核
```

| 模块 | 架构责任 | v2 新边界 | 当前行为 |
|---|---|---|---|
| M0 | 配置、事件、快照、Django 外层 | `ActorContext`、提交契约、`AsyncJobStatus` | Django 作业 `skipped` |
| M1 | 授权课程、来源版本、确定性分块 | 不增新智能引擎 | 保持现有实现 |
| M2 | 证据索引、RAG 检索与审计 | `EmbeddingModelRef`、`RetrievalPolicy`、`RetrievalAudit` | pgvector 引用为 `empty` |
| M3 | 知识包、题库、量规、蓝图、Q 矩阵 | 为 DINA/IRT 提供版本化标定依据 | 保持教师确认 JSON 输入 |
| M4 | 任务、蓝图与工作流编排 | 冻结下游策略版本 | 保持现有实现 |
| M5 | 学习观测、认知诊断、知识追踪、状态 | DINA 系契约与 BKT 系契约 | 空运行，不估计参数 |
| M6 | S0—S5 教学控制 | 消费 M5 诊断/追踪结果 | 保持现有确定性状态机 |
| M7 | 主观评分与学生反馈 | DeepSeek 唯一 LLM 适配器 | 返回空生成结果，零网络调用 |
| M8 | 测评、评分、IRT、自适应选题与在线标定 | IRT 参数、能力估计、标定与选题契约 | 空参数集与空选题 |
| M9 | 教师分析、质量门槛与复核 | DeepSeek 教师叙述、`ModelQualityReport` | 空生成，质量为 `insufficient_data` |

## 分层映射

| 路径 | 唯一职责 |
|---|---|
| `src/course_insight/contracts/` | 84 个公开 Pydantic 契约及来源图逻辑 |
| `src/course_insight/application/` | 通用业务与智能空结果编排 |
| `src/course_insight/modules/m0_*`—`m9_*` | 十个责任域的服务、仓储边界和可替换实现 |
| `src/course_insight/infrastructure/` | SQLite 基线、JSON/日志、DeepSeek 空适配器；未来放 PostgreSQL/pgvector 适配器 |
| `contracts/` | 84 份 Schema、1 份中性空示例与 `contract_provenance.json` |
| `data/raw_course/` | 本地授权原始资料占位；真实资料不进入可分发产物 |
| `runtime/` | 数据库、索引、快照和日志；不进入可分发产物 |

## 边界不变量

- 契约对象跨模块原样传递，不降级为临时字典。
- 时间必须带时区，校验和使用规范化 JSON 的 SHA-256。
- 外部身份只通过 M0 的伪匿名 `ActorContext` 进入核心。
- DeepSeek 的密钥只能在运行时由 `DEEPSEEK_API_KEY` 提供，契约、审计和日志不存密钥或完整提示词。
- DINA/BKT/IRT 和在线标定的每次运行都必须绑定数据水位、模型/参数版本与质量报告。
- 新标定参数先以 shadow 版本产生，经 M9 质量门槛与教师审核后才能被 M8 启用。
- 当前空实现不读取密钥、不访问网络、不连接数据库服务、不伪造模型指标。
- 量规、试卷、审计和结果总分必须守恒；教师复核追加新版本，不覆盖旧版本。
- 对外不传播主机路径；索引、作业和产物使用逻辑引用或相对路径。

## 扩展顺序

1. 先保持空实现的契约与编排边界稳定。
2. 在 M0 外层实现 Django 页面、权限和无路径表单契约，不改领域服务签名。
3. 将持久化适配器切换为 PostgreSQL，并在 M2 实现 pgvector 建库、检索与审计。
4. 在去标识化作答数据达到质量门槛后，于 M5 启用 DINA/BKT，于 M8 启用 IRT 影子标定和自适应选题。
5. 在 M7/M9 的安全、审计和量规约束完成后，将 DeepSeek 空适配器替换为真实 API 适配器。
