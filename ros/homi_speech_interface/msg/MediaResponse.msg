bool success            # 操作是否成功
string message          # 返回消息 "playInfo"或"getPlayResource"
int32 progress          # 播放进度(1-100)，仅在get_progress命令时有效
int32 current_time      # 当前播放时间(毫秒)，仅在get_progress命令时有效
int32 total_time        # 总时长(毫秒)，仅在get_progress命令时有效
int32 volpct            # 音量百分百 1-100
int32 status            # 播放状态：0 暂停 1 播放 2 关闭