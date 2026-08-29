# 新知识架构本地人工测试说明书

> 适用版本：2026-08-27 新知识入库、测评画像与可溯源 RAG 架构
>
> 项目根目录：下文命令均从克隆后的仓库根目录执行
>
> Python 环境：先激活项目专用的 `course-insight` Conda 环境

## 1. 本说明书测试什么

本说明书用于从用户视角验证以下完整流程：

```text
登录与权限
  → 配置 DeepSeek
  → 上传任意数量的混合格式知识文件
  → 上传固定格式题目 TXT
  → 确认处理并观察后台进度
  → 验证多知识点、来源文件和题目标注
  → 新增文件并重新发布
  → 编辑题目并重新标注
  → 删除文件并验证知识、检索和题目标注同步更新
  → 学生进行有来源的课程答疑
  → 验证任务恢复、历史版本和 M4–M9 兼容
```

浏览器主要验证 M0、M1、M3 和 M7 的用户流程。M2、M4、M5、M6、M8、M9 中没有独立人工输入页面的内部数据传递，通过本说明书给出的定向自动测试验证。

## 2. 当前本机基线

编写本说明书时已经确认：

- Django migration `0001`–`0014` 均已应用；
- 角色权限已同步；
- 数据库中有 8 个测试账号；
- 从零测试前可用 `reset_learning_test_state` 清理指定课程、班级的课程文件、题目、测评投影和学生画像；
- DeepSeek 密钥由教师按课程号、班级号配置；知识抽取、题目标注和学生答疑均不回退到全局密钥；
- 自动测试已覆盖带假传输的成功答疑、引用校验和 HTML 转义。

如果你的数据库计数已经改变，不要把本说明书中的版本号或数量照搬为固定值，应比较每一步操作前后的变化。

## 3. 测试材料

### 3.1 新架构专用材料

目录：

```text
examples/new_architecture_manual_test/
```

用途如下：

| 文件 | 用途 |
|---|---|
| `knowledge/01_congestion_primary.md` | 第一份拥塞控制、拥塞窗口、慢启动、流量控制来源 |
| `knowledge/02_congestion_secondary.txt` | 第二份拥塞控制和慢启动来源，用于来源合并与删除保留测试 |
| `knowledge/03_congestion_avoidance_addition.md` | 后续新增拥塞避免、AIMD 知识点 |
| `questions/questions_valid.txt` | 选择、填空、主观三类合法题目 |
| `questions/questions_valid_v2.txt` | 编辑后的四题版本，用于题目版本和重新标注测试 |
| `questions/questions_partial_invalid.txt` | 两道合法题加一道缺少 rubric 的坏题，用于单题隔离 |

### 3.2 混合格式完整课件

目录：

```text
examples/five_day_acceptance/courseware/
```

包含正常 MD、TXT、DOCX、PDF、PPTX：

```text
01_network_layers.md
02_ip_addressing.txt
03_transport_protocols.docx
04_performance_metrics.pdf
05_fault_diagnosis.pptx
```

旧版 PPT 使用本机演示课件：

```text
<本地课件目录>\*.ppt
```

处理旧 PPT 的 ingestion Worker 所在 Windows 主机必须安装 Microsoft PowerPoint；Web
服务器本身不依赖 PowerPoint。系统会把旧 PPT 转换为临时 PPTX 后解析，列表、学生引用和
数据库来源仍显示原始 `.ppt` 文件名。

### 3.3 非法文件

目录：

```text
examples/five_day_acceptance/invalid_fixtures/
```

优先测试：

- `corrupt.pptx`：损坏的 OOXML；
- `empty.md`：空文件；
- `invalid_utf8.txt`：非法 UTF-8；
- `unsupported_legacy.ppt`：伪造或损坏、没有 OLE 文件头的旧 PPT；
- `corrupt.pdf`：损坏 PDF；
- `scanned_no_text_layer.pdf`：需要 OCR 的扫描 PDF。

## 4. 测试账号

账号清单位于：

```text
examples/five_day_acceptance/accounts/account_roles.csv
```

