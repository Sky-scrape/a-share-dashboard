# 轮末因子分析 mboard_round3_M3  (n=483)

## 涨停组 (n=318)
corr(close_ret): score:+0.114  dist_high60:-0.005  ret20:+0.129  vol_ratio:-0.030  amount:+0.010  seal_ratio:+0.072  cont_cnt:+0.136  theme_cnt:+0.052  amt_ratio:+0.043

### 涨停时间(小时) 分桶
           n  win%  avg%
lu_hour                 
09       238  65.5  3.14
10        59  59.3  2.17
11        12  83.3  3.37
13         7  42.9  0.39
14         2  50.0 -0.25

### 次日开盘 open_ret 分桶
           n  win%  avg%
open_ret                
<0        95  38.9 -0.91
0~2       61  57.4  1.91
2~5       68  80.9  4.51
5~8       43  83.7  6.12
>8        49  83.7  6.58

### 封单比 seal_ratio 分桶
              n  win%  avg%
seal_ratio                 
<3%           2   0.0 -1.68
3~8%         24  58.3  2.05
8~15%        73  74.0  3.28
>15%        219  62.6  2.89

### 连板数 cont_cnt 分桶
            n  win%  avg%
cont_cnt                 
1板        192  62.5  2.42
2板         94  63.8  3.17
3板         21  76.2  4.68
≥4板        11  81.8  5.07

### 题材内涨停 theme_cnt 分桶
             n  win%  avg%
theme_cnt                 
2           90  62.2  2.60
3~4        106  62.3  2.57
≥5         122  68.0  3.37

### 当日成交额 分桶
          n   win%  avg%
amount                  
<1.5亿     0    NaN   NaN
1.5~3亿   50   52.0  1.61
3~100亿  265   66.4  3.12
>100亿     3  100.0  3.57

### 入选分 score 分桶
         n   win%  avg%
score                  
<55      0    NaN   NaN
55~60    1  100.0  0.17
60~65    7   71.4  1.65
65~70   21   57.1  1.93
≥70    289   64.7  2.99

### 买点触发 vs 未触发
                 n  win%  avg%
buy_triggered                 
False          225  50.7  1.64
True            93  97.8  5.91

### 按市场环境
              n  win%  avg%
regime                     
aggressive   80  60.0  2.07
defensive   151  60.9  2.83
normal       87  74.7  3.72

## 非涨停组 (n=165)
corr(close_ret): score:+0.017  dist_high60:+0.064  ret20:+0.170  vol_ratio:-0.042  amount:+0.053  ind_rank:-0.050  ind_rank5:-0.042  ind_lu_cnt:+0.114  amt_ratio:+0.524

### 距60日高点 dist_high60 分桶
               n  win%  avg%
dist_high60                 
<0.86          0   NaN   NaN
0.86~0.90    111  49.5  0.77
0.90~0.95     54  46.3  0.51
0.95~0.99      0   NaN   NaN
≥0.99          0   NaN   NaN

### 20日涨幅 ret20 分桶
        n  win%  avg%
ret20                
<0      0   NaN   NaN
0~10   42  54.8  0.85
10~20  77  42.9 -0.01
20~35  46  52.2  1.71
>35     0   NaN   NaN

### 板块涨幅分位 ind_rank 分桶
           n  win%  avg%
ind_rank                
<0.6      15  40.0 -0.21
0.6~0.8   34  52.9  1.03
0.8~0.85  17  64.7  2.06
≥0.85     99  45.5  0.47

### 5日板块分位 ind_rank5 分桶
            n  win%  avg%
ind_rank5                
<0.6       37  54.1  0.80
0.6~0.8    38  50.0  0.64
0.8~0.85   14  35.7 -0.23
≥0.85      76  47.4  0.82

### 板块涨停数 ind_lu_cnt 分桶
             n  win%  avg%
ind_lu_cnt                
0           10  30.0 -1.45
1           28  46.4  0.88
2           35  57.1  1.37
≥3          92  47.8  0.60

### 量比 vol_ratio 分桶
             n  win%  avg%
vol_ratio                 
<0.5         0   NaN   NaN
0.5~0.8     34  44.1  0.84
0.8~1.3    131  49.6  0.65
1.3~2.2      0   NaN   NaN
>2.2         0   NaN   NaN

### 当日成交额 分桶
          n  win%  avg%
amount                 
<4亿       0   NaN   NaN
4~10亿     0   NaN   NaN
10~30亿  122  45.1  0.43
>30亿     43  58.1  1.40

### 入选分 score 分桶
         n  win%  avg%
score                 
<55      0   NaN   NaN
55~60    0   NaN   NaN
60~65    0   NaN   NaN
65~70    0   NaN   NaN
≥70    165  48.5  0.69

### 买点触发 vs 未触发
                n   win%  avg%
buy_triggered                 
False          90    5.6 -2.91
True           75  100.0  5.00

### 按市场环境
            n  win%  avg%
regime                   
defensive  77  46.8  0.43
normal     88  50.0  0.91
