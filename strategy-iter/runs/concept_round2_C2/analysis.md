# 轮末因子分析 concept_round2_C2  (n=483)

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

## 非涨停组 (n=165)
corr(close_ret): score:+0.131  dist_high60:+0.103  ret20:+0.094  vol_ratio:-0.047  amount:+0.117  ind_rank:+0.145  ind_rank5:+0.010  ind_lu_cnt:+0.093  amt_ratio:+0.572  con_pct:+0.099  con_lu_cnt:+0.045  con_pct5:+0.065

### 距60日高点 dist_high60 分桶
               n  win%  avg%
dist_high60                 
<0.86          0   NaN   NaN
0.86~0.90    107  49.5  0.49
0.90~0.95     58  48.3  0.73
0.95~0.99      0   NaN   NaN
≥0.99          0   NaN   NaN

### 20日涨幅 ret20 分桶
        n  win%  avg%
ret20                
<0      0   NaN   NaN
0~10   53  54.7  0.80
10~20  74  44.6  0.07
20~35  38  50.0  1.26
>35     0   NaN   NaN

### 板块涨幅分位 ind_rank 分桶
           n  win%  avg%
ind_rank                
<0.6      18  22.2 -1.88
0.6~0.8   35  51.4  0.38
0.8~0.85  19  68.4  1.97
≥0.85     93  49.5  0.84

### 5日板块分位 ind_rank5 分桶
            n  win%  avg%
ind_rank5                
<0.6       38  55.3  0.72
0.6~0.8    30  46.7 -0.22
0.8~0.85   19  42.1 -0.01
≥0.85      78  48.7  0.95

### 板块涨停数 ind_lu_cnt 分桶
             n  win%  avg%
ind_lu_cnt                
0           11  36.4 -1.05
1           25  40.0 -0.35
2           31  61.3  1.47
≥3          98  49.0  0.71

### 驱动概念涨幅 con_pct 分桶
          n   win%  avg%
con_pct                 
<-3       1  100.0  0.16
-3~-1    11   36.4 -0.01
-1~0     16   68.8  2.16
0~2      65   38.5 -0.25
>2       72   55.6  1.07

### 概念5日涨幅 con_pct5 分桶
           n  win%  avg%
con_pct5                
<-5        7  71.4  1.92
-5~0      41  43.9 -0.17
0~5       77  48.1  0.77
>5        39  53.8  0.82

### 概念内涨停 con_lu_cnt 分桶
              n  win%  avg%
con_lu_cnt                 
缺失/0          1   0.0 -1.71
1             8  25.0 -0.37
2             5  60.0 -1.19
≥3          151  50.3  0.70

### 量比 vol_ratio 分桶
             n  win%  avg%
vol_ratio                 
<0.5         0   NaN   NaN
0.5~0.8     25  40.0  0.09
0.8~1.3    140  50.7  0.66
1.3~2.2      0   NaN   NaN
>2.2         0   NaN   NaN

### 当日成交额 分桶
          n  win%  avg%
amount                 
<4亿       0   NaN   NaN
4~10亿     0   NaN   NaN
10~30亿  120  44.2  0.20
>30亿     45  62.2  1.58

### 入选分 score 分桶
         n  win%  avg%
score                 
<55      0   NaN   NaN
55~60    0   NaN   NaN
60~65    0   NaN   NaN
65~70    0   NaN   NaN
≥70    165  49.1  0.58

### 买点触发 vs 未触发
                n   win%  avg%
buy_triggered                 
False          89    5.6 -3.04
True           76  100.0  4.81

### 按市场环境
            n  win%  avg%
regime                   
defensive  77  51.9  0.54
normal     88  46.6  0.60
