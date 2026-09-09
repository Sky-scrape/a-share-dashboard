"""Rule versions. Each round uses one frozen config. New versions append at bottom.

选股范围口径（用户确认 2026-09-04）：
  - 最终选股（每日标的）仅限沪深主板：沪 600/601/603/605 + 深 000/001/002/003
    （002/003 中小板 2021 年已并入深主板）；创业板 300/301、科创板 688/689、北交所 .BJ
    一律不入选。select 层 in_pick_universe() 过滤 + run_round 每只 pick 硬断言。
  - 研究层（市场宽度/涨停家数/题材计数/板块涨停数/龙虎榜识别度）用全市场面板：
    创业板/科创板/北交所（面板 344 只 .BJ，30% 涨跌停档）照常参与情绪与热度统计。
  - 版本线：V1→V3 为沪深全板口径的历史迭代（runs 原件归档 runs_backup_scope_verify）；
    M1→M_N 为主板口径新迭代。M1 = V1 初始规则 + 主板范围，V2/V3 调整不预设带入
    （其在全板口径下推导），由主板口径各轮评审重新推导。

Version log:
V1 -> V2 (based on round1, n=566):
  [采纳] 非涨停组重构: V1 的"热门板块高位趋势股"追高逻辑整体负期望(均次-0.23%, 胜率43.5%),
         所有因子分位数无区分度(相关性≈0), 平均开盘-0.66% 说明隔夜情绪不认可。
         改为"强势板块缩量回调低吸": 当日涨幅[-3.5,+2.5], 贴近MA10不离过远, 量缩,
         距60日高点0.80-0.985的回撤区, 板块当日或昨日有涨停(持续性), 龙虎榜净买权重 5->15。
  [采纳] 涨停组: 时间因子 corr=+0.095 最有效, 权重 15->20; struct 权重 15->10(高分桶反而略差)。
         次日开盘分档显示 +2%~+8% 高开组均次 +5.4~+7.8%, 买点触发上限 +3% -> +5%。
  [采纳] 市场环境: 增加指数趋势闸门(上证5日跌幅≤-2.5% 强制 defensive)、恐慌跌停闸门(ld≥40);
         freeze 阈值下调至 lu<42 且 adv<0.28 (V1 阈值整个区间从未触发);
         defensive 配额 (1,2)->(2,1) (defensive 下涨停组+3.08% 明显优于非涨停组-0.40%)。
  [采纳] 非涨停组买点收紧: 开盘≥-1.5%, 盘中不破-2.5%, 收盘站上 max(开盘,昨收)。
  [观察] 9月初系统性回撤(09-01选股遇09-02普跌)无法由个股选择规避, 归因保留"市场系统性风险"。
V2 -> V3 (based on round2, n=545):
  [采纳] 非涨停组板块门槛收紧: V2 sector 因子 corr=+0.134, 高分桶(≥0.8)+0.46% vs 0.6-0.8 桶-0.95%。
         sector_strict: 板块当日涨停≥2, 或(≥1 且涨幅分位≥0.85), 或5日分位≥0.85; 另要求板块当日涨幅分位≥0.6。
         sector 权重 20->25。
  [采纳] aggressive 环境配额 (2,2)->(2,1): V2 NLU 在 aggressive 下 -0.35% (n=54),
         defensive/normal/freeze 均 +0.12~+0.79%。涨停组三个环境已收敛(+3.1~+3.3), 规则冻结。
  [微调] pos 梯度: 深回撤区(0.80-0.90) 1.0, 0.90-0.95 0.8, ≥0.95 0.6 (低桶 +1.48% 但 n=12, 只做小幅度调整防过拟合)。
  [冻结] 涨停组全部规则与买点/风控参数; 市场环境分类与 V2 一致。
  [保留观察] 非涨停组买点触发与否差异巨大(+3.88% vs -1.54%), 触发条件属 T+1 信息不可前置,
         继续通过提高选股质量间接提升触发率。
M1 -> M2 (based on mboard_round1_M1, n=566: 涨停组242 / 非涨停组324; 整体胜率52.47% 均次+1.36% 盈亏比1.75):
  [采纳][涨停组] 权重重排: theme 25->15 (theme_cnt 三桶 3.14/2.77/3.28 零梯度, corr +0.009),
         ladder 15->20, time 15->20 (09时档 n=178 均次+3.49% vs 10时档 +1.90%, 时间因子最强;
         cont_cnt corr +0.160 次强)。
  [采纳][涨停组] 买点校准: 次日开盘<0 组 win 34.7% 均次-1.22% (n=75), 2~5/5~8/>8 桶 +5.13/+6.58/+7.23
         -> 观察窗口 open_min -2.0->0.0 (低开不接); trigger_open_max 3->5 (提高可触发覆盖)。
  [采纳][非涨停组] 重构为"活跃板块缩量回调低吸"(原"贴近 60 日高点趋势股"负期望):
         dist_high60 梯度 0.90~0.95 +0.86% / 0.95~0.99 -0.18% / >=0.99 -1.35% (corr -0.193 全场最强)
         -> 区间 0.86~1.01 改 0.80~0.95;
         vol_ratio 0.5~0.8 +1.69% / 0.8~1.3 +0.17% / 1.3~2.2 -0.47% -> 区间 0.5~2.2 改 0.35~1.2;
         ret20 0~10 桶 +0.65% 最好 -> 区间 -5~45 改 0~30。
  [采纳][非涨停组] 权重重排: pos 20->25 (最强因子, pos_tiers 配置化: <0.85 ->1.0 / <0.90 ->0.85 / 其余 0.6),
         recog 5->10, mom 15->10, trend 25->20; sector 分量配置化 sector_mix=(0.55,0.25,0.2,2)
         (M1 中板块涨停 >=3 家桶 +0.02% 弱于 2 家桶 +0.85%, 家数分量降权、rank5 升权)。
  [采纳] 配额: aggressive 非涨停组 -0.51% (win 38.8%, n=80) -> aggressive (2,2)->(2,1);
         defensive 涨停组 +2.91% (n=76) 明显优于非涨停组 +0.02% (n=156) -> defensive (1,2)->(2,1)。
  [观察] 涨停组 >=4板 桶 +7.79% 但 n=7 样本不足, 不动 ladder 档位与高位独狼惩罚;
         涨停组 normal 环境 74.4% 最优 / aggressive 57.5% 偏弱, regime 阈值本轮不动, 下轮复核。
M2 -> M3 (based on mboard_round2_M2, n=523: 涨停组318 / 非涨停组205; 整体胜率58.13% 均次+1.76%):
  [采纳][非涨停组] dist_high_min 0.80->0.86: M2 深回调桶 <0.86 均次-0.30% (n=111) 拖累,
         0.86~0.90 +0.08 / 0.90~0.95 +1.40; 与 M1 corr(-0.193) 合并口径 = 0.86~0.95 甜区,
         深跌(<0.86 接飞刀)与贴顶(>0.95 追高)两端都规避。
  [采纳][非涨停组] min_amount 4e8->1e9: 4~10亿桶 -0.65% (n=82) vs 10~30亿 +0.14 / >30亿 +1.27,
         corr +0.152, 主板口径流动性偏好明显。
  [采纳][涨停组] ladder 4板档 0.8->1.0 (ladder_tiers 配置化): >=4板 M1 +7.79% (n=7) /
         M2 +6.58% (n=10, win 90%) 两轮同向, 小样本采纳持续观察; 高位独狼惩罚保留。
  [采纳] aggressive 环境非涨停组连续两轮负期望 (M1 -0.51% n=80 / M2 -0.47% n=40)
         -> aggressive 配额 (2,1)->(2,0), 该环境只做涨停组并诚实缺额。
  [观察] ind_rank5 梯度跨轮反转 (M1 >=0.85 桶 +0.20 -> M2 -0.45) 判为噪声不改; ret20 梯度同样
         反转 (0~10 桶 +0.65 -> -0.32) 观察一轮; 涨停组次日低开负期望属 T+1 信息 (open<0 桶
         -0.91%, n=95), 选股侧不可用, 买点窗口 open_min=0 已在执行层规避。
M3 轮末评审（mboard_round3_M3, n=483）: 无实质性规则修改 —— 各分桶与 M2 一致, 非涨停组负桶已清空
  (10~30亿 +0.43% / >30亿 +1.40%), 剩余观察项均小样本 (ind_lu_cnt=0 桶 n=10 / >=4板 n=11)
  或 T+1 不可前置信息; ind_rank5 / ret20 梯度连续跨轮漂移判噪声。
终止决定（2026-09-04）: M2 vs M3 涨停组胜率变化 0.0pct / 非涨停组 +0.19pct (均 <=3pct), 均次/回撤/
  逻辑兑现率无显著恶化, 权重结构一致无核心规则增删 -> 满足稳定标准 1/2/3, M3 固化为 M_Final。
  最终方案: reports/最终筛选方案-主板.md; 旧全板口径方案 reports/最终筛选方案.md 归档保留。
"""