密码只从以下被 Git 忽略的本地文件读取，不要把密码复制进截图、测试报告或提交记录：

```text
runtime/five_day_acceptance/credentials.txt
```

本说明书主要使用：

| 账号 | 用途 |
|---|---|
| `pseudonym_teacher_001` | 管理 `course_network` 的知识文件和 DeepSeek 配置 |
| `pseudonym_student_mid` | 访问 `course_network/class_01` 并测试学生答疑 |
| `pseudonym_student_unauthorized` | 验证跨课程拒绝 |
| `pseudonym_course_admin_001` | 验证课程管理员权限 |
| `pseudonym_system_admin_001` | 验证系统管理员权限 |
| `pseudonym_teacher_revoked` | 已撤销账号，应无法正常使用授权功能 |

登录地址是：

```text
http://127.0.0.1:8000/accounts/login/
```

连续输错密码会触发登录限流。人工测试时不要对同一账号连续尝试五次错误密码。

## 5. 启动前检查

打开 PowerShell，进入项目目录：

```powershell
Set-Location -LiteralPath '<仓库根目录>'
$python = (Get-Command python).Source
& $python --version
& $python manage.py check
& $python manage.py makemigrations --check --dry-run
& $python manage.py showmigrations m0_platform_web
& $python manage.py sync_roles --apply
```

通过标准：

- Python 是 3.12.x；
- `manage.py check` 为 `0 silenced`；
- migration 显示 `0001`–`0014` 都是 `[X]`；
- `makemigrations` 显示 `No changes detected`；
- `sync_roles` 不报权限或 CSV 错误。

检查数据包：

```powershell
& $python -m scripts.verify_five_day_acceptance --pack examples/five_day_acceptance
```

检查新架构题目文件：

```powershell
& $python -c "from pathlib import Path; from course_insight.modules.m3_knowledge_bundle.question_files import parse_question_file; root=Path('examples/new_architecture_manual_test/questions'); [(lambda r: print(p.name, len(r.questions), [(i.code,i.locator) for i in r.issues]))(parse_question_file(p.name,p.read_text(encoding='utf-8'))) for p in sorted(root.glob('*.txt'))]"
```

预期：

```text
questions_partial_invalid.txt 2 [('QUESTION_RUBRIC_REQUIRED', 'lines:12-18')]
questions_valid.txt 3 []
questions_valid_v2.txt 4 []
```

## 6. 启动三个进程

### 6.1 终端 A：Web

```powershell
Set-Location -LiteralPath '<仓库根目录>'
python manage.py runserver 127.0.0.1:8000
```

### 6.2 终端 B：知识入库 Worker

```powershell
Set-Location -LiteralPath '<仓库根目录>'
python manage.py run_ingestion_worker
```

它负责文件解析、长文本切割、DeepSeek 知识抽取、题目标注和活动版本发布。

### 6.3 终端 C：outbox Worker

```powershell
Set-Location -LiteralPath '<仓库根目录>'
python manage.py run_outbox_worker
```

它负责既有学习事件投递。只测试知识上传时可以暂时不启动，但测试 M4–M9 完整学习流程时必须启动。

### 6.4 检查健康状态

浏览器打开：

```text
http://127.0.0.1:8000/health/live/
http://127.0.0.1:8000/health/ready/
```

预期：

- live 返回 `{"status":"live"}`；
- 三个进程都运行时，ready 应为 `ready`；
- 关闭终端 B 后，ready 应变成 HTTP 200 + `degraded`；
- `web_auth`、`course_runtime` 仍为 `ready`；
- `knowledge_ingestion` 变为 `not_ready`；
- 登录仍然成功，不应返回 503。

如果 ready 为 `not_ready`/503，应先检查 `config`、`database`、`migrations`、`runtime`、`logging`，不要继续测试上传。

## 7. 测试 DeepSeek 配置

1. 使用 `pseudonym_teacher_001` 登录。
2. 在教师首页打开“DeepSeek 接口”。
3. 地址也可直接打开：

```text
http://127.0.0.1:8000/teacher/courses/course_network/classes/class_01/deepseek/
```

