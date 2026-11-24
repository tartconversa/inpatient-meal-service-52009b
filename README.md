# 住院配餐的供餐与耗用事实仓

仓库分两层：`sourcepack` 是通用资料检查器，`mealwh` 是业务事实仓。字段、单位和业务记录的含义见 docs/资料口径.md，完整性目录为 data/catalog.json，事实层语义见 docs/仓模型.md。不得把文件自检成功解释成业务结论成立。

## 环境

Python 3.11.2；仓库依赖已在 requirements.txt 固定版本。

    python3 -m pip install -r requirements.txt
    python3 -m sourcepack

资料检查入口核对文件摘要、字段类型、记录键、引用与行数，不进行估计、推荐、批准或结算。新增业务资料可使用相同字段契约，目录摘要只对应当前保存的输入文件。

## 业务入仓与查询(mealwh)

`mealwh` 把订餐版本、实际制作、运送签收和厨房原料领退持续入仓，按可知截点构建多粒度事实，并提供可读查询与只增不改的结算快照。系统只按提供的餐种约定处理运营事实，不为患者决定饮食或给出营养治疗建议。

    # 按批次入仓(批次与接收时刻由调用者明确;相同文件摘要不增加事实)
    python3 -m mealwh ingest --db wh.duckdb --batch BASE \
        --received-at 2026-10-16T21:00:00+08:00 --data-dir data
    # 后续补录(转科补录、原料退库等)按新批次分次装载
    python3 -m mealwh ingest --db wh.duckdb --batch SUP1 \
        --received-at 2026-10-17T09:00:00+08:00 --file ingredient_moves=补录.csv

    # 按可知截点构建一版事实;不加 --as-of 表示全部已接收资料
    python3 -m mealwh build --db wh.duckdb [--as-of 2026-10-16T21:00:00+08:00]

    # 可读查询(默认取最新构建,--build 可指定历史构建)
    python3 -m mealwh fulfillment --db wh.duckdb [--ward W2]   # 按餐次×病区查需求兑现与浪费
    python3 -m mealwh pot-cost --db wh.duckdb [--pot K1]       # 按锅次查实耗与各用途成本
    python3 -m mealwh trace --db wh.duckdb --slot S1           # 患者餐回溯锅次与费用资格

    # 结算确认:快照只增不改,原结算依据保留供厨房和病区比较差异
    python3 -m mealwh settle --db wh.duckdb --label 晚餐结算
    python3 -m mealwh settlement --db wh.duckdb --id 1 --compare 2

    # 接收历史与构建、结算列表
    python3 -m mealwh batches --db wh.duckdb
    python3 -m mealwh builds --db wh.duckdb
    python3 -m mealwh settlements --db wh.duckdb

查询输出为 JSON，金额为人民币元(同时给出分)，时刻保留来源的 +08:00 偏移。缺锅次或缺领退证据时供餐事实仍可确认，食材成本标记 `pending` 未决。

## 已有检查与语法构建

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack mealwh tests

测试覆盖资料容器、输入诊断与业务口径(供餐认定、损耗分类、费用资格、共锅分摊、舍入尾差、结算保留)。运行资料检查与业务测试不需要访问外部业务系统。
