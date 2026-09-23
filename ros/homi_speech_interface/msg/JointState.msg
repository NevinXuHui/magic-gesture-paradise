# 0x01 temperature 字段有效
# 0x02 alarm 字段有效
# 0x04 freq 字段有效
# 0x08 tau_est 字段有效
int32 valid_mask

# 12个关节温度
float32[] temperature

# 12个关节告警码
uint32[] alarm

# 关节通信频率
uint32[] freq

# 关节力矩估计值 (N·m)
float32[] tau_est