4. 页面必须只显示掩码，不得显示完整密钥。
5. 保存后，`course_network/class_01` 的教师和学生共同使用该作用域配置；其他课程或班级不能读取或借用。
6. 不要把真实密钥复制到测试报告。

可在终端执行安全状态检查，命令不会输出密钥：

```powershell
& $python manage.py deepseek_status
```

该命令只读取旧 M7/M9 全局适配器的安全状态，不会初始化第二套 Web Runtime；它不能代替课程、班级页面对作用域密钥的检查。不要改用 `runtime.get_web_runtime()` 做状态检查；文件日志采用单写入者保护，第二个独立进程尝试占用 `app.log` 时会正确返回 `LOG_SINK_IN_USE`。

知识抽取、题目标注和独立学生答疑都要求当前课程号、班级号下存在有效教师密钥。学生问句仍先经过确定性脱敏和出站复核，但独立答疑不依赖旧主观评分所需的钉住语义隐私模型。

## 8. 场景 A：登录、菜单和权限

### A1 教师正常入口

1. 使用教师账号登录。
2. 教师首页应显示“课程知识与题目文件”。
3. 点击“管理课程 course_network”。
4. 页面地址应为：

```text
http://127.0.0.1:8000/teacher/courses/course_network/knowledge/
```

通过标准：页面不是 404，包含上传、文件列表、确认处理三部分。

### A2 未授权学生

1. 退出教师账号。
2. 使用 `pseudonym_student_unauthorized` 登录。
3. 尝试打开：

```text
http://127.0.0.1:8000/student/courses/course_network/classes/class_01/qa/
```

通过标准：返回 403，不显示课程文件名、知识点或其他学生信息。

### A3 旧审核入口

教师登录后打开：

```text
http://127.0.0.1:8000/teacher/knowledge-reviews/
```

预期返回 410。再打开：

```text
http://127.0.0.1:8000/teacher/knowledge-reviews/?course_id=course_network
```

预期重定向到新的课程知识文件页，不再要求审核 ID。

## 9. 场景 B：首次上传与活动版本发布

### B1 上传两份知识来源

1. 教师进入课程知识文件页。
2. 文件用途选择“知识文件”。
3. 一次选择：

```text
examples/new_architecture_manual_test/knowledge/01_congestion_primary.md
examples/new_architecture_manual_test/knowledge/02_congestion_secondary.txt
```

4. 点击“上传到待处理列表”。

预期：

- 页面提示成功加入 2 个文件；
- 两个文件状态为待处理；
- 尚未点击确认时，学生仍看不到新的活动发布版本。

### B2 上传题目文件

1. 文件用途改为“题目文件”。
2. 上传：

```text
examples/new_architecture_manual_test/questions/questions_valid.txt
```

预期：文件列表显示题目文件，并出现“编辑题目 TXT”入口。

### B3 确认处理

1. 勾选“确认按当前文件列表重新解析并发布”。
2. 点击“确认处理”。
3. 观察进度条。

正常阶段依次为：

```text
reading_sources
sources_parsed
extracting_concepts
concepts_extracted
concepts_merged
linking_questions
questions_linked
publishing
published
```

进度采用真实工作量计算：读取阶段按文件数，知识提取阶段按已完成文本批次和可见字符数，题目标注阶段按题目数。知识提取主要按 6,000 个非空白字符组批；默认不以 chunk 数提前截断，仍保留可显式启用的 4,096 块安全上限。等待单次 DeepSeek 返回时，进度条显示活动动画、当前批次和本步骤等待秒数，但不会按时间伪造百分比。输出校验失败时页面显示当前第几次重试，重试不虚增字符；若当前批被自动拆小，每个成功子批才立即增加已完成字符数。

通过标准：

- 最终状态为 `succeeded`；
- 如果存在单题解析问题或单文件失败，可以是 `partially_succeeded`，但必须有新的完整活动版本；
- 状态不能永久停留在 `queued` 或 `running`；
- 终端 B 不应输出完整文件正文、完整 prompt 或 API 密钥。

## 10. 用终端检查发布结果

