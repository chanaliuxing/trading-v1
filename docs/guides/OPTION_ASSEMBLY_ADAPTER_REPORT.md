# Option Assembly Adapter Report

目的：把“plugin 是否真的只是 assembly adapter”从人工判断变成自动报告。  
权威工具：

- `./tool/report/build_option_assembly_adapter_report.py`

## 检查内容

active option strategy 必须同时满足：

1. strategy 由 plugin-kernel 路径承载
2. plugin 标记为 `assembly_adapter_mode = true`
3. `apply_default_extras()` 产出完整 definition identity
4. `kernel_mode` 属于 `plugin / pure_plugin / hybrid`
5. strategy 不是一个没有 `definition_id` 的 legacy runtime path

## 当前意义

这份报告不是替代 replay / shadow / acceptance。  
它解决的是更前一层的问题：

1. 这个 active option strategy 到底是不是 definition-driven
2. plugin 有没有偷带第二套私有语义
3. 平台是否已经具备“assembly adapter only”的基本准入门

## Operator 命令

```bash
./venv/bin/python tool/report/build_option_assembly_adapter_report.py
```

如需落盘：

```bash
./venv/bin/python tool/report/build_option_assembly_adapter_report.py \
  --output runtime/state/option_assembly_adapter_report.json
```
