# Loguru 敏感信息泄露定位与生产修正（可重放）

面向 Loguru 0.7.3（本仓库源码即该版本）。所有敏感值均为本目录内定义的
**假数据常量**，统一带 `FAKE-` 与 `TEST-MARKER` 标记；脚本不读取任何环境
变量、凭据文件或进程状态。

## 如何运行

```bash
# 泄露矩阵（普通脚本，打印每个用例实际观察到的 marker）
python3 security_log_leak_audit/leak_probe.py

# 修正后的零泄露验证
python3 security_log_leak_audit/verify_hardening.py

# 两者都可作为 pytest 回归测试（目录内自带 pytest.ini，覆盖仓库级 testpaths）
pytest security_log_leak_audit/
# 或在仓库根目录显式传文件
python3 -m pytest security_log_leak_audit/leak_probe.py security_log_leak_audit/verify_hardening.py
```

## 实测到的泄露路径（只列观察到的事实）

下表中的“消息自带”指敏感值在 Loguru 格式化之前就已存在于
`message` / 异常 `args` / `extra`；“诊断器局部变量”指敏感值只存在于栈帧
局部变量，由 better-exceptions 主动渲染。用例编号对应 `leak_probe.py` 输出。

| 用例 | 入口 / sink | diagnose=False | diagnose=True 额外泄露 |
| --- | --- | --- | --- |
| A1 | 普通 `logger.error()`，文本 sink | 消息自带 `FAKE-PW` | 无新增（无异常帧） |
| A2 | `serialize=True` JSON sink | `record.message`、`record.extra`、`text` 三处均含 `FAKE-PW`/`FAKE-TEMP-TOKEN` | 同左 |
| B1 | `opt(exception=...)` 显式 `__cause__` 链，文本 | 外层异常消息中的 `FAKE-INNER-SECRET` | 内异常 raise 行引用的局部变量 `FAKE-TEMP-TOKEN` |
| B2 | 异常链 + `serialize=True` | 泄露只在 JSON 的 `text` 字段；`record.exception` 仅 `{type, value, traceback:bool}`，不含 traceback 内容 | 同左 + `text` 中出现局部变量 |
| B3 | 隐式 `__context__`，`backtrace=False` | 异常消息 marker 仍在 | 帧局部变量仍被渲染 |
| B4 | `enqueue=True` 自定义 sink | 与同步 sink 一致（`diagnose` 是 per-sink 选项，入队不改变结论） | 同 B1 |
| C1 | `ExceptionGroup` 子异常 | 子异常消息 marker 在两种 diagnose 下都在 | 同左 |
| D1 | `@logger.catch` 装饰器入口 | 异常消息中的 `FAKE-PW` 在两种 diagnose 下都在 | 仅当局部变量在 traceback 源代码行被**引用**时才渲染该变量 |
| E1 | 标准库 `logging` 经 `InterceptHandler` 转接，`logging.exception(...)` | stdlib 格式化消息 + 异常链消息均带 marker，两种 diagnose 相同 | 无额外（stdlib 调用帧不是用户帧，better-exceptions 不渲染其局部变量） |
| F1 | 自定义可调用 sink 收到的原始 record | sink 代码可自行遍历 `__cause__`/`__context__`，内异常消息始终可达，与 diagnose 无关 | — |
| G1 | sink 自身抛错，`catch=True`（默认） | ErrorInterceptor 向 **stderr** 打印 `Record was: <record repr>` 与标准库 traceback，消息里的 `FAKE-PW`/`FAKE-TEMP-TOKEN` 全部落 stderr | — |
| G1 | 同上但 `catch=False` | 错误直接向上抛，stderr 兜底路径不执行（无该路径泄露，但调用方要自己处理异常） | — |

### 关键结论

1. **关掉某个 sink 的 `diagnose` 只能消除“诊断器局部变量”这一类**。消息本身、
   异常 `args`、`bind()/extra`、异常链中任何一环的异常消息里的敏感值，在
   `diagnose=False` 下依旧原样输出（B1/B2/E1 均观察到）。
2. **`serialize=True` 不是脱敏层**。结构化的 `record.exception` 只有
   type/value/布尔值，但完整 traceback（含局部变量，取决于该 sink 的 diagnose）
   被原样塞进 JSON 的 `text` 字段；`record.message`、`record.extra` 也原样保留。
3. **局部变量的渲染条件比直觉更窄**：只有出现在某条 traceback 帧源代码行、
   且被该行引用的名字才会被 better-exceptions 展示（见
   `loguru/_better_exceptions.py` 的 `_get_relevant_values`）。没被引用的
   局部变量不会出现，但不能把这当成保护手段——重构代码即可改变结果。
