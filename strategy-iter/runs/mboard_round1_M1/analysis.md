# 轮末因子分析 mboard_round1_M1  (n=566)

## 涨停组 (n=242)
corr(close_ret): score:+0.075  dist_high60:-0.009  ret20:+0.106  vol_ratio:-0.065  amount:+0.040  seal_ratio:+0.119  cont_cnt:+0.160  theme_cnt:+0.009  amt_ratio:+0.053

### 涨停时间(小时) 分桶
           n  win%  avg%
lu_hour                 
09       178  68.0  3.49
10        48  52.1  1.90
11         9  77.8  3.12
13         6  50.0  1.56
14         1   0.0 -4.14

### 次日开盘 open_ret 分桶
           n  win%  avg%
open_ret                
<0        75  34.7 -1.22
0~2       42  57.1  1.68
2~5       53  81.1  5.13
5~8       36  86.1  6.58
>8        35  88.6  7.23

### 封单比 seal_ratio 分桶
              n  win%  avg%
seal_ratio                 
<3%           2  50.0  0.10
3~8%         12  58.3  2.20
8~15%        53  64.2  2.57
>15%        175  65.1  3.33

### 连板数 cont_cnt 分桶
            n   win%  avg%
cont_cnt                  
1板        163   62.6  2.73
2板         54   61.1  2.87
3板         18   77.8  5.10
≥4板         7  100.0  7.79

### 题材内涨停 theme_cnt 分桶
             n  win%  avg%
theme_cnt                 
2           44  65.9  3.14
3~4         83  61.4  2.77
≥5         115  66.1  3.28

### 当日成交额 分桶
          n   win%  avg%
amount                  
<1.5亿     0    NaN   NaN
1.5~3亿   41   63.4  2.65
3~100亿  200   64.5  3.17
>100亿     1  100.0  2.61

### 入选分 score 分桶
         n   win%  avg%
score                  
<55      0    NaN   NaN
55~60    2  100.0  6.47
60~65    5   60.0  1.09
65~70   21   57.1  2.36
≥70    214   65.0  3.17

### 买点触发 vs 未触发
                 n  win%  avg%
buy_triggered                 
False          186  54.8  2.50
True            56  96.4  5.02

### 按市场环境
             n  win%  avg%
regime                    
aggressive  80  57.5  2.32
defensive   76  60.5  2.91
normal      86  74.4  3.94

## 非涨停组 (n=324)
corr(close_ret): score:+0.017  dist_high60:-0.193  ret20:-0.057  vol_ratio:-0.115  amount:+0.017  ind_rank:+0.034  ind_rank5:+0.042  ind_lu_cnt:-0.074  amt_ratio:+0.447

### 距60日高点 dist_high60 分桶
               n   win%  avg%
dist_high60                  
<0.86          0    NaN   NaN
0.86~0.90      1  100.0  3.80
0.90~0.95    107   51.4  0.86
0.95~0.99    187   40.1 -0.18
≥0.99         29   34.5 -1.35

### 20日涨幅 ret20 分桶
         n  win%  avg%
ret20                 
<0       0   NaN   NaN
0~10    57  49.1  0.65
10~20  132  39.4 -0.07
20~35  135  45.2 -0.03
>35      0   NaN   NaN

### 板块涨幅分位 ind_rank 分桶
            n  win%  avg%
ind_rank                 
<0.6        3   0.0 -4.29
0.6~0.8    20  35.0 -0.61
0.8~0.85   20  45.0 -0.03
≥0.85     281  44.5  0.17

### 5日板块分位 ind_rank5 分桶
             n  win%  avg%
ind_rank5                 
<0.6        30  40.0 -0.25
0.6~0.8     43  37.2 -0.52
0.8~0.85    26  50.0  0.30
≥0.85      225  44.4  0.20

### 板块涨停数 ind_lu_cnt 分桶
              n  win%  avg%
ind_lu_cnt                 
0             5  40.0  1.59
1            13  38.5 -1.31
2            31  58.1  0.85
≥3          275  42.2  0.02

### 量比 vol_ratio 分桶
             n  win%  avg%
vol_ratio                 
<0.5         0   NaN   NaN
0.5~0.8     25  52.0  1.69
0.8~1.3    190  43.7  0.17
1.3~2.2    109  41.3 -0.47
>2.2         0   NaN   NaN

### 当日成交额 分桶
          n  win%  avg%
amount                 
<4亿       0   NaN   NaN
4~10亿   129  45.0 -0.06
10~30亿  131  45.0  0.33
>30亿     64  37.5 -0.20

### 入选分 score 分桶
         n  win%  avg%
score                 
<55      0   NaN   NaN
55~60    0   NaN   NaN
60~65    0   NaN   NaN
65~70    0   NaN   NaN
≥70    324  43.5  0.07

### 买点触发 vs 未触发
                 n   win%  avg%
buy_triggered                  
False          193    5.2 -2.75
True           131  100.0  4.23

### 按市场环境
              n  win%  avg%
regime                     
aggressive   80  38.8 -0.51
defensive   156  43.6  0.02
normal       88  47.7  0.70
