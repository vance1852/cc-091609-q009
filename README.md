# 配方颗粒等量换算

该项目描述饮片基准、颗粒产品标准、处方发生时间和换算记录。换算结果保留公式、舍入差额与采用的产品版本，审核结果不会随标准更新漂移。

`products/contracts.py` 定义产品和换算类型，`fixtures/granule_switch.json` 是一次脱敏的供应标准切换资料。使用 Python 3.11，可运行 `python -m compileall products` 检查契约。

## 模块

- `products/contracts.py` — 饮片剂量、颗粒产品标准、换算记录的不可变契约。
- `products/catalog.py` — 已批准标准目录，只增不改；标准更正以新版本注册，按调配日选择生效标准。
- `products/conversion.py` — 单味换算纯函数：饮片量 ÷ 提取比 → 分次 → 包装步长四舍五入 → 日调配量，差额保留展示。
- `products/service.py` — 换算与审核服务：处方快照、版本化换算、幂等重算、人工审核与追溯文本。
- `products/fixture_loader.py` — 加载脱敏样例（缺失字段使用显式占位默认值）。

## 规则要点

- 处方开具后即为不可变快照，换算只引用快照，从不回写医师开具的饮片名与剂量。
- 每次换算/重算生成新版本；已审核调配单不被标准更正追改，更正只影响新单或药师主动发起的重算。
- 相同 `request_key` 的重复重算请求返回同一版本。
- 缺少对应品种、超过最大日用量、不同厂家不可互换（或步长舍入为零）时进入人工审核，药师决定必须填写理由并留痕。

## 运行

```bash
python -m compileall products          # 检查契约
python -m unittest discover -s tests   # 运行测试
python -m products                     # 演示：生效日前后对比 + 标准更正不追改历史
```
