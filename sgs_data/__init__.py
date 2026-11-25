"""造训练数据：摆关键局面、从对局里抽普通局面、往下模拟打标签、导出 SFT / DPO / 题库。

这个包只通过引擎的公开接口工作（Game、clone、determinize、view、sgs.testing 里摆局面的小工具、
智能体注册表、提示词模板），不改 sgs/ 里的任何代码。入口：

    python -m sgs_data configs/data/demo.yaml          # 造数据
    python -m sgs_data.evalbank configs/data/evalbank.yaml   # 用题库考一个模型

设计和取舍见 docs/data-gen.md。
"""
