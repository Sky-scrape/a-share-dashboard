# 轮末因子分析 concept_round1_C1  (n=480)

## 涨停组 (n=318)
corr(close_ret): score:+0.076  dist_high60:-0.034  ret20:+0.082  vol_ratio:-0.038  amount:+0.004  seal_ratio:+0.062  cont_cnt:+0.119  theme_cnt:+0.054  amt_ratio:+0.030  con_pct:+0.086  con_lu_cnt:+0.049

### 涨停时间(小时) 分桶
           n  win%  avg%
lu_hour                 
09       261  65.5  3.01
10        40  60.0  2.21
11         9  66.7  2.64
13         6  66.7  2.82
14         2  50.0 -0.25

### 次日开盘 open_ret 分桶
           n  win%  avg%
open_ret                
<0        99  42.4 -0.88
0~2       63  57.1  2.10
2~5       56  82.1  4.82
5~8       45  84.4  6.10
>8        53  81.1  6.20

### 封单比 seal_ratio 分桶
              n  win%  avg%
seal_ratio                 
<3%           9  44.4  1.32
3~8%         30  60.0  1.97
8~15%        78  71.8  3.14
>15%        201  63.7  2.98

### 连板数 cont_cnt 分桶
            n  win%  avg%
cont_cnt                 
1板        177  63.8  2.48
2板        105  61.9  2.82
3板         24  79.2  5.34
≥4板        12  75.0  4.25

### 题材内涨停 theme_cnt 分桶
             n  win%  avg%
theme_cnt                 
2          123  61.8  2.45
3~4        103  61.2  2.53
≥5          92  72.8  3.82

### 驱动概念涨幅 con_pct 分桶
           n  win%  avg%
con_pct                 
<-3        2  50.0  4.06
-3~0      44  61.4  2.61
0~2      106  56.6  2.21
2~5      130  70.8  3.24
>5        36  72.2  3.79

### 概念内涨停 con_lu_cnt 分桶
              n  win%  avg%
con_lu_cnt                 
缺失/0          0   NaN   NaN
1             3  66.7  5.02
2            16  75.0  3.95
3            31  54.8  2.29
≥4          268  65.3  2.85

### 当日成交额 分桶
          n   win%  avg%
amount                  
<1.5亿     0    NaN   NaN
1.5~3亿   41   61.0  2.26
3~100亿  276   65.2  2.95
>100亿     1  100.0  7.93

### 入选分 score 分桶
         n  win%  avg%
score                 
<55      0   NaN   NaN
55~60    0   NaN   NaN
60~65    1   0.0 -6.76
65~70    4  25.0 -1.33
≥70    313  65.5  2.96

### 买点触发 vs 未触发
                 n  win%  avg%
buy_triggered                 
False          230  53.0  1.77
True            88  95.5  5.76

### 按市场环境
              n  win%  avg%
regime                     
aggressive   80  60.0  2.15
defensive   151  62.9  2.89
normal       87  72.4  3.51

## 非涨停组 (n=162)
corr(close_ret): score:+0.112  dist_high60:+0.124  ret20:+0.054  vol_ratio:-0.022  amount:+0.177  ind_rank:+0.066  ind_rank5:+0.066  ind_lu_cnt:+0.161  amt_ratio:+0.550  con_pct:+0.167  con_lu_cnt:+0.155  con_pct5:+0.112

### 距60日高点 dist_high60 分桶
               n  win%  avg%
dist_high60                 
<0.86          0   NaN   NaN
0.86~0.90    110  43.6  0.05
0.90~0.95     52  48.1  0.73
0.95~0.99      0   NaN   NaN
≥0.99          0   NaN   NaN

### 20日涨幅 ret20 分桶
        n  win%  avg%
ret20                
<0      0   NaN   NaN
0~10   45  57.8  0.90
10~20  73  39.7 -0.19
20~35  44  40.9  0.37
>35     0   NaN   NaN

### 板块涨幅分位 ind_rank 分桶
           n  win%  avg%
ind_rank                
<0.6      16  31.2 -1.07
0.6~0.8   31  45.2 -0.39
0.8~0.85  17  58.8  1.77
≥0.85     98  44.9  0.43

### 5日板块分位 ind_rank5 分桶
            n  win%  avg%
ind_rank5                
<0.6       31  41.9 -0.37
0.6~0.8    35  45.7 -0.19
0.8~0.85   15  40.0  0.16
≥0.85      81  46.9  0.72

### 板块涨停数 ind_lu_cnt 分桶
             n  win%  avg%
ind_lu_cnt                
0            9  11.1 -3.43
1           28  35.7 -0.82
2           34  58.8  1.52
≥3          91  46.2  0.50

### 驱动概念涨幅 con_pct 分桶
          n  win%  avg%
con_pct                
<-3       0   NaN   NaN
-3~-1     7  42.9  1.03
-1~0     11  45.5 -0.29
0~2      73  35.6 -0.51
>2       71  54.9  1.08

### 概念5日涨幅 con_pct5 分桶
           n  win%  avg%
con_pct5                
<-5        4  75.0  3.10
-5~0      33  33.3 -1.36
0~5       85  45.9  0.67
>5        40  50.0  0.47

### 概念内涨停 con_lu_cnt 分桶
              n  win%  avg%
con_lu_cnt                 
缺失/0         11  18.2 -1.52
1            19  26.3 -1.36
2            16  43.8 -1.29
≥3          116  50.9  0.92

### 量比 vol_ratio 分桶
             n  win%  avg%
vol_ratio                 
<0.5         0   NaN   NaN
0.5~0.8     30  33.3 -0.72
0.8~1.3    132  47.7  0.49
1.3~2.2      0   NaN   NaN
>2.2         0   NaN   NaN

### 当日成交额 分桶
          n  win%  avg%
amount                 
<4亿       0   NaN   NaN
4~10亿     0   NaN   NaN
10~30亿  113  38.9 -0.34
>30亿     49  59.2  1.67

### 入选分 score 分桶
         n  win%  avg%
score                 
<55      0   NaN   NaN
55~60    0   NaN   NaN
60~65    0   NaN   NaN
65~70    0   NaN   NaN
≥70    162  45.1  0.27

### 买点触发 vs 未触发
                n   win%  avg%
buy_triggered                 
False          93    4.3 -3.30
True           69  100.0  5.08

### 按市场环境
            n  win%  avg%
regime                   
defensive  74  40.5 -0.40
normal     88  48.9  0.83
