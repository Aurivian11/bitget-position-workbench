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

## 多持仓合并与 CFD 补查 · 2026-11-06

本次更新将实时持仓合并到每个渠道的一条消息，并增加可选 CFD 只读监控、成交账本补查和 Bark 开仓提醒。渠道目标变更时，汇总消息与补查开仓消息的引用一起隔离。

验证使用 Python 3.14.7，在发布代码副本内执行：

- `PYTHONPATH=app python -B -m unittest discover -s tests -q`：114 项通过。
- `PYTHONPATH=app python -B app/test_bot.py`：12 项通过。
- 新增 70 项测试，覆盖多仓合并、关闭一个仓位、重启去重、发送回执不确定、旧消息迁移、CFD 快速开平仓、歧义匹配、费用与币种、Bark 补查及通知目标变更。

新增测试仅使用虚构成交记录和隔离数据库，不依赖部署服务器的 QA 文件、真实账户记录或密钥，也不发送真实通知。未修改前端文件，本次无需重新生成页面。实际平台权限、持仓响应与设备送达仍需部署者在自己的账户中核对。

## 网页界面重写 · 2026-11-14

v1.2.0 从零重写了网页前端，后端接口、认证与 CSRF 规则未改动。验证使用 Python 3.14.7，在发布代码副本内执行：

- `PYTHONPATH=app python -B -m unittest discover -s tests`：120 项通过。
- `PYTHONPATH=app python -B app/test_bot.py`：12 项通过。
- `python -B tools/build_web.py`：两次生成结果一致。
- `app.js`、`login.js` 通过 `node --check`。
- 前端读取的每个字段都与 `web_app.py`、`web_admin.py` 的实际返回逐项核对。
- 使用纯虚构数据的本机模拟服务，在 1440 px 桌面和 375 px 手机宽度下检查各页面、空状态、筛选分页、合约 / CFD 账户切换及设置保存流程；页面无横向滚动，浏览器控制台无错误。

README 截图来自同一模拟服务，不含真实账户数据。