V1 = dict(
    version="V1",
    # ---- market regime thresholds ----
    regime=dict(
        aggressive_lu=75, aggressive_adv=0.52, aggressive_ld_max=15,
        freeze_lu=25, freeze_adv=0.30,
        defensive_lu=45, defensive_adv=0.40,
        panic_ld=30,
        idx5_force_defensive=None,
    ),
    # quota per regime: (limit-up group, non-limit-up group)
    quotas=dict(aggressive=(2, 2), normal=(2, 2), defensive=(1, 2), freeze=(0, 1)),
    # ---- limit-up group ----
    lu_group=dict(
        min_score=55.0,
        weights=dict(theme=25, ladder=15, seal=20, time=15, liq=10, struct=15),
        min_amount=1.5e8, max_amount=2.0e10,
        min_bars=60,
        exclude_late_time="14:45",      # later than this = late sneak limit-up
        theme_min_members=2,
        exclude_prev_yizi=True,         # yesterday one-word board -> bad risk/reward
        exchanges=("SH", "SZ"),
        seal_ratio_tiers=[(0.15, 1.0), (0.08, 0.7), (0.03, 0.4), (0.0, 0.15)],
        time_tiers=[("09:45", 1.0), ("10:30", 0.85), ("11:30", 0.7),
                    ("13:30", 0.5), ("14:30", 0.3), ("23:59", 0.05)],
        vol_ratio_tiers=[(0.8, 3.0, 1.0), (3.0, 4.0, 0.7), (0.0, 0.8, 0.6), (4.0, 99, 0.45)],
    ),
    # ---- non-limit-up group ----
    nlu_group=dict(
        min_score=55.0,
        weights=dict(sector=25, trend=25, pos=20, mom=15, liq=10, recog=5),
        min_amount=4e8, min_amt_ma5=3e8,
        min_bars=120,
        max_raw_pct=7.0, min_raw_pct=-3.0,
        ret20_min=-5.0, ret20_max=45.0,
        dist_high_min=0.86, dist_high_max=1.01,
        vol_ratio_min=0.5, vol_ratio_max=2.2,
        top_ind_n=12,
        exchanges=("SH", "SZ"),
        ma10_gap_max=None,              # V2: max close/ma10 - 1
        require_sector_persistence=False,  # V2: sector lu today or yesterday
        exclude_prev_lu=False,          # V2: skip yesterday limit-up stocks
    ),
    # ---- next-day buy/risk conditions ----
    buys=dict(
        lu=dict(open_min=-2.0, open_max=5.0, trigger_open_max=3.0,
                trigger_low_min=-3.0, risk_low=-5.0, risk_close=-4.0),
        nlu=dict(open_min=-2.0, open_max=3.0,
                 risk_low=-4.0, risk_close=-3.5,
                 trigger_open_min=None, trigger_low_min=None),
    ),
    # scoring weights (result score formula)
    scoring=dict(close_w=600, high_w=200, low_pen=150, low_pen_start=0.03),
)

