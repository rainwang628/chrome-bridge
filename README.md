# Chrome 桥接（chrome-bridge）

让 DSH 在你**已经登录**的 Chrome 窗口里读页面、填表单、点按钮，不用重启浏览器、不用复制 cookie。

## 结构

```
DSH / ctl.py  ──HTTP(127.0.0.1:8777)──▶  server.py  ──WebSocket──▶  Chrome 扩展  ──▶  标签页
```

- `server.py`：本地桥接服务，只用 Python 标准库。
- `extension/`：MV3 扩展，负责真正在页面里执行。
- `ctl.py`：命令行客户端，也是可 `import` 的 Python 库。
- `token`：本地口令，服务端首次启动自动生成（0644→0600 权限），防止别的进程/网页乱发命令。
- `extension/config.js`：由服务端从 `token` 自动生成，扩展实际读取它；仓库里只有 `config.example.js` 模板。

**凭据不入库**：`token` 与 `extension/config.js` 含真实口令，已由 `.gitignore` 排除，仓库只保留模板。换机或重装时删掉 `token` 重启 `./run.sh`，服务端会重新生成口令并写好扩展配置。

## 一次性安装

1. 启动服务：`./run.sh`
2. 打开 `chrome://extensions`，右上角打开「开发者模式」
3. 「加载已解压的扩展程序」→ 选 `~/rain/projs/apps/chrome-bridge/extension`
4. 回到 `python3 ctl.py status`，看到 `"connected": true` 就成了

扩展装好后会自动重连；服务没开时扩展每 2 秒重试一次，服务开了就自己接上。

## 常用命令

```bash
python3 ctl.py status                 # 连接状态
python3 ctl.py tabs                   # 列出标签页（* 是当前页）
python3 ctl.py goto https://example.com   # 新开标签页并等加载完
python3 ctl.py text h1                # 读文字
python3 ctl.py js 'document.title'    # 在页面执行任意表达式
python3 ctl.py fill '#username' 张三
python3 ctl.py fillm '{"#user":"abc","#pwd":"123"}'
python3 ctl.py click 'button[type=submit]'
python3 ctl.py wait '.result' --timeout 10       # 等元素出现
python3 ctl.py waittext '提交成功'                # 等文字出现
python3 ctl.py shot ~/rain/tmp/x.png  # 当前可见区域截图
```

当库用：

```python
import sys; sys.path.insert(0, "/path/to/chrome-bridge")  # 按本机实际路径
import ctl
ctl.fillm({"#user": "abc", "#pwd": "123"})
ctl.click("button[type=submit]")
print(ctl.text(".result"))
```

## 能做什么 / 不能做什么

能：读 DOM、执行任意 JS、填写 input/textarea/contenteditable、派发鼠标和键盘事件、切换/新建/关闭标签页、截图、等待元素。

不能：`chrome://`、`chrome-extension://`、Chrome 应用商店、PDF 内置查看器这些页面不允许注入，会明确报错。

注意：

- 页面用严格 CSP 时，执行路径会自动从 `chrome.scripting` 切到 DevTools 协议（`chrome.debugger`），后者是浏览器级调试，**不会**被页面 CSP 限制，但短时间内该标签页顶部可能出现一条「正在调试此浏览器」提示，命令结束即消失。
- 填值走的是原生 setter + `input`/`change` 事件，React/Vue 受控组件能识别；但少数库只认真实键盘输入（`isTrusted`），这类需要走 `chrome.debugger` 的 `Input.dispatchKeyEvent`，当前没接。
- 扩展权限是 `<all_urls>` + `debugger`，等于「能读写你所有网站的页面」。这是本地个人工具的取舍；不用时在 `chrome://extensions` 里关掉或移除即可。

## 本地测试页

`demo/form.html` 是填表流程的靶子，`demo/csp.html` 用来验证严格 CSP 下的回退路径：

```bash
(cd demo && python3 -m http.server 8788 --bind 127.0.0.1 &)
python3 ctl.py goto http://127.0.0.1:8788/form.html
```

服务重启后扩展会在 2 秒内自动重连，不需要动 `chrome://extensions`。`bridge.log` 里每个命令记一行 `方法 ok 耗时`。

## 排错

| 现象 | 处理 |
| --- | --- |
| `扩展未连接` | 打开 `chrome://extensions` 看扩展是否启用；点扩展图标会触发重连 |
| `找不到元素` | selector 不对或页面还没渲染，先 `wait` 或 `waittext` |
| `这个页面不允许脚本注入` | 换普通 http(s) 页面 |
| 端口被占 | `BRIDGE_PORT=8788 ./run.sh`；服务端会自动把新 port 写进扩展配置，在扩展页点「重新加载」 |
| 想换口令 | 删 `token` 后重启服务，服务端自动生成新 token 并同步写进 `extension/config.js`，在扩展页点「重新加载」 |

## 卸载

`chrome://extensions` 移除扩展；删掉本目录；`pkill -f chrome-bridge/server.py`。
