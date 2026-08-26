# 新知识架构人工测试数据

本目录用于测试 2026-08-25 文件入库、知识点来源合并、题目自动关联、增量发布和删除重建。

推荐顺序：

1. 同时上传 `knowledge/01_congestion_primary.md` 与 `knowledge/02_congestion_secondary.txt`，验证同一知识点保留两个文件来源。
2. 上传 `questions/questions_valid.txt`，验证选择、填空、主观三类题目自动关联。
3. 上传 `knowledge/03_congestion_avoidance_addition.md`，再次确认处理，验证新增知识点和题目关系重建。
4. 在题目编辑页用 `questions/questions_valid_v2.txt` 替换内容，再次确认处理，验证不可变题目文件版本。
5. 单独上传 `questions/questions_partial_invalid.txt`，验证其中坏题被隔离、合法题仍可进入新发布版本。
6. 删除 `01_congestion_primary.md` 后确认，验证“拥塞控制”和“慢启动”仍有第二个文件来源。
7. 再删除 `02_congestion_secondary.txt` 后确认，验证失去全部来源的知识点及其题目关联从活动版本消失。

长文本分块使用现有完整 RFC：

```text
examples/five_day_acceptance/official_sources/rfc9293.txt
```

混合格式测试使用：

```text
examples/five_day_acceptance/courseware/
```

非法文件测试使用：

```text
examples/five_day_acceptance/invalid_fixtures/
```
