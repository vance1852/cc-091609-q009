# 配方颗粒等量换算

该项目描述饮片基准、颗粒产品标准、处方发生时间和换算记录。换算结果保留公式、舍入差额与采用的产品版本，审核结果不会随标准更新漂移。

## 模块

- `products/contracts.py` — 领域契约：饮片剂量（`DecoctionDose`）、颗粒产品标准（`GranuleStandard`）、换算记录（`ConversionRecord`）。
- `products/standards.py` — 已批准标准注册表：按调配日期选择现行标准；更正以新版本取代旧版本，旧版本保留供审计、不再参与新选择。
- `products/conversion.py` — 纯函数换算：饮片日量 ÷ 当量系数 → 分次均摊 → 按包装步长四舍五入 → 日调配量；舍入差额与完整公式显式记录，绝不回改处方原量。
- `products/review.py` — 人工审核标记（缺少品种标准 / 超过最大日用量 / 厂家不可互换）与药师决定（接受换算、手工核定、指定替代标准、退回）。
- `products/service.py` — `GranuleConversionService`：处方快照、调配单版本、审核签发、幂等重算与对照报告。

## 关键规则

- 处方先快照医师开具的饮片名与剂量，再按**调配日期**选择当时已批准的标准。
- 标准更正**不追改**已存在的调配单（尤其已审核单），只影响新单或药师主动发起的重算版本；重算沿用原调配日期，重复的 `request_key` 返回同一版本。
- 缺少对应品种、超过最大日用量、续方厂家不可互换时进入人工审核，药师的每个决定随单留痕。
- `compare()` / `compare_snapshot()` 输出每个数字的公式、标准来源、舍入差额与人工决定，支持生效日前后对照。

## 运行

```bash
python -m compileall products            # 检查契约
python -m unittest discover -s tests -v  # 单元测试（标准库 unittest）
python scripts/compare_fixture.py        # 标准切换前后对照演示
```

`fixtures/granule_switch.json` 是一次脱敏的供应标准切换资料。fixture 未携带的字段（每日次数、最大日量、厂家）在演示脚本中采用默认值，实际部署应来自产品主数据。使用 Python 3.11。