以下命令只读取当前活动版本。

### 10.1 发布版本摘要

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import CourseKnowledgeRelease as R; r=R.objects.get(course_id='course_network',status='active'); print({'release_id':str(r.pk),'version':r.version_number,'concepts':r.concepts.count(),'questions':r.questions.count(),'sources':sum(c.source_references.count() for c in r.concepts.all()),'links':sum(q.concept_links.count() for q in r.questions.all())})"
```

通过标准：

- 只有一个 `active` release；
- concepts、questions、sources、links 都大于 0；
- questions 应为 3；即使 DeepSeek 没有给某题任何可用知识标签，该题仍会保存在 release 中，但不会进入旧 M4–M9 题卡投影。

### 10.2 知识点和来源文件

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import CourseKnowledgeRelease as R; r=R.objects.get(course_id='course_network',status='active'); [(print('\nCONCEPT',c.name,c.concept_id),[print('  SOURCE',s.source_version.source.display_name,s.locator,s.relation_type) for s in c.source_references.select_related('source_version__source').all()]) for c in r.concepts.all()]"
```

重点检查“拥塞控制”和“慢启动”相关知识点。

通过标准：

- 同一个合并知识点可以同时列出 `01_congestion_primary.md` 和 `02_congestion_secondary.txt`；
- 合并不能只留下其中一个来源；
- locator 非空；
- 来源身份包含文件版本和 chunk，不引用待删除或其他课程文件。

由于模型输出存在合理措辞差异，不要求概念数量和名称逐字固定。但如果两个文件中的同一概念被重复建立，或者合并后丢失一个来源，应记录为失败。

### 10.3 题目关联

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import CourseKnowledgeRelease as R; r=R.objects.get(course_id='course_network',status='active'); [(print('\nQUESTION',q.question_id,q.question_type,q.stem),[print('  LINK',l.concept.name,float(l.confidence),l.status) for l in q.concept_links.select_related('concept').all()]) for q in r.questions.all()]"
```

通过标准：

- `tcp-choice-001` 应关联慢启动或拥塞窗口；
- `tcp-fill-001` 应关联拥塞窗口；
- `tcp-subjective-001` 应同时关联流量控制与拥塞控制相关知识点；
- link 的 concept 必须属于同一活动 release；
- 不允许模型创造活动概念集合之外的编号。

## 11. 场景 C：混合格式上传

文件用途选择“知识文件”，一次选择 `courseware/` 下五个文件：MD、TXT、DOCX、PDF、PPTX；如需验证旧格式解析，再从本地课件目录选择一个真实 `.ppt`。

通过标准：

- 六个文件（包含旧 PPT）可在同一表单上传；
- 文件数不是固定的 5，少传或多传都不会因为数量报错；
- 点击确认后生成比上一版本号更大的 release；
- PDF 来源 locator 包含页码，PPT/PPTX 来源 locator 包含 slide，文本文件包含 paragraph/lines；
- 之前发布的 release 变为 `retired`，但记录仍存在。

版本检查：

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import CourseKnowledgeRelease as R; print(list(R.objects.filter(course_id='course_network').order_by('version_number').values_list('version_number','status','release_id')))"
```

## 12. 场景 D：非法文件和同批隔离

### D1 损坏 PPTX 与合法文件同批上传

文件用途选择“知识文件”，同时选择：

```text
examples/five_day_acceptance/invalid_fixtures/corrupt.pptx
examples/new_architecture_manual_test/knowledge/03_congestion_avoidance_addition.md
```

预期：

- `corrupt.pptx` 在上传边界被拒绝，并显示安全、简短的错误；
- 合法 MD 仍进入待处理列表；
- 不显示 ZIP 内部路径、服务器绝对路径或异常堆栈；
- 点击确认后，合法文件仍可发布。

### D2 其他非法格式

分别尝试空 MD、非法 UTF-8、伪造或损坏的 `.ppt` 和损坏 PDF。

通过标准：每个文件独立拒绝，其他已上传文件不丢失，Web 和 Worker 继续运行。

## 13. 场景 E：长文本切割

