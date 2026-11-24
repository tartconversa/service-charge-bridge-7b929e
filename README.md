# 医疗服务执行与收费包关联仓

把服务申请、真实执行、收费项目与现金流水落到可核对的事实上：
已实施、未履行、退款撤回与仍有争议的部分分别留痕，供内部运营核对。
仓内不判定治疗必要性或医疗效果，不按同日同人同价猜测匹配，
申请取消不删除执行，补记执行不自动生成收费。

字段与来源语义见 `docs/资料口径.md`，入仓后的表结构、状态机与算法口径见
`docs/仓模型与口径.md`。

## 环境

Python 3.11.2；依赖已在 requirements.txt 固定（duckdb）。

    python3 -m pip install -r requirements.txt

## 资料自检（原有能力）

无参数运行时只做资料容器检查，核对文件摘要、字段类型、记录键、引用与行数，
不解释业务：

    python3 -m sourcepack

## 业务仓命令

业务能力通过子命令使用，仓文件默认 `warehouse.duckdb`（已在 .gitignore），
可用 `-w` 指定内存之外的任意路径。

    # 1) 按引用顺序接收目录资料并重建事实（同摘要文件自动跳过）
    python3 -m sourcepack -w warehouse.duckdb ingest-dir data --rebuild

    # 2) 收费处视角：销售包已兑现/未兑现/退款/无法归属金额
    python3 -m sourcepack -w warehouse.duckdb packages
    python3 -m sourcepack -w warehouse.duckdb slots --package PK1

    # 3) 科室视角：按执行日期、团队的服务量与对应合同收入
    python3 -m sourcepack -w warehouse.duckdb department
    python3 -m sourcepack -w warehouse.duckdb executions --team T2

    # 4) 履约裁决明细（accepted/pending/disputed），两侧下钻到同一关系
    python3 -m sourcepack -w warehouse.duckdb links
    python3 -m sourcepack -w warehouse.duckdb links --status disputed

    # 5) 退款撤回范围、实退与合同资格额差异；现金匹配状态
    python3 -m sourcepack -w warehouse.duckdb refunds
    python3 -m sourcepack -w warehouse.duckdb cash

    # 6) 两侧缺证：无有效兑现的执行、无兑现的收费权益
    python3 -m sourcepack -w warehouse.duckdb gaps

    # 7) 持续接收新文件（同键整行修订，历史批次保留），随后重建
    python3 -m sourcepack -w warehouse.duckdb ingest-file new/refunds.csv refunds --rebuild

### 人工对账轮次

    python3 -m sourcepack -w warehouse.duckdb open-round
    python3 -m sourcepack -w warehouse.duckdb decide <round_id> F1 disputed --reason "互斥留痕"
    # 错引套餐时改引权益，明细级 effective_slot 随重建更新，来源 slot 保留对照
    python3 -m sourcepack -w warehouse.duckdb decide <round_id> F2 accepted --new-slot S1
    python3 -m sourcepack -w warehouse.duckdb confirm-round <round_id>
    python3 -m sourcepack -w warehouse.duckdb rounds

确认返回确认前后逐表逐键的字段级差异；硬规则冲突与 pending 证据不能被人工决策覆盖。

## 编程接口

```python
from sourcepack.warehouse import Warehouse

with Warehouse("warehouse.duckdb") as wh:
    wh.ingest_directory("data")
    wh.rebuild()
    wh.package_positions()      # 收费处视角
    wh.department_summary()     # 科室视角
    wh.links(status="disputed") # 争议履约关系
    wh.evidence_gaps()          # 两侧缺证
```

## 测试

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack tests

测试覆盖资料接收与修订、履约裁决规则（同人/适配/替代折算/容量/互斥/时点）、
固定分母合同分摊与尾差、退款分类与现金匹配、人工对账快照差异、两侧缺证与恒等式。
