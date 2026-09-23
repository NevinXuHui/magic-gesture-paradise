# 组件版本信息详情
# Component Version Information

# 组件名称，唯一标识
string component_name

# 语义化版本号 (major.minor.patch)
string version

# 组件类型 (third_party/algorithm/business)
string component_type

# 组件描述文本
string description

# 子模块版本列表
SubModuleVersion[] sub_modules

# 构建时间戳 (Unix timestamp)
int64 build_timestamp

# Git提交哈希值（可选）
string git_commit_hash