V2 = dict(
    version="V2",
    regime=dict(
        aggressive_lu=80, aggressive_adv=0.55, aggressive_ld_max=12,
        freeze_lu=42, freeze_adv=0.28,
        defensive_lu=55, defensive_adv=0.45,
        panic_ld=40,
        idx5_force_defensive=-2.5,   # SSE 5-day return <= this -> force defensive
    ),
    quotas=dict(aggressive=(2, 2), normal=(2, 2), defensive=(2, 1), freeze=(0, 1)),
    lu_group=dict(
        min_score=55.0,
        weights=dict(theme=25, ladder=15, seal=20, time=20, liq=10, struct=10),
        min_amount=1.5e8, max_amount=2.0e10,
        min_bars=60,
        exclude_late_time="14:45",
        theme_min_members=2,
        exclude_prev_yizi=True,
        exchanges=("SH", "SZ"),
        seal_ratio_tiers=[(0.15, 1.0), (0.08, 0.7), (0.03, 0.4), (0.0, 0.15)],
        time_tiers=[("09:45", 1.0), ("10:30", 0.85), ("11:30", 0.7),
                    ("13:30", 0.5), ("14:30", 0.3), ("23:59", 0.05)],
        vol_ratio_tiers=[(0.8, 3.0, 1.0), (3.0, 4.0, 0.7), (0.0, 0.8, 0.6), (4.0, 99, 0.45)],
    ),
    nlu_group=dict(
        min_score=60.0,
        weights=dict(sector=20, trend=20, pos=20, mom=10, liq=10, recog=20),
        min_amount=4e8, min_amt_ma5=3e8,
        min_bars=120,
        max_raw_pct=2.5, min_raw_pct=-3.5,
        ret20_min=10.0, ret20_max=45.0,
        dist_high_min=0.80, dist_high_max=0.985,
        vol_ratio_min=0.35, vol_ratio_max=1.3,
        top_ind_n=12,
        exchanges=("SH", "SZ"),
        ma10_gap_max=0.08,
        require_sector_persistence=True,
        exclude_prev_lu=True,
    ),
    buys=dict(
        lu=dict(open_min=-2.0, open_max=5.0, trigger_open_max=5.0,
                trigger_low_min=-3.0, risk_low=-5.0, risk_close=-4.0),
        nlu=dict(open_min=-2.0, open_max=3.0,
                 risk_low=-4.0, risk_close=-3.5,
                 trigger_open_min=-1.5, trigger_low_min=-2.5),
    ),
    scoring=dict(close_w=600, high_w=200, low_pen=150, low_pen_start=0.03),
)

V3 = dict(
    version="V3",
    regime=dict(
        aggressive_lu=80, aggressive_adv=0.55, aggressive_ld_max=12,
        freeze_lu=42, freeze_adv=0.28,
        defensive_lu=55, defensive_adv=0.45,
        panic_ld=40,
        idx5_force_defensive=-2.5,
    ),
    # V3: aggressive 配额非涨停组 2->1 (V2 中 aggressive 环境 NLU -0.35%, n=54; 其余环境 +0.12~+0.79)
    quotas=dict(aggressive=(2, 1), normal=(2, 2), defensive=(2, 1), freeze=(0, 1)),
    # V3: 涨停组规则冻结 (V1->V2 后三个环境均次收敛于 +3.1~+3.3, 不再修改核心规则)
    lu_group=dict(
        min_score=55.0,
        weights=dict(theme=25, ladder=15, seal=20, time=20, liq=10, struct=10),
        min_amount=1.5e8, max_amount=2.0e10,
        min_bars=60,
        exclude_late_time="14:45",
        theme_min_members=2,
        exclude_prev_yizi=True,
        exchanges=("SH", "SZ"),
        seal_ratio_tiers=[(0.15, 1.0), (0.08, 0.7), (0.03, 0.4), (0.0, 0.15)],
        time_tiers=[("09:45", 1.0), ("10:30", 0.85), ("11:30", 0.7),
                    ("13:30", 0.5), ("14:30", 0.3), ("23:59", 0.05)],
        vol_ratio_tiers=[(0.8, 3.0, 1.0), (3.0, 4.0, 0.7), (0.0, 0.8, 0.6), (4.0, 99, 0.45)],
    ),
    nlu_group=dict(
        min_score=62.0,
        # V3: sector 20->25 (corr=+0.134, 高分桶+0.46% vs 低分桶-0.95%); pos 梯度微调; recog 保持 20 (净买桶+1.12%)
        weights=dict(sector=25, trend=18, pos=17, mom=10, liq=10, recog=20),
        min_amount=4e8, min_amt_ma5=3e8,
        min_bars=120,
        max_raw_pct=2.5, min_raw_pct=-3.5,
        ret20_min=10.0, ret20_max=45.0,
        dist_high_min=0.80, dist_high_max=0.985,
        vol_ratio_min=0.35, vol_ratio_max=1.3,
        top_ind_n=12,
        exchanges=("SH", "SZ"),
        ma10_gap_max=0.08,
        require_sector_persistence=True,
        sector_strict=True,     # V3: 板块当日涨停≥2, 或(≥1且涨幅分位≥0.85), 或5日分位≥0.85
        exclude_prev_lu=True,
    ),
    buys=dict(
        lu=dict(open_min=-2.0, open_max=5.0, trigger_open_max=5.0,
                trigger_low_min=-3.0, risk_low=-5.0, risk_close=-4.0),
        nlu=dict(open_min=-2.0, open_max=3.0,
                 risk_low=-4.0, risk_close=-3.5,
                 trigger_open_min=-1.5, trigger_low_min=-2.5),
    ),
    scoring=dict(close_w=600, high_w=200, low_pen=150, low_pen_start=0.03),
)

