# Contributing to Zhixu / 参与知序开发

Open issues with reproducible steps, expected behavior and sanitized error details.
Submit focused pull requests against `main`; the historical release branch preserves
its application code. Contributions are distributed under [Apache-2.0](LICENSE).

保持中英文界面同步，用户数据不经过翻译字典。修改租户查询时验证所有资源 ID 的权限；
新增模型或解析适配器应显式声明依赖、数据发送范围与不可用状态。不要提交密钥、数据库、
真实知识库文档、上传文件或未经脱敏的截图。

Install `backend/requirements-core.lock` in an isolated virtual environment. Run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q backend/app tests start.py
python -m pip check
node --test tests/web/*.test.mjs
```

Use synthetic files, temporary databases and fake upstreams for automated tests.
Describe migration effects and preserve existing records when changing a schema.
For UI work, verify both languages in a real browser and record relevant screenshots.
Do not infer retrieval quality or production readiness from isolated API tests.