4. **标准 logging 转接不增加也不减少保护**：`logger.opt(exception=record.exc_info)`
   之后完全由 Loguru sink 的 `diagnose`/格式化决定；stdlib 消息里拼了什么就带什么。
5. **`catch=True`（默认）的 stderr 兜底是独立泄露面**：`ErrorInterceptor.print`
   用 `str(record)` 打印整条 record，绕过所有 sink 的格式化与过滤。
6. **Loguru 没有承诺任何脱敏能力**。源码与文档中不存在 redaction/masking/
   scrubbing 配置项；`filter`、`patcher` 能改 record，但 better-exceptions 的
   局部变量渲染发生在 handler 内部、在它们之后，因此改 record 挡不住
   `diagnose=True` 的局部变量，而且直接改异常 `args` 会污染调用方持有的活异常对象。
7. 默认值本身偏向开发体验：`LOGURU_DIAGNOSE`、`LOGURU_BACKTRACE`、
   `LOGURU_CATCH` 默认全部为 `True`（见 `loguru/_defaults.py`）。

## 生产配置修正

见 `hardened_logging.py`，要点：

- 每个 sink 显式 `diagnose=False`（唯一能关掉局部变量展示的内建开关）。
- `catch=False`，避免 sink 故障时 ErrorInterceptor 把整条 record 打到 stderr；
  由上层统一处理 sink 异常。
- 用**自定义 sink 在最终输出上做正则脱敏**：
  - `ScrubbingStreamSink` 对 `str(message)`（已含 traceback 的最终文本）脱敏，
    即使将来有人误开 `diagnose=True`，渲染出来的局部变量也过不了这一层；
  - `ScrubbingJsonSink` 不使用 `serialize=True`，而是自定义 schema，对
    `message`、`extra`、异常链每个 `value`、以及脱敏后的 traceback 文本
    分别清洗后再 `json.dumps`。
- 输出层脱敏不修改 `record["exception"]` 里的活异常对象（`verify_hardening.py`
  中有专门断言保证异常 `args` 不变）。
- `InterceptHandler` 与 README 官方配方一致，stdlib 流量走同一批受控 sink。
- `enqueue=True` 与上述措施兼容；注意入队依赖 pickle，自定义 sink 与
  record 内容需可 pickle。

## 自定义 sink 仍需自行承担的责任

Loguru 只负责把 `Message`（带原始 `record`）交给 sink，以下事项 sink 自负：

1. **对最终字符串与结构化字段分别脱敏**。只清洗 `record["message"]` 不够，
   `str(message)` 里的 traceback、`extra`、异常链 `__cause__`/`__context__`
   （含 `ExceptionGroup` 拆分出的子异常）都要覆盖。
2. **不要假设 `diagnose=False` 就安全**：record 里的异常消息、`extra` 仍可能
   含明文；任何把 record 再序列化（写 JSON、发网络、转存）的 sink 都要自带清洗。
3. **不能在 sink 里再打日志**：handler 不可重入，sink 内调用 `logger` 会触发
   死锁保护；sink 内部错误要就地处理。
4. **自己决定 sink 异常策略**：`catch=True` 会把 record repr 打到 stderr（本报告
   G1），生产 sink 建议 `catch=False` 并在部署层处理失败，避免旁路泄露。
5. **JSON sink 自行处理不可序列化对象**：内建 `serialize=True` 用
   `json.dumps(default=str)`，会把任意对象 `str()` 后明文写出；自定义 schema
   要显式决定如何表示/拒绝这些对象。
6. **线程/进程安全与落盘权限**：多线程写入、`enqueue` 的 pickle 边界、文件
   权限与轮转（`rotation`/`retention`）都由 sink 配置负责，Loguru 不做脱敏落盘。
7. **模式维护**：正则表（`DEFAULT_SECRET_PATTERNS`）必须随凭据形态演进；
   基于模式的脱敏无法覆盖未知格式，应与“不要把口令拼进异常消息/日志消息”的
   上游编码规范一起落地。

## 文件清单

- `leak_probe.py`：假数据夹具 + 21 个用例的泄露矩阵 + 实测行为 oracle（pytest）。
- `hardened_logging.py`：生产可用的加固配置（脱敏 sink、stdlib 转接、安全默认值）。
- `verify_hardening.py`：复用同一套假数据，验证加固后全部路径零泄露且不污染
  活异常对象（pytest）。
- `pytest.ini`：让本目录可独立被 pytest 收集。
