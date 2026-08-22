# MVP 人工试测材料

本目录只放**可提交的说明**。试测课包由脚本在本机生成，写入已忽略的 `runtime/`，不把真实课件、数据库或密钥送上 GitHub。

课包内容是为试测新写的计算机网络短笔记（IPv6 压缩、IPv4 `/24` 主机位、TCP 三次握手、吞吐量），不是某本教材的摘录。组卷、提示、跟练的交互对齐了诊断蓝图先约束再出卷、同构练习和班级到个体下钻这类做法，页面没有照搬外部仓库。

完整从零操作步骤见仓库根目录对话交付，或按下面最短路径执行。

## 最短路径

在仓库根目录、已安装项目的 Python 环境中：

```shell
python scripts/prepare_mvp_manual_trial.py
python manage.py migrate
python manage.py sync_roles --apply
python manage.py changepassword pseudonym_student_001
python manage.py changepassword pseudonym_teacher_001
```

另开两个终端：

```shell
python manage.py runserver 127.0.0.1:8000
python manage.py run_outbox_worker
```

课程号 `course_network`，班级号 `class_01`。登录页 `/accounts/login/`。

DeepSeek 密钥只在本机环境变量 `DEEPSEEK_API_KEY` 或教师配置页填写，不要写入仓库。客观题试测不依赖它。