### E1 无 API 成本的确定性验证

```powershell
& $python -m pytest tests/unit/test_m1_long_text_chunking.py -q -p no:cacheprovider --basetemp outputs/tmp/manual-long-chunk
```

该测试验证：

- 默认阈值 6,000 个非空白 Unicode 字符；
- 先按段落切；
- 单段过长时优先按句末切；
- 没有句末时按弱标点/空白切；
- 最后按字符硬切；
- locator 保留原始字符范围。

### E2 浏览器真实 API 验证

完整长资料可使用：

```text
examples/five_day_acceptance/official_sources/rfc9293.txt
```

这是高成本场景，可能产生数十次 DeepSeek 请求。先确认账户余额和调用预算，再上传并确认。

通过标准：

- 任务不因文本长而出现请求尺寸错误；
- 进度持续更新；
- 最终来源 locator 中可出现 `segment:` 和 `chars:`；
- 如果某批结构或溯源无效，系统会先对同一批额外重试 5 次；仍失败、恰好返回 100 个知识点或返回 `DEEPSEEK_INCOMPLETE_RESPONSE` 时，系统会自动二分并重新分析；
- 二分后生成的知识点仍能保存对应 chunk 文本和来源，不出现发布 KeyError。

## 14. 场景 F：新增知识文件与全量题目标注

1. 上传：

```text
examples/new_architecture_manual_test/knowledge/03_congestion_avoidance_addition.md
```

2. 再次确认处理。
3. 检查新 release。

通过标准：

- 新 release 版本号增加；
- 出现拥塞避免、加性增大或 AIMD 相关知识点；
- 所有活动题目都重新标注，不只是新上传的题目；
- 旧 release 保持 retired，不被修改。

## 15. 场景 G：编辑题目文件

1. 在文件列表找到 `questions_valid.txt`。
2. 点击“编辑题目 TXT”。
3. 打开本地 `questions_valid_v2.txt`，复制全部内容到编辑框。
4. 保存。
5. 返回文件页，再次确认处理。

通过标准：

- 原题目来源生成新版本，不覆盖旧版本；
- 活动 release 中包含 4 道题；
- 新主观题关联流量控制、慢启动和拥塞避免；
- `tcp-fill-aimd-001` 关联 AIMD 或加性增大/乘性减小知识点；
- 历史 release 仍保留旧三题内容。

检查题目来源版本：

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import CourseSource as S; s=S.objects.get(course_id='course_network',display_name='questions_valid.txt'); print(list(s.versions.order_by('version_number').values_list('version_number','status','sha256')))"
```

## 16. 场景 H：坏题隔离

上传 `questions_partial_invalid.txt` 并确认处理。

预期：

- 作业状态为 `partially_succeeded`；
- `tcp-valid-sibling-001` 和 `tcp-valid-sibling-002` 进入活动 release；
- `tcp-broken-001` 不进入活动 release；
- 其他题目和知识点不受影响；
- 活动 release 仍完整可读。

检查：

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import CourseKnowledgeRelease as R; r=R.objects.get(course_id='course_network',status='active'); print(list(r.questions.order_by('question_id').values_list('question_id',flat=True)))"
```

## 17. 场景 I：批量删除和来源保留

### I1 删除第一份来源

1. 勾选 `01_congestion_primary.md`。
2. 点击“将选中文件加入待删除列表”。
3. 文件应立即从教师文件列表消失；此时它只是 `pending_delete`，学生仍使用旧活动版本。
4. 点击“确认处理”。

通过标准：

- 生成新 release；
- 新 release 不再引用 `01_congestion_primary.md`；
- “拥塞控制”和“慢启动”因为还有 `02_congestion_secondary.txt` 来源而保留；
- 来源集合只剩仍活动的文件；
- 题目关联重新计算，仍指向这些保留知识点；
- 文件变为最终 `deleted`，后续确认任务不会重复包含它。

### I2 删除第二份来源

再删除 `02_congestion_secondary.txt` 并确认。

通过标准：

