# M0/M4/M5/M6/M8 Merge Gates Implementation Plan

> **For agentic workers:** This plan is executed inline in the current session because the user explicitly prohibited multi-agent execution. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复合并后仅涉及 M0、M4、M5、M6、M8 的依赖、契约文档和 PostgreSQL live 验证门槛，并将验证结果同步到远端 `main`。

**Architecture:** 保持现有 M0/M4/M5/M6/M8 运行时代码和跨模块接口不变，只修正其可复现安装约束、CI 环境、公开教师复核契约说明，并让 CI 在一次性 PostgreSQL 服务上执行已有 live 测试。M2、M3、M7、M9 的实现和设计不在本次范围内。

**Tech Stack:** Python 3.11/3.12, pytest, PostgreSQL 16, GitHub Actions, Pydantic JSON Schema。

## Global Constraints

- 不修改 M2、M3、M7、M9 的实现或设计。
- 不改变 M0/M4/M5/M6/M8 的业务运行时逻辑；只修复依赖、CI、契约公开说明和验证配置。
- 不提交密钥；CI PostgreSQL 只使用一次性服务容器凭据。
- 提交前必须执行定向测试、全量测试、Schema 一致性、编译、依赖和 Git 空白检查。
- 只有验证通过后才提交并推送当前 `main`。

---

### Task 1: 固定 M4 intent 与 M5/M8 算法依赖

**Files:**
- Modify: `requirements/ci-constraints.txt`
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `pyproject.toml` optional groups `dev` and `intent`。
- Produces: 文档、CI 和约束文件对完整测试安装方式的一致说明。

- [x] **Step 1: 将 `numpy`、`scipy`、`scikit-learn`、`joblib` 的当前兼容版本加入 CI constraints。
- [x] **Step 2: 将 README、部署文档和 CI 安装命令统一为 `.[dev,intent]`。
- [x] **Step 3: 检查所有安装说明和约束引用，不修改 M2/M3/M7/M9。

### Task 2: 将 PostgreSQL live 验证纳入 CI

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `docs/deployment.md`

**Interfaces:**
- Consumes: `tests/integration/_postgres_live.py` 的 `COURSE_INSIGHT_TEST_DATABASE_URL` 和 `COURSE_INSIGHT_TEST_DATABASE_NAME` 守卫。
- Produces: CI 中带健康检查的 PostgreSQL 16 服务和真实 live 测试环境变量。

- [x] **Step 1: 配置专用 `course_insight_test` PostgreSQL 服务、健康检查和连接环境变量。
- [x] **Step 2: 让现有全量 pytest 在 CI 中运行 live 测试，而不是在缺少数据库时跳过；同时修复 M4 v10→v11 live 测试误取 v15 migration 的问题。
- [x] **Step 3: 保留本地文档中的 destructive test 安全边界和不可使用生产库的说明。

### Task 3: 补齐 M0 教师复核公开契约

**Files:**
- Modify: `README.md`
- Modify: `docs/interface_guide.md`

**Interfaces:**
- Consumes: `TeacherReviewSubmission.expected_audit_checksum`，代码字段定义见 `src/course_insight/contracts/platform.py`。
- Produces: 文档字段表、接口说明与已导出的 JSON Schema 一致。

- [x] **Step 1: 在 README 的 `TeacherReviewSubmission` 字段表加入 `expected_audit_checksum:str`。
- [x] **Step 2: 在接口指南说明版本和 checksum 必须同时携带。
- [x] **Step 3: 重新导出 Schema 并检查无语义差异。

### Task 4: 验证并同步

**Files:**
- Verify: `tests/contract/`, `tests/integration/`, `tests/e2e/`

- [x] **Step 1: 运行 M0/M4/M5/M6/M8 定向回归。
- [x] **Step 2: 运行完整 pytest、compileall、pip check、Schema 导出和 git diff --check。
- [x] **Step 3: 检查 diff 仅包含本计划范围。
- [ ] **Step 4: 提交 conventional commit 并推送 `main` 到 `origin/main`。
- [ ] **Step 5: 推送后重新读取远端与本地提交关系，记录结果。