# ==========================================================================
# 主板口径迭代（M 线，2026-09-04 起）：最终选股仅沪深主板，研究层全市场。
# M1 = V1 初始规则 + 主板范围；V2/V3 的调整不在 M1 预设带入，由各轮轮末评审重新推导。
M1 = dict(
    version="M1",
    # ---- market regime thresholds ----
    regime=dict(
        aggressive_lu=75, aggressive_adv=0.52, aggressive_ld_max=15,
        freeze_lu=25, freeze_adv=0.30,
        defensive_lu=45, defensive_adv=0.40,
        panic_ld=30,
        idx5_force_defensive=None,
    ),
    # quota per regime: (limit-up group, non-limit-up group)
    quotas=dict(aggressive=(2, 2), normal=(2, 2), defensive=(1, 2), freeze=(0, 1)),
    # ---- limit-up group ----
    lu_group=dict(
        min_score=55.0,
        weights=dict(theme=25, ladder=15, seal=20, time=15, liq=10, struct=15),
        min_amount=1.5e8, max_amount=2.0e10,
        min_bars=60,
        exclude_late_time="14:45",      # later than this = late sneak limit-up
        theme_min_members=2,
        exclude_prev_yizi=True,         # yesterday one-word board -> bad risk/reward
        exchanges=("SH", "SZ"),
        main_board_only=True,           # 沪深主板 only
        seal_ratio_tiers=[(0.15, 1.0), (0.08, 0.7), (0.03, 0.4), (0.0, 0.15)],
        time_tiers=[("09:45", 1.0), ("10:30", 0.85), ("11:30", 0.7),
                    ("13:30", 0.5), ("14:30", 0.3), ("23:59", 0.05)],
        vol_ratio_tiers=[(0.8, 3.0, 1.0), (3.0, 4.0, 0.7), (0.0, 0.8, 0.6), (4.0, 99, 0.45)],
    ),
    # ---- non-limit-up group ----
    nlu_group=dict(
        min_score=55.0,
        weights=dict(sector=25, trend=25, pos=20, mom=15, liq=10, recog=5),
        min_amount=4e8, min_amt_ma5=3e8,
        min_bars=120,
        max_raw_pct=7.0, min_raw_pct=-3.0,
        ret20_min=-5.0, ret20_max=45.0,
        dist_high_min=0.86, dist_high_max=1.01,
        vol_ratio_min=0.5, vol_ratio_max=2.2,
        top_ind_n=12,
        exchanges=("SH", "SZ"),
        main_board_only=True,           # 沪深主板 only
        ma10_gap_max=None,              # V2: max close/ma10 - 1
        require_sector_persistence=False,  # V2: sector lu today or yesterday
        exclude_prev_lu=False,          # V2: skip yesterday limit-up stocks
    ),
    # ---- next-day buy/risk conditions ----
    buys=dict(
        lu=dict(open_min=-2.0, open_max=5.0, trigger_open_max=3.0,
                trigger_low_min=-3.0, risk_low=-5.0, risk_close=-4.0),
        nlu=dict(open_min=-2.0, open_max=3.0,
                 risk_low=-4.0, risk_close=-3.5,
                 trigger_open_min=None, trigger_low_min=None),
    ),
    # scoring weights (result score formula)
    scoring=dict(close_w=600, high_w=200, low_pen=150, low_pen_start=0.03),
)