- 如果某知识点已经没有任何其他活动来源，该知识点不进入新 release；
- 每道题目的知识点集合同步缩水，只移除已经消失的知识点关联，其他知识点关联继续保留；
- 仍由其他课件支持的知识点继续保留；
- 学生检索不再返回两个已删文件；
- 历史 release 仍可查到原来源，用于审计。

检查活动来源文件：

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import CourseKnowledgeRelease as R; r=R.objects.get(course_id='course_network',status='active'); print(sorted(set(s.source_version.source.display_name for c in r.concepts.all() for s in c.source_references.select_related('source_version__source').all())))"
```

## 18. 场景 J：学生可溯源答疑

### J1 无课程证据时外部检索与警告

使用 `pseudonym_student_mid` 登录，打开：

```text
http://127.0.0.1:8000/student/courses/course_network/classes/class_01/qa/
```

输入明显不属于课程的问题，例如：

```text
量子纠缠的贝尔不等式是什么？
```

预期：DeepSeek 可自行检索回答，但页面必须明确显示“无课程知识点支撑”以及“建议核对事实”等警告；不得伪造课程文件引用，也不得引用已经删除的文件。

### J2 有证据课程问题

输入：

```text
慢启动和拥塞避免的窗口增长方式有什么区别？
```

当前课程、班级已配置教师密钥时，通过标准是：

- 返回简明回答；
- 每条事实主张引用当前检索证据；
- 来源卡显示文件名和 locator；
- 不引用已删除文件；
- 不显示模型系统提示或内部 ID 堆栈。

成功答疑路径可用假传输验证：

```powershell
& $python -m pytest tests/unit/test_m7_student_qa.py tests/integration/test_django_student_qa.py -q -p no:cacheprovider --basetemp outputs/tmp/manual-student-qa
```

浏览器真实答疑是否可用以当前课程、班级的教师密钥为准；删除或清空该配置后必须立即变为不可用，不能回退到环境变量或其他班级密钥。

### J3 跨课程拒绝

使用未授权学生访问 `course_network/class_01` 答疑页，应返回 403。学生不能通过修改 URL 读取其他课程来源。

## 19. 场景 K：Worker 停止、恢复和幂等

### K1 Worker 停止

1. 停止终端 B。
2. 上传一个合法文件并确认。

预期：

- Web 登录和文件页面仍可使用；
- 作业保持 queued；
- readiness 为 degraded 而不是 503；
- 重新启动终端 B 后作业继续处理。

### K2 崩溃恢复

在任务进入 extracting 阶段后强制关闭终端 B，等待租约过期后重新启动 Worker。

通过标准：

- 过期 running 作业可被重新认领；
- 不生成两个活动 release；
- 最终要么发布完整新版本，要么保持旧活动版本；
- 不出现半成品活动版本。

该等待场景可先用快速自动测试验证：

```powershell
& $python -m pytest tests/unit/test_m0_ingestion_worker_recovery.py tests/integration/test_knowledge_ingestion_pipeline.py -q -p no:cacheprovider --basetemp outputs/tmp/manual-worker-recovery
```

### K3 重复确认

同一待处理列表重复提交时，变更集校验和应复用同一个任务或同一发布结果，不能无限创建内容完全相同的活动版本。

## 20. 场景 L：M4–M9 下游兼容

运行：

```powershell
& $python -m pytest tests/integration/test_knowledge_release_downstream.py tests/unit/test_m5_partial_state_update.py tests/unit/test_m8_weighted_blueprint.py tests/unit/test_follow_up_bundle.py -q -p no:cacheprovider --basetemp outputs/tmp/manual-downstream
```

它验证：

- M4 冻结新 release UUID，但 `TaskPlan` 公共字段不变；
- M5 只消费可用题目—知识点关系；
- M6 继续使用原有状态机和冻结任务；
- M8 新试卷使用新 release，历史试卷保持旧 checksum；
- M9 读取知识点、来源、题目和关联质量，不恢复旧知识审批状态。

完整回归：

```powershell
& $python -m pytest -q -p no:cacheprovider --basetemp outputs/tmp/manual-full-suite
```

最近一次本地完整回归基线（2026-08-27）是 `2506 passed, 26 skipped`。26 个 skip 是需要受保护 PostgreSQL、真实隐私工件等外部条件的 live 测试，不能把 skip 记为生产通过。

## 21. 每一步应保存的证据

建议为每个场景记录：

| 字段 | 示例 |
|---|---|
| 场景 ID | B3 |
| 操作时间 | 2026-08-25 15:30 +08:00 |
| 使用账号 | pseudonym_teacher_001 |
| job ID | 页面 URL 中 UUID |
| release ID/版本 | 终端摘要输出 |
| 结果 | 通过/失败/阻断 |
| 截图 | 页面、进度、来源卡，不含密钥和密码 |
| 实际错误码 | 如 `QUESTION_RUBRIC_REQUIRED` |
| 备注 | 模型措辞差异、耗时、API 调用次数 |

不要在报告中保存：

- DeepSeek 原始密钥；
- 测试账号密码；
- 完整 prompt 或完整模型响应；
- 本机绝对秘密路径；
- 真实学生数据。

## 22. 最终通过标准

新架构人工验收通过需要同时满足：

- [ ] 教师可进入课程知识文件页，不再使用审核 ID；
- [ ] 可以上传任意数量和混合格式知识文件；
- [ ] 文件数量不足或超过旧固定数量不会导致程序报错；
- [ ] 真实旧 PPT 可转换解析；假 PPTX、伪造 PPT、空文件和非法编码在边界被安全拒绝；
- [ ] 一个坏文件或坏题不破坏合法兄弟文件/题目；
- [ ] 长文本按阈值切割，locator 可追踪；
- [ ] 一次分析可保存多个知识点；
- [ ] 合并概念时保留全部来源文件索引；
- [ ] 选择、填空、主观题均可解析和自动关联多个知识点；
- [ ] 新增知识文件产生新知识点 ID 时全部活动题目重新标注；仅增加已有知识点来源时只在本地刷新证据；
- [ ] 删除知识来源后活动检索、知识点和题目标注同步更新；
- [ ] 历史 release 和历史试卷不被破坏；
- [ ] 学生问题可匹配零到多个知识点；课程证据不足时允许外部检索并明确警告；
- [ ] 课程有证据时，学生回答显示文件名、定位和对应原文；
- [ ] 当前课程、班级未配置教师密钥时答疑不可用，且不回退到全局或其他作用域；
- [ ] 诊断测评和阶段评测更新画像，随心练习、订正和答疑不更新画像；
- [ ] 学生可查看本人画像型历史试卷的 ID、得分、错误情况和逐题完整反馈；
- [ ] 逐题反馈包含选择题选项、答案、知识点、来源原文和自动发问的答疑入口；
- [ ] Worker 停止只导致能力降级，不阻断登录；
- [ ] Worker 恢复后 queued/过期 running 任务可以继续；
- [ ] 任意时刻每门课程最多一个活动 release；
- [ ] M4–M9 兼容测试全部通过；
- [ ] 全量测试无失败。

## 23. 常见问题定位

### 23.1 登录返回 503

访问 `/health/ready/`。如果 `web_auth=not_ready`，检查数据库、迁移、配置和日志；不要只重启 outbox Worker。

### 23.2 作业一直 queued

知识 Worker 没有运行。启动：

```powershell
& $python manage.py run_ingestion_worker
```

Worker 会在每个任务开始时重新读取教师保存的模型和 Thinking 设置，不需要为配置变更重启 Worker。已经进入 DeepSeek 单次请求的任务仍使用该请求开始时的配置。

如果页面显示“后台处理进程已停止，等待新的处理进程接管”，说明数据库任务仍可恢复，但 Worker 已退出或任务租约已经过期。重新运行上述命令后，Worker 会安全接管，不会提前替换当前活动知识包。

### 23.3 Worker 提示已存在

错误中会显示安全 PID 和 heartbeat。先检查该 PID 是否确实存在。不要直接删除活动进程的锁文件；确认进程已经退出后再重启命令。

### 23.4 作业 failed

检查页面 error code、终端 B 和以下只读摘要：

```powershell
& $python manage.py shell -c "from course_insight.modules.m0_platform.django_app.models import KnowledgeIngestionJob as J; print(list(J.objects.filter(course_id='course_network').order_by('-created_at')[:5].values('job_id','status','progress','checkpoint','error_code','worker_id','lease_until')))"
```

常见原因包括密钥缺失、模型不可用、DeepSeek JSON 不符合约束、文件内容为空或没有任何可溯源知识点。

当前实现还有以下恢复规则：

- DeepSeek 不再计算容易出错的字符起止位置，只能逐字返回 `quote` 原文证据。系统在对应 `chunk_id` 的原文中查找该引用并生成 `span_start/span_end`；引用不存在、为空或指向未知 chunk 时仍然拒绝。
- 知识文本主要按 6,000 个非空白字符组批，默认不以 chunk 数提前截断，显式安全上限为 4,096 块。模型输出无效时，同一批额外重试 5 次，并在提示中携带安全校验代码；仍无效或明确返回 `DEEPSEEK_INCOMPLETE_RESPONSE` 时，系统才把当前批递归缩小，最多 8 层。最小文本也要完成 5 次重试才结束任务。
- 教师开启 Thinking 后，每一次 DeepSeek 请求使用 16384 输出 token 和 120 秒请求超时。若遇到网络超时、服务暂不可用、响应不完整或响应 JSON 损坏，系统会对同一批内容自动切换到普通模式再处理；鉴权失败、密钥缺失和内容过滤不会自动降级绕过。教师关闭 Thinking 时，每一次 DeepSeek 请求使用 4096 token 和 30 秒请求超时。这里的 30/120 秒都不是批次、全部重试或整项任务的累计处理上限。
- 若主调用和自动降级都失败，网络错误会直接显示安全细分错误码；响应不完整则先执行上述缩小重试，只有最小文本仍失败才显示 `DEEPSEEK_INCOMPLETE_RESPONSE`。系统不保存或展示密钥、提示词和模型完整回复。旧失败任务仍保留当时的原始外层错误码。
- 常驻 Worker 中某个任务失败时，只输出安全错误码并继续处理后续任务；使用 `--once` 调试时仍会以非零状态退出，便于脚本发现问题。
- 失败任务作为历史记录保留，并保留失败前最后一次真实进度，不再强制显示 100%。修复原因后可以再次点击“确认处理”，系统会用同一批暂存文件创建新的独立任务，不会复用或覆盖旧失败任务。

### 23.5 学生有证据问题仍提示暂不可用

先打开第 7 节所示的精确课程、班级配置页，确认教师密钥已保存且状态有效。独立答疑不使用全局密钥，也不会借用其他班级的配置；修改后重新打开答疑页检查状态。

### 23.6 概念存在但没有进入 M8 题库

检查该题是否至少有一个 `status=usable` 的知识点关联。无可用关联的题目会保存在 release 中供质量检查，但不会投影成旧 M4–M9 的可出卷 `ItemCard`。

### 23.7 删除后旧来源仍能在数据库找到

先确认查询的是 `status=active` 的 release。旧来源应在历史 retired release 中保留，这是审计和历史试卷可复现要求；它不应出现在当前活动检索和学生答疑中。

## 24. 当前已知测试边界

1. 本地默认使用 SQLite；PostgreSQL+pgvector 的生产性能需要独立 live 环境验证。
2. DeepSeek 输出可能在概念名称和粒度上有合理变化，应验收来源完整性和业务不变量，不要求固定概念数量。
3. 当前教师 UI 直接展示文件和作业进度；知识点、来源并集和题目关联的详细检查使用本说明书中的只读终端命令。
4. 知识文件通过“上传新文件 + 删除旧文件”调整；只有题目 TXT 提供文本编辑页。
5. 浏览器真实学生答疑会产生实际 DeepSeek 调用，只有当前课程、班级已配置教师密钥时才能测试；自动测试使用假传输，不消耗真实 API。
6. 完整 RFC 长文本测试会产生实际 API 成本，先做单元验证，再决定是否运行浏览器高成本场景。
