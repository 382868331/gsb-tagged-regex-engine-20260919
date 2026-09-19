# 带捕获组的有限正则匹配引擎

协议校验组件需要无指数回溯风险的有限正则完整匹配，并返回捕获跨度。
`regex_engine.py` 将模式编译为有序 Thompson NFA（带捕获标签），用按优先序处理的
有序状态集合模拟：每个输入位置对同一 NFA 状态只保留最高优先路径，epsilon 闭包
按优先序深度优先处理并去重，不使用指数回溯，匹配过程不调用 `re`。

## 接口

```python
from regex_engine import compile, fullmatch, RegexError

prog = compile('(a|ab)(c|bcd)')   # 非法模式抛 RegexError
m = prog.fullmatch('abcd')        # 必须消耗整个输入；失败返回 None
m.span(0)                         # (0, 4)，整体跨度（code point 下标）
m.span(1)                         # (0, 1)，按左括号顺序编号的捕获组
m.spans                           # ((0, 4), (0, 1), (1, 4))，组 0..n
m.groups()                        # 组 1..n 的跨度
m.states_processed                # 本次匹配实际处理的状态计数
prog.last_state_count             # 最近一次 fullmatch 的状态计数（失败也有）
prog.groups                       # 捕获组数量
```

- 成功路径上每组**最后一次完成**的捕获保留；从未参与的组为 `None`，
  与空跨度（如 `(0, 0)`）严格区分。
- 便捷函数 `fullmatch(pattern, text)` 等价于 `compile(pattern).fullmatch(text)`。

## 语法

支持：Unicode 字面字符、转义元字符（`\. \| \( \) \* \+ \? \[ \] \{ \} \^ \$ \\`）、
点号（匹配含换行在内的任意 code point）、连接、`|`（左分支优先）、捕获括号、
贪婪 `* + ?`（优先进入循环体）。允许空模式和空括号 `()`。

拒绝：`[]{}^$` 未转义、其他转义（如 `\d`、`\n`）、空 alternation 分支（`a|`、`|a`）、
量词叠加（`a**`、`a*?`）、字符类及其他扩展。编译时计算 nullable，`*`/`+` 的操作数
可匹配空串则报错（如 `(a?)*`、`(a*)+`）；`?` 可作用于可空体。

限制：模式 ≤ 256 字符、括号深度 ≤ 32、捕获组 ≤ 16、输入 ≤ 2000 code point。

## 环境与命令

Windows 原生 Python 3.14.7，仅标准库，无第三方依赖、外部服务或 Docker。

演示（约 8 秒内，展示正常结果、可空重复拒绝与实际触发的匹配失败及线性状态计数）：

```
python demo.py
```

测试（含与 `re.fullmatch(pattern, text, re.DOTALL)` 的固定种子随机交叉核验）：

```
python -m unittest discover -s tests -v
```

## 实现说明

- 解析：递归下降生成 AST，同时校验保留字符、转义、空分支、量词叠加与各项限制。
- 编译：nullable 分析拒绝可空 `*`/`+` 操作数；有序 Thompson 构造生成 NFA，
  分支左优先、量词优先进入，捕获组编译为成对的 save 标签状态。
- 匹配：Pike VM 式模拟。标签（捕获位置）随路径以不可变元组携带，闭包按优先序
  首次到达即保留，因此同一状态只存在最高优先路径的捕获历史；失败的高优先路径
  不会污染幸存路径的标签。
- 只做 fullmatch，不做 search、替换或非贪婪规则。
