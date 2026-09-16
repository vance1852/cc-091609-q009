# 配方颗粒等量换算

该项目描述饮片基准、颗粒产品标准、处方发生时间和换算记录。换算结果保留公式、舍入差额与采用的产品版本，审核结果不会随标准更新漂移。

`products/contracts.py` 定义产品和换算类型，`fixtures/granule_switch.json` 是一次脱敏的供应标准切换资料。使用 Python 3.11，可运行 `python -m compileall products` 检查契约。