M2 = dict(
    version="M2",
    regime=dict(
        aggressive_lu=75, aggressive_adv=0.52, aggressive_ld_max=15,
        freeze_lu=25, freeze_adv=0.30,
        defensive_lu=45, defensive_adv=0.40,
        panic_ld=30,
        idx5_force_defensive=None,
    ),
    # M2: aggressive (2,2)->(2,1), defensive (1,2)->(2,1)
    quotas=dict(aggressive=(2, 1), normal=(2, 2), defensive=(2, 1), freeze=(0, 1)),
    lu_group=dict(
        min_score=55.0,
        # M2: theme 25->15, ladder 15->20, time 15->20
        weights=dict(theme=15, ladder=20, seal=20, time=20, liq=10, struct=15),
        min_amount=1.5e8, max_amount=2.0e10,
        min_bars=60,
        exclude_late_time="14:45",
        theme_min_members=2,
        exclude_prev_yizi=True,
        exchanges=("SH", "SZ"),
        main_board_only=True,
        seal_ratio_tiers=[(0.15, 1.0), (0.08, 0.7), (0.03, 0.4), (0.0, 0.15)],
        time_tiers=[("09:45", 1.0), ("10:30", 0.85), ("11:30", 0.7),
                    ("13:30", 0.5), ("14:30", 0.3), ("23:59", 0.05)],
        vol_ratio_tiers=[(0.8, 3.0, 1.0), (3.0, 4.0, 0.7), (0.0, 0.8, 0.6), (4.0, 99, 0.45)],
    ),
    nlu_group=dict(
        min_score=55.0,
        # M2: pos 25 / sector 25 / trend 20 / recog 10 / mom 10 / liq 10
        weights=dict(sector=25, trend=20, pos=25, mom=10, liq=10, recog=10),
        min_amount=4e8, min_amt_ma5=3e8,
        min_bars=120,
        max_raw_pct=7.0, min_raw_pct=-3.0,
        ret20_min=0.0, ret20_max=30.0,          # M2: -5~45 -> 0~30
        dist_high_min=0.80, dist_high_max=0.95,  # M2: 0.86~1.01 -> 0.80~0.95 回调低吸区
        vol_ratio_min=0.35, vol_ratio_max=1.2,   # M2: 0.5~2.2 -> 0.35~1.2 缩量
        top_ind_n=12,
        exchanges=("SH", "SZ"),
        main_board_only=True,
        ma10_gap_max=None,
        require_sector_persistence=False,
        exclude_prev_lu=False,
        sector_mix=(0.55, 0.25, 0.2, 2.0),       # M2: rank/rank5/涨停家数 家数分量降权
        pos_tiers=[(0.85, 1.0), (0.90, 0.85), (1.01, 0.6)],  # M2: 深回调优先
    ),
    buys=dict(
        # M2: 低开不接 (open_min -2.0->0.0), trigger_open_max 3->5
        lu=dict(open_min=0.0, open_max=5.0, trigger_open_max=5.0,
                trigger_low_min=-3.0, risk_low=-5.0, risk_close=-4.0),
        nlu=dict(open_min=-2.0, open_max=3.0,
                 risk_low=-4.0, risk_close=-3.5,
                 trigger_open_min=None, trigger_low_min=None),
    ),
    scoring=dict(close_w=600, high_w=200, low_pen=150, low_pen_start=0.03),
)

M3 = dict(
    version="M3",
    regime=dict(
        aggressive_lu=75, aggressive_adv=0.52, aggressive_ld_max=15,
        freeze_lu=25, freeze_adv=0.30,
        defensive_lu=45, defensive_adv=0.40,
        panic_ld=30,
        idx5_force_defensive=None,
    ),
    # M3: aggressive (2,1)->(2,0), 非涨停组连续两轮负期望
    quotas=dict(aggressive=(2, 0), normal=(2, 2), defensive=(2, 1), freeze=(0, 1)),
    lu_group=dict(
        min_score=55.0,
        weights=dict(theme=15, ladder=20, seal=20, time=20, liq=10, struct=15),
        min_amount=1.5e8, max_amount=2.0e10,
        min_bars=60,
        exclude_late_time="14:45",
        theme_min_members=2,
        exclude_prev_yizi=True,
        exchanges=("SH", "SZ"),
        main_board_only=True,
        ladder_tiers={1: 0.55, 2: 0.9, 3: 1.0, 4: 1.0},  # M3: 4板档 0.8->1.0
        seal_ratio_tiers=[(0.15, 1.0), (0.08, 0.7), (0.03, 0.4), (0.0, 0.15)],
        time_tiers=[("09:45", 1.0), ("10:30", 0.85), ("11:30", 0.7),
                    ("13:30", 0.5), ("14:30", 0.3), ("23:59", 0.05)],
        vol_ratio_tiers=[(0.8, 3.0, 1.0), (3.0, 4.0, 0.7), (0.0, 0.8, 0.6), (4.0, 99, 0.45)],
    ),
    nlu_group=dict(
        min_score=55.0,
        weights=dict(sector=25, trend=20, pos=25, mom=10, liq=10, recog=10),
        min_amount=1e9, min_amt_ma5=3e8,           # M3: 4e8->1e9
        min_bars=120,
        max_raw_pct=7.0, min_raw_pct=-3.0,
        ret20_min=0.0, ret20_max=30.0,
        dist_high_min=0.86, dist_high_max=0.95,    # M3: 0.80->0.86
        vol_ratio_min=0.35, vol_ratio_max=1.2,
        top_ind_n=12,
        exchanges=("SH", "SZ"),
        main_board_only=True,
        ma10_gap_max=None,
        require_sector_persistence=False,
        exclude_prev_lu=False,
        sector_mix=(0.55, 0.25, 0.2, 2.0),
        pos_tiers=[(0.85, 1.0), (0.90, 0.85), (1.01, 0.6)],
    ),
    buys=dict(
        lu=dict(open_min=0.0, open_max=5.0, trigger_open_max=5.0,
                trigger_low_min=-3.0, risk_low=-5.0, risk_close=-4.0),
        nlu=dict(open_min=-2.0, open_max=3.0,
                 risk_low=-4.0, risk_close=-3.5,
                 trigger_open_min=None, trigger_low_min=None),
    ),
    scoring=dict(close_w=600, high_w=200, low_pen=150, low_pen_start=0.03),
)

