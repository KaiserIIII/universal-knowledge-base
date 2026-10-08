# 合成客服样本

本目录内容完全虚构，供检索对比和人工忠实度复核使用，不代表真实产品政策。

将 `warranty.md`、`setup-manual.md` 和 `injection-probe.md` 上传到测试知识库，等待文档状态为 `completed`。查询标注在 `queries.json` 中使用文件名，方便审查；调用评测 API 前，先从文档列表取得本次上传的 UUID，按文件名替换 `relevant_filenames` 为 `relevant_doc_ids`。不能直接提交文件名，也不能把一次上传得到的 UUID 当作通用标签。

检索指标只衡量文档命中，不评判回答是否正确。人工判断例子见 `manual-judgments.md`。
