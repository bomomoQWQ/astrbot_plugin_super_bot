# 参考资料镜像（第三方代码）

本目录是 **[dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)**
的只读镜像，仅用于设计对照，**不参与本插件的运行**。

- 上游：`https://github.com/deepseek-ai/dsh-superagent-prototype`
- 许可：**MIT**，Copyright (c) 2026 DeepSeek Harness Team
- 许可全文见同目录 [LICENSE](LICENSE)（按 MIT 要求原样保留）

## 为什么要镜像

调研期间需要反复对照它的 Gate / Assessment 分离、单槽 + 只追加的求值调度、
`key(*parts)=sha256(json)[:32]` 这类具体做法。上游仓库可能变更或消失，
镜像一份能保证文档里的引用长期有效。

## 引用方式

本仓库文档中引用它时统一写作 `docs/调查/04-reference-model.md` 里的对应章节，
并在结论旁标注上游文件路径，便于回溯。

> 如果你要基于本插件二次开发，**不需要**读这个目录；它对本插件的功能没有任何影响。
