重构模块: $ARGUMENTS

流程:
1. 阅读该模块当前代码，总结现有逻辑
2. 对照 docs/playbook_template_v2.md 列出需改动的点
3. 展示修改计划（文件 + 函数 + 改动内容），等我确认
4. 实施修改
5. 编写/更新单元测试
6. 运行 pytest tests/ -v 确认通过
7. 用 git diff 展示所有变更

约束:
- 不硬编码 US 特有假设，交易时段等从 market config 读取
- 每个公开函数必须有 docstring
- 修改现有函数签名时，同步更新所有调用方