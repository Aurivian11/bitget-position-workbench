# 新服务器验证记录 · 2026-11-05

验证环境：Debian 12、Python 3.11.2、Node.js 18.20.4，仅在独立代码副本内运行测试。Python运行依赖按 `config/requirements.lock` 安装，Node仅用于前端语法检查。

## 确认并修复的问题

通知配置原先只在判断本地Bark地址时读取URL端口，外部HTTPS主机的 `:0`、`:65536`、`:invalid` 因而可以被保存，随后通知传输无法使用该地址。

现在所有Bark地址先验证端口是否有效，再判断HTTPS或本机HTTP规则。无效配置在保存前被拒绝，合法自定义HTTPS端口继续支持。

回归先在未修复代码上复现了3种错误输入，再确认修复后通过。没有访问实际Bark服务。

## 检查结果

- `PYTHONPATH=app .venv/bin/python -B -m unittest discover -s tests -v`：44项通过（原42项及新增2项，覆盖3种错误端口和合法自定义端口）。
- `PYTHONPATH=app .venv/bin/python -B app/test_bot.py`：12项通过。
- `python -B tools/build_web.py`：生成成功，已提交页面与重新生成结果一致。
- 4份前端JavaScript通过 `node --check`；应用、工具和测试Python源码均通过语法解析。

测试使用虚构凭据、临时数据库及模拟外部响应，没有启用账户监控、真实通知或交易请求。真实Bitget权限、平台响应与手机送达不属于本次离线验证范围。
