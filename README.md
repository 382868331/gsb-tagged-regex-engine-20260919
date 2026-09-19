# 带捕获组的有限正则匹配引擎

协议校验组件需要无指数回溯风险的有限正则完整匹配，并返回捕获跨度。本仓库提供纯标准库实现 `regex_engine.py`，只支持完整匹配（fullmatch)，不支持 search/替换。

## 接口

```python
import regex_engine as rx

pat = rx.compile("(a|ab)(c|bcd)(d*)")   # 非法语法/超限抛 rx.RegexError
m = pat.fullmatch("abcd")               # 必须消耗整个输入，否则返回 None
m.spans        # ((0, 4), (0, 1), (1, 4), (4, 4))，下标0为整体匹配
m.span(2)      # (1, 4)，code point 跨度
m.group(2)     # 'bcd'
m.state_count  # 本次匹配实际处理的 NFA 状态数（失败时经 pat.state_count 读取）

rx.fullmatch(pattern, text)             # 便捷函数：编译并匹配
```

- 捕获组按左括号顺序编号（1..n)，组0为整体匹配。
- 成功路径上每组保留最后一次完成的捕获；从未参与的组为 `None`，空捕获是真实跨度 `(i, i)`，二者可区分。
- `Pattern.groups` 为捕获组数量，`Pattern.n_states` 为 NFA 状态数。

## 语法

支持：Unicode 字面字符、转义元字符、`.`（匹配含换行在内的任意 code point)、连接、`|`、捕获括号 `()`、贪婪量词 `* + ?`。允许空模式和空括号 `()`。

拒绝（抛 `RegexError`)：未转义的 `[]{}^$`（保留字符，转义后为字面量）、`\d` 等不支持的转义、空 alternation 分支（`a|`、`|a`)、量词叠加（`a**`)、字符类及其他扩展。`*`/`+` 作用于可匹配空串的操作数时在编译期报错（如 `(a?)*`、`(a*)+`);`?` 可作用于可空体。

限制：模式 ≤256 字符，括号深度 ≤32，捕获组 ≤16，输入 ≤2000 code point。

## 实现要点

编译为有序 Thompson NFA 并打捕获标签：分支优先左侧、量词优先进入（贪婪）。匹配是 Pike VM 式模拟——epsilon 闭包按优先序处理，每个输入位置对同一状态只保留最高优先路径，因此处理状态数随输入长度线性增长，无指数回溯。引擎本身不调用 `re`；测试用 `re.fullmatch(pattern, text, re.DOTALL)` 对共同支持子集做固定种子随机交叉核验。

## 环境与命令

Windows 原生 Python 3.14.7，仅标准库，无第三方依赖、外部服务或 Docker。

演示（约 1 秒内完成，展示正常匹配、真实失败、可空重复拒绝和状态计数线性增长）：

```
python demo.py
```

测试：

```
python -m unittest discover -s tests -v
```