VERSIONS = {"V1": V1, "V2": V2, "V3": V3, "M1": M1, "M2": M2, "M3": M3}

# ==========================================================================
# 概念维度迭代（C 线，2026-09-04 起）：系统文档纳入「概念」选股标准后的结构性变更
# （概念为主、行业为辅；驱动概念 = 所属概念中当日概念指数涨幅最高者，平手取涨停家数）。
# C1 = M_Final(M3) 结构 + 概念一阶因子，按文档"版本号前进一位"纪律从头重跑整个区间：
#   [涨停组] 权重重排腾出概念分: theme 15->5 (M1/M2 已证零梯度, 只作交叉校验),
#     seal 20->15, 新增 concept 15 (概念内涨停 >=4 家->1.0 / 3 家->0.8 / 2 家->0.6 /
#     家数<2 且概念收涨->0.4 / 其余 0.2; 归属缺失 0.3 中低档, 不奖励不惩罚到位);
#     新增概念独狼排除: 个股全部所属概念当日走弱(pct<0)且概念内涨停家数均=1 -> 按
#     独狼板处理排除（与题材独狼排除并列; 归属/行情缺失时该排除不启用, 诚实降级）。
#   [非涨停组] 辨识度首看概念: recog = max(概念档位, 原龙虎榜/板块领袖口径)（与线上
#     build_pool 2026-09-04 概念接入同口径）; 新增概念退潮闸门: 驱动概念当日 pct<0 且
#     5 日 pct5<0 -> 回调股按下跌中继排除（概念退潮不接; 数据缺失不启用）。
#   [验证/归因] 新增 strong_concept（T+1 收盘强于所属概念指数）与「概念持续性不足」
#     归因（T+1 概念指数 <=-0.5% 且个股未涨, 排在板块持续性不足之后）。
#   数据边界：概念归属用当前成分口径近似（concept_map 7 天新鲜度, GENERIC 机械概念
#   剔除）；概念指数日线 2025-11-03 起抓取（scripts/fetch_concepts.py）。
C1 = dict(
    version="C1",
    regime=M3["regime"],
    quotas=M3["quotas"],
    lu_group={**M3["lu_group"],
              "weights": dict(theme=5, concept=15, ladder=20, seal=15, time=20, liq=10, struct=15),
              "concept_lu_tiers": [(4, 1.0), (3, 0.8), (2, 0.6)],
              "concept_pos_floor": 0.4, "concept_cold": 0.2, "concept_missing": 0.3,
              "exclude_concept_lonewolf": True},
    nlu_group={**M3["nlu_group"],
               "recog_concept_first": True,
               "concept_recede_gate": True},
    buys=M3["buys"],
    scoring=M3["scoring"],
)

VERSIONS.update({"C1": C1})

# ---------------------------------------------------------------------------
# C1 -> C2 (based on concept_round1_C1, n=480: 涨停组318 / 非涨停组162;
# 整体 58.13% / +1.99%; 涨停组 64.78% / +2.87% vs M3 64.47% / +2.89 持平偏好;
# 非涨停组 45.06% / +0.27% vs M3 48.48% / +0.69% 明显变差 —— 病灶定位:
# C1 的 recog=max 吸收让概念冷的票仍靠龙虎榜/板块领袖高分入选。
# 交叉验证(162 只非涨停组): 概念内涨停<=2家 n=46 均次 -1.37% (四个概念涨幅桶内全负),
# >=3家 n=116 均次 +0.92% (全正) -> 主梯度 = 概念内涨停家数, 与概念涨幅同向可交叉):
#   [采纳][非涨停组] recog 改概念直接阶梯: >=3家->1.0 / 2家->0.4 / 1家->0.3 /
#     0家->0.2, 概念数据缺失回退原口径 (龙虎榜净买/板块领袖/板块涨停>=2)。
#     概念冷(<=2家)最高 4 分 vs 概念热 10 分, 差距足以把冷概念票挤出前 2 名。
#   [采纳][非涨停组] 移除 C1 概念退潮闸门(pct<0 且 pct5<0): 其目标桶(-3~0)内
#     概念热的票仍在赚(+2.00/+0.04), 概念冷的票已被家数阶梯覆盖; 闸门无独立贡献,
#     按"不做双重惩罚、防过拟合"纪律移除。
#   [冻结][涨停组] C1 接线整体持平偏好(胜率+0.31pct, 均次-0.01pct), 概念涨幅梯度
#     方向正确(2~5 桶 +3.24% / >5 桶 +3.79% vs 0~2 桶 +2.21%), 家数档位 3 家桶偏弱
#     但 n=31 不满足调档纪律 -> 保持观察, 本轮不动。
C2 = dict(
    version="C2",
    regime=M3["regime"],
    quotas=M3["quotas"],
    lu_group=C1["lu_group"],   # 涨停组与 C1 一致（冻结观察）
    nlu_group={**M3["nlu_group"],
               "recog_concept_tiers": [(3, 1.0), (2, 0.4), (1, 0.3), (0, 0.2)]},
               # C2: 移除 concept_recede_gate / recog_concept_first（理由见上）
    buys=M3["buys"],
    scoring=M3["scoring"],
)

