# dwell 更新与调试

## 首次修正 PM2 入口

PM2 必须运行 `run.py`，否则日历、悄悄话等模块不会注册：

```bash
cd ~/dwell-on-something/backend
pm2 delete dwell-backend
pm2 start run.py --interpreter python3 --name dwell-backend
pm2 save
```

## 以后统一更新

```bash
cd ~/dwell-on-something
bash backend/update.sh
```

脚本会依次完成：

1. `git pull --ff-only`
2. 显示当前 commit
3. 重启 PM2
4. 请求 `/api/version` 验证正在运行的版本

不再需要执行：

```bash
cp web/index.html backend/index.html
```

后端会直接读取 `web/index.html`，并自动移除演示模式、修复日历残留错误、应用昵称文字和悄悄话侧栏入口。

## 手动检查

```bash
pm2 describe dwell-backend | grep -E 'script path|exec cwd'
curl -s http://127.0.0.1:8888/api/version | python3 -m json.tool
curl -s http://127.0.0.1:8888/api/whisper | python3 -m json.tool
```
