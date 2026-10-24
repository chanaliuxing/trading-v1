# Cron 调度配置

本目录包含用于 P3-P4 组件的定期任务调度。

## 任务列表

### 记忆巩固 (Memory Consolidation)
- **频率**: 每日凌晨 2:00
- **脚本**: `memory_consolidation.sh`
- **功能**: STS → MTS → LTS 自动升级

### 实验清理 (Experiment Cleanup)
- **频率**: 每周日凌晨 3:00
- **脚本**: `experiment_cleanup.sh`
- **功能**: 清理过期实验记录

### 健康检查 (Health Check)
- **频率**: 每小时
- **脚本**: `health_check.sh`
- **功能**: 检查所有组件健康状态

## 安装

```bash
# 安装所有 cron 任务
./install_crontab.sh

# 查看当前 cron 任务
crontab -l

# 卸载
./uninstall_crontab.sh
```

## 日志

所有 cron 任务的日志输出到 `logs/cron/`