VERSIONS.update({"C2": C2})

# ---------------------------------------------------------------------------
# C2 -> C3 (based on concept_round2_C2, n=483: 整体 59.42% / +2.09%, 超 M_Final 基线;
# 涨停组 64.78% / +2.87% (与 C1 分毫不差, 冻结生效); 非涨停组 49.09% / +0.58%
# (C1 45.06% -> +4.03pct 修复, vs M3 48.48% / +0.69% 胜率+回撤均优)。
# 轮末评审剩余负桶筛查:
#   [采纳][非涨停组] 行业当日涨幅分位 <0.6 桶连续两轮负期望 (C1 -1.07% n=16 /
#     C2 -1.88% n=18, 满足连续多样本同向纪律) -> 加 sector_rank_min=0.6 硬下限:
#     概念定驱动、行业定背景, 行业当日明确走弱(后 40%)的背景不接。
#   [观察] 概念内涨停 <=2 家残余入选 n=14 (1家 -0.37% / 2家 -1.19%), 样本不足,
#     家数阶梯已处最低档, 只标记不动作; ret20 10~20 桶 +0.07% 弱正, 跨轮漂移判噪声。
C3 = dict(
    version="C3",
    regime=M3["regime"],
    quotas=M3["quotas"],
    lu_group=C1["lu_group"],
    nlu_group={**C2["nlu_group"], "sector_rank_min": 0.6},
    buys=M3["buys"],
    scoring=M3["scoring"],
)

VERSIONS.update({"C3": C3})

# ---------------------------------------------------------------------------
# C3 -> C4（US 线，2026-09-09 起）：系统文档纳入「隔夜美股」维度后的结构性变更。
# 复盘时点同步推迟：T 日复盘在 T+1 美股收盘后 1 小时内完成（北京时间约 04:00 夏令
# 时 / 05:00 冬令时之后），T 日选股可用信息新增「隔夜美股」= 最近一个在 T 日
# 09:15 前收盘的美股交易日 U 的完整场次（严格无未来函数：U 的收盘先于选股）。
# 数据单一来源 backend/us_market.py factors.json：IXIC 纳指综合 + SPY 标普代理 +
# SPDR 11 板块 ETF/SMH + 概念→美股代理篮子（US_PROXY_GROUPS）。
#   [环境] 新增隔夜美股闸门 us_night_force_defensive=(-2.5, -2.0)：纳指隔夜 <=-2.5%
#     或标普隔夜 <=-2.0% 强制 defensive（排在恐慌跌停闸门之后、idx5 闸门之前），
#     把外盘系统性风险隔离出 aggressive/normal。
#   [涨停组] 新增 us 因子权重 5（struct 15->10 腾出）：驱动概念的美股代理组隔夜
#     涨幅分档 >=2%->1.0 / >=1%->0.8 / >=0->0.6 / >=-1%->0.35 / <-1%->0.15；
#     无映射/数据缺失 ->0.5 中性档（诚实缺失，不奖励不惩罚到位）。
#   [非涨停组] 新增 us 因子权重 5（trend 20->15 腾出——多头排列已是非涨停组硬筛
#     条件，trend 因子边际信息量低）。分档口径与涨停组一致。
#   [验证/归因] 新增「隔夜美股拖累」归因（T+1 隔夜纳指 <=-1% 且 A 股大盘 <=-1% 且
#     个股收跌），排在市场系统性风险之前；validation.csv 落 us_ndx1/us_spx1 列。
#   数据边界：US 历史由腾讯/新浪日线一次性回补（不复权缺口以 close 口径为准），
#     未收盘的美股 bar 不入库；factors 覆盖率由 run_round 硬断言 >=95%。
C4 = dict(
    version="C4",
    regime={**M3["regime"], "us_night_force_defensive": (-2.5, -2.0)},
    quotas=M3["quotas"],
    lu_group={**C1["lu_group"],
              "weights": dict(theme=5, concept=15, ladder=20, seal=15, time=20,
                              liq=10, struct=10, us=5)},
    nlu_group={**C3["nlu_group"],
               "weights": dict(sector=25, trend=15, pos=25, mom=10, liq=10,
                               recog=10, us=5)},
    buys=M3["buys"],
    scoring=M3["scoring"],
)

VERSIONS.update({"C4": C4})

