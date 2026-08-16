# M1–M6、M8 阶段补齐设计

## 目标

在已合并的 M1–M3 S1–S6 生产基线之上，补齐上一轮审计发现的非 M7/M9 待办项：教师知识包审核页面、PDF 输入边界、可重复的性能与生产联调验证，以及 M6 受控运行验证记录。

## 范围与排除

- 实现 M3 S4 的教师知识包审核 Web 流程。
- 保留 M0/M8/M9 评分复核页面，不将其改造成 M3 页面。
- 为 M1 提供可插拔 OCR 端口：文本层 PDF 继续使用 pypdf；扫描 PDF 在配置 OCR 后提取文字；没有 OCR 时返回明确、可恢复的错误。
- 为 M2 增加目标规模基准配置、结果落盘和验收测试；不在本阶段直接启用 ANN 索引，索引选择以基准结果为依据。
- 为 M6 增加受控验证命令与结构化记录，默认仍保持 rules、零 rollout、零探索。
- 增加生产联调检查清单和可重复的 disposable PostgreSQL/pgvector 验证入口。
- M7 与 M9 的 DeepSeek、主观评分、反馈生成、教师叙述以及 M6 OPE 接入 M9 均不实现、不修改。

## 设计

### M3 Web 审核

新增教师知识包审核路由，页面只负责展示状态、版本、校验报告摘要和审核操作。每个 POST 请求使用 Django CSRF、现有教师作用域授权和一次性 flow token；服务层调用 `AppCoordinator` 的 M3 CAS facade，不直接访问仓储。批准调用 `build_knowledge_bundle_after_approval` 所需的 review id/version，过期页面返回稳定错误，不覆盖新版本。

页面沿用现有 Django 模板体系，只加入深圳大学官网的基础视觉特征：深红色顶部线条、白色页眉、简洁横向导航、浅灰页面背景、红色标题和无圆角的细边框卡片。页面不复制校徽、图片轮播或官网具体内容。

### OCR

M1 parser registry 增加 `.pdf` 解析器和 OCR provider 端口。解析顺序为：先提取 PDF 文本层；若所有页面均无文字且 OCR provider 已配置，则把页面交给 OCR provider；未配置时抛出 `COURSE_PDF_OCR_REQUIRED`，不生成空的成功课程包。OCR provider 的输入输出均为内存中的页面文本，测试使用确定性 fake provider，生产实现由配置注入，避免强制安装系统级 OCR 程序。

### 性能与生产验证

M2 基准 CLI 接受真实 chunk 数、embedding 维度、查询数、top-k 和阈值，输出 JSON，包含规模、P50/P95、Recall@K、构建时间、关系体积和 explain。新增目标规模 profile 只生成配置和验收报告，不在普通单元测试中启动大规模任务。生产联调命令只接受名字带 `test`、`ci` 或 `tmp` 的 disposable 数据库，并在 cleanup 时校验数据库身份。

M6 验证命令按 rules → shadow → OPE → 人工批准 → 小流量 active → kill switch/rollback 顺序记录结果；任何缺少真实数据的步骤返回 `blocked`，不伪造通过。

## 验收标准

1. M3 教师可从 Web 页面完成 draft、submit、approve、reject、recall，非法版本不能写入。
2. 文本层 PDF 可导入；扫描 PDF 无 OCR 时明确失败，有 OCR provider 时产生非空、可定位 chunks。
3. M2 目标规模命令输出可审计 JSON；小规模和目标 profile 测试覆盖阈值、失败和清理路径。
4. M6 验证缺少真实教学数据时为 blocked，默认配置不改变。
5. 生产联调检查只使用 disposable 数据库，迁移、恢复、health/readiness 和 cleanup 全部有结果。
6. M7/M9 代码和契约保持不变。
