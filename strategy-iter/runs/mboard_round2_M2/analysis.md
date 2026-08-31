# 轮末因子分析 mboard_round2_M2  (n=523)

## 涨停组 (n=318)
corr(close_ret): score:+0.114  dist_high60:+0.002  ret20:+0.136  vol_ratio:-0.036  amount:+0.009  seal_ratio:+0.077  cont_cnt:+0.159  theme_cnt:+0.051  amt_ratio:+0.052

### 涨停时间(小时) 分桶
           n  win%  avg%
lu_hour                 
09       238  65.5  3.16
10        59  59.3  2.17
11        12  83.3  3.37
13         7  42.9  0.39
14         2  50.0 -0.25

### 次日开盘 open_ret 分桶
           n  win%  avg%
open_ret                
<0        95  38.9 -0.91
0~2       61  57.4  1.91
2~5       69  79.7  4.38
5~8       43  83.7  6.12
>8        49  83.7  6.58

### 封单比 seal_ratio 分桶
              n  win%  avg%
seal_ratio                 
<3%           2   0.0 -1.68
3~8%         24  58.3  2.05
8~15%        73  74.0  3.28
>15%        219  62.6  2.91

### 连板数 cont_cnt 分桶
            n  win%  avg%
cont_cnt                 
1板        193  62.2  2.39
2板         94  63.8  3.17
3板         21  76.2  4.68
≥4板        10  90.0  6.58

### 题材内涨停 theme_cnt 分桶
             n  win%  avg%
theme_cnt                 
2           90  62.2  2.60
3~4        106  62.3  2.63
≥5         122  68.0  3.37

### 当日成交额 分桶
          n   win%  avg%
amount                  
<1.5亿     0    NaN   NaN
1.5~3亿   50   52.0  1.72
3~100亿  265   66.4  3.12
>100亿     3  100.0  3.57

### 入选分 score 分桶
         n   win%  avg%
score                  
<55      0    NaN   NaN
55~60    1  100.0  0.17
60~65    7   71.4  1.65
65~70   21   57.1  1.93
≥70    289   64.7  3.01

### 买点触发 vs 未触发
                 n  win%  avg%
buy_triggered                 
False          225  50.7  1.66
True            93  97.8  5.91

### 按市场环境
              n  win%  avg%
regime                     
aggressive   80  60.0  2.15
defensive   151  60.9  2.83
normal       87  74.7  3.72

## 非涨停组 (n=205)
corr(close_ret): score:-0.061  dist_high60:+0.137  ret20:+0.055  vol_ratio:-0.006  amount:+0.152  ind_rank:+0.005  ind_rank5:-0.169  ind_lu_cnt:-0.043  amt_ratio:+0.529

### 距60日高点 dist_high60 分桶
               n  win%  avg%
dist_high60                 
<0.86        111  45.9 -0.30
0.86~0.90     78  50.0  0.08
0.90~0.95     16  56.2  1.40
0.95~0.99      0   NaN   NaN
≥0.99          0   NaN   NaN

### 20日涨幅 ret20 分桶
        n  win%  avg%
ret20                
<0      0   NaN   NaN
0~10   55  49.1 -0.32
10~20  84  50.0  0.05
20~35  66  45.5  0.13
>35     0   NaN   NaN

### 板块涨幅分位 ind_rank 分桶
            n  win%  avg%
ind_rank                 
<0.6       14  42.9 -0.12
0.6~0.8    44  43.2  0.08
0.8~0.85   28  42.9 -0.41
≥0.85     119  52.1  0.04

### 5日板块分位 ind_rank5 分桶
             n  win%  avg%
ind_rank5                 
<0.6        33  63.6  1.76
0.6~0.8     50  50.0  0.07
0.8~0.85    21  28.6 -1.02
≥0.85      101  46.5 -0.45

### 板块涨停数 ind_lu_cnt 分桶
              n  win%  avg%
ind_lu_cnt                 
0             8  62.5 -0.35
1            22  50.0  0.40
2            54  51.9  0.31
≥3          121  45.5 -0.23

### 量比 vol_ratio 分桶
             n  win%  avg%
vol_ratio                 
<0.5         0   NaN   NaN
0.5~0.8     48  52.1  0.22
0.8~1.3    157  47.1 -0.10
1.3~2.2      0   NaN   NaN
>2.2         0   NaN   NaN

### 当日成交额 分桶
         n  win%  avg%
amount                
<4亿      0   NaN   NaN
4~10亿   82  41.5 -0.65
10~30亿  96  53.1  0.14
>30亿    27  51.9  1.27

### 入选分 score 分桶
         n  win%  avg%
score                 
<55      0   NaN   NaN
55~60    0   NaN   NaN
60~65    0   NaN   NaN
65~70    0   NaN   NaN
≥70    205  48.3 -0.02

### 买点触发 vs 未触发
                 n   win%  avg%
buy_triggered                  
False          111    4.5 -3.43
True            94  100.0  4.00

### 按市场环境
             n  win%  avg%
regime                    
aggressive  40  45.0 -0.47
defensive   77  46.8  0.00
normal      88  51.1  0.16