# ---------------------------------------------------------------------------
# C4 -> C5（based on us_round1_C4, n=478: 整体 58.37% / +2.08% vs C3 59.75% / +2.13%;
# 涨停组 63.21% / +2.87 (C3 64.78% / +2.87), 非涨停组 48.75% / +0.50 (C3 49.69% / +0.64);
# 选股重合度 79.8%。轮末分桶证据（runs/us_round1_C4/analysis.md）：
#   [采纳][非涨停组] 移除 us 因子并恢复 trend 20->15 原权重：us_pct 分桶无梯度
#     （1~2 桶 -1.17% n=13 / >2 桶 +0.63% n=26, 方向紊乱），符合 C1→C2「无独立
#     贡献的因子不保留」纪律；非涨停组回归 C3 口径（该组 C4 退化 -0.94pct 回收）。
#   [采纳][涨停组] us 因子保留（正值区方向正确: 0~1 桶 +3.71% / >2 桶 +3.32% vs
#     -1~0 桶 +1.72%），但 struct 15->10 回滚为 15、theme 5->0 腾出权重——M1/M2
#     已证 theme 零梯度（corr +0.009），struct 分桶在 M 线有真实梯度不该动。
#     C4 涨停组 -1.57pct 的来源隔离到本轮验证。
#   [采纳][环境] 闸门 (-2.5,-2.0)->(-2.0,-2.0)：区间内纳指 <=-2.5 仅 1 天无法评估，
#     而纳指 <-2 桶证据明确（涨停组 +0.46% 胜率 42.9% n=14 / 非涨停组 -1.49% n=8），
#     伤害区从 -2 起步；放宽后区间内命中 8 天。
#   [观察] 「隔夜美股拖累」归因区间内零命中（条件：隔夜纳指<=-1 且大盘<=-1 且收跌），
#     继续留档；us_pct 深负桶（<-2 n=5 +5.74%）反直觉，小样本不动档位。
C5 = dict(
    version="C5",
    regime={**M3["regime"], "us_night_force_defensive": (-2.0, -2.0)},
    quotas=M3["quotas"],
    lu_group={**C1["lu_group"],
              "weights": dict(theme=0, concept=15, ladder=20, seal=15, time=20,
                              liq=10, struct=15, us=5)},
    nlu_group={**C3["nlu_group"]},   # 回滚：us 无梯度，trend 恢复 20
    buys=M3["buys"],
    scoring=M3["scoring"],
)

VERSIONS.update({"C5": C5})

# ---------------------------------------------------------------------------
# C5 -> C6（based on us_round2_C5, n=478: 整体 58.16% / +2.00%; 涨停组 61.95% / +2.66
# 连续第二轮劣于 C3（C4 63.21% / C3 64.78%）——涨停组 us 因子两种权重腾挪方式
# （struct 腾 / theme 腾）都选不出更好的票，判定该分档因子在涨停组无正向贡献；
# 非涨停组回滚 C3 后修复（50.62% / +0.70 超 C3 49.69% / +0.64），其中闸门日避损
# 有边际贡献。轮末评审：
#   [采纳][涨停组] 移除 us 因子，涨停组完全回滚 C1/C3 冻结口径（theme 5 恢复）。
#   [采纳][环境] 保留隔夜美股闸门 (-2.0,-2.0)——C5 闸门日 8 天的 picks C3 原样
#     42.9%/+0.26% 确认外盘重挫日 A 股隔夜受损真实存在，gate 是唯一的结构性防线；
#     非涨停组在 C5 的超额主要来自闸门避损。
#   [观察] C6 若整体仍不优于 C3，则本轮「美股维度」的诚实结论 = 个股/板块代理
#     因子在该区间无增量，只保留环境闸门作为风控层；数据层保留作监控与归因。
C6 = dict(
    version="C6",
    regime={**M3["regime"], "us_night_force_defensive": (-2.0, -2.0)},
    quotas=M3["quotas"],
    lu_group={**C1["lu_group"]},       # 回滚：C1 冻结口径（theme=5, 无 us）
    nlu_group={**C3["nlu_group"]},     # C3 口径（无 us）
    buys=M3["buys"],
    scoring=M3["scoring"],
)

VERSIONS.update({"C6": C6})

# ---------------------------------------------------------------------------
# C6 -> C7（based on us_round3_C6 轮末优化分析 + exp 变体实验，2026-09-09）：
# 实验单一变量隔离（scripts/exp_c6_variants.py，共享 store 全区间跑）：
#   expA aggressive (2,0)->(1,0): LU 65.36%/+2.93 (n=280) 每票质量略升，但整体
#     60.0%/+2.12 低于 C6（被砍的 38 票均次仍为正），【否决，观察】。
#   expB min_score 55->70: 整体 60.59%/+2.20 —— C6 两组 95%+ 入选票得分 >=70，
#     60~70 分区间 5 只全亏，提门槛纯清尾巴，【采纳】。
#   expC NLU 市场量能闸门 amt_ratio>=0.9: 整体 61.25%/+2.41（C6 60.04%/+2.15），
#     NLU 51.33%/+1.10 (n=113, 砍 47 票)，LU 分毫不差 —— 缩量日低吸 -0.24%(n=47)
#     vs 放量日 +2.13%(n=41) 梯度单调，且 amt_ratio 为 T 日收盘可得信息，【采纳】。
#   另两项实验结论（scripts/exp_hold_sizing.py，不进规则线）：
#     T+2 持有对照：均笔 +0.57%->-1.84%、胜率 51%->28%，隔日溢价两日内衰减殆尽，
#       T+1 收盘离场为铁律，【证伪】。
#     资金管理层（不改动选股/买卖点，费后 0.15% 双边口径）：S1 环境缩仓
#       （defensive/freeze 档 0.5x 仓位）费后累计 +57.4% vs 满仓 +49.1%，回撤
#       -39.8%->-19.6% 近乎减半；连败降仓 S2/组合 S3 均劣于 S1。S1 作为资金管理
#       建议层记录（选股规则线不含仓位；实盘仓位由使用者按档执行）。
C7 = dict(
    version="C7",
    regime=C6["regime"],
    quotas=C6["quotas"],
    lu_group={**C6["lu_group"], "min_score": 70.0},
    nlu_group={**C6["nlu_group"], "min_score": 70.0, "market_amt_ratio_min": 0.9},
    buys=C6["buys"],
    scoring=C6["scoring"],
)

VERSIONS.update({"C7": C7})
